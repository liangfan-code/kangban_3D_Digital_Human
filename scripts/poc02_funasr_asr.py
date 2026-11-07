"""POC-02 验证脚本：FunASR 流式转写 + Edge-TTS 首包延迟基线。

流程：
  1) edge-tts 合成一句中文（同时记录 TTS 首包延迟，验收项 3 基线）；
  2) PyAV 解码为 16k/单声道/PCM16，内存中切成 60ms 帧流式喂给 FunASR WSS；
  3) 发送 is_speaking=false 后计时，等待 2pass offline 最终转写；
  4) 输出「说完→最终转写」时延与转写文本，回填 docs/POC_验证计划.md POC-02。

用法：uv run python scripts/poc02_funasr_asr.py [--url wss://127.0.0.1:10096] [--text ...]
仅生成合成音频用于测试，不落任何真实用户音频。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import ssl
import sys
import time
from pathlib import Path

import av
import numpy as np
import websockets

DEFAULT_URL = "wss://127.0.0.1:10096"
CHUNK_MS = 60
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


def pcm16_to_frames(pcm: bytes, chunk_ms: int = CHUNK_MS) -> list[bytes]:
    """把整段 PCM16 切成 chunk_ms 毫秒的帧列表（模拟麦克风流）。"""
    frame_bytes = SAMPLE_RATE * BYTES_PER_SAMPLE * chunk_ms // 1000
    return [pcm[i : i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]


def decode_audio_to_pcm16(data: bytes) -> bytes:
    """用 PyAV 解码任意容器（mp3 等）到 16k/单声道/PCM16。"""
    container = av.open(io.BytesIO(data))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    out = bytearray()
    for frame in container.decode(audio=0):
        for rf in resampler.resample(frame):
            out += bytes(rf.planes[0])
    return bytes(out)


async def synth_test_audio(text: str) -> tuple[bytes, float]:
    """edge-tts 合成测试句，返回 (pcm16_16k, tts_first_byte_ms)。"""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
    first_byte_ms: float | None = None
    t0 = time.perf_counter()
    mp3 = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            if first_byte_ms is None:
                first_byte_ms = (time.perf_counter() - t0) * 1000
            mp3 += chunk["data"]
    if first_byte_ms is None:
        raise RuntimeError("edge-tts 未产出任何音频字节")
    pcm = decode_audio_to_pcm16(bytes(mp3))
    return pcm, first_byte_ms


async def run_asr(url: str, pcm: bytes, insecure: bool = True) -> dict:
    """把 PCM 流式喂给 FunASR 2pass 服务，返回时延与转写结果。"""
    ssl_ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    frames = pcm16_to_frames(pcm)
    result: dict = {"online_texts": [], "final_text": None, "final_ms": None}

    async with websockets.connect(url, ssl=ssl_ctx, max_size=None) as ws:
        config = {
            "mode": "2pass",
            "chunk_size": [5, 10, 5],
            "chunk_interval": 10,
            "wav_name": "poc02",
            "wav_format": "pcm",
            "audio_fs": SAMPLE_RATE,
            "is_speaking": True,
        }
        await ws.send(json.dumps(config))

        # 按真实语速回放：60ms/帧
        t_start = time.perf_counter()
        for frame in frames:
            await ws.send(frame)
            await asyncio.sleep(CHUNK_MS / 1000)
        speak_end = time.perf_counter()

        # 结束信号：说完
        await ws.send(json.dumps({"is_speaking": False}))

        async def _finalize() -> None:
            """等待 offline 最终结果，记录说完→最终转写时延。

            注意：该镜像的 mode 值为 '2pass-online' / '2pass-offline'（教师样板
            用 'offline' in mode 模糊匹配，实测确认）。
            """
            async for raw in ws:
                msg = json.loads(raw)
                mode = msg.get("mode", "")
                text = msg.get("text", "")
                if "online" in mode and text:
                    result["online_texts"].append(text)
                if "offline" in mode and msg.get("is_final"):
                    result["final_text"] = text
                    result["final_ms"] = (time.perf_counter() - speak_end) * 1000
                    return

        try:
            await asyncio.wait_for(_finalize(), timeout=15)
        except TimeoutError:
            pass
        result["speak_duration_s"] = speak_end - t_start
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="POC-02 FunASR 流式转写验证")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--text", default="你好康伴，今天房间温度怎么样？")
    args = parser.parse_args()

    print(f"[POC-02] 1/3 edge-tts 合成测试句：{args.text!r}")
    pcm, tts_first_ms = await synth_test_audio(args.text)
    dur = len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
    print(f"[POC-02] TTS 首包延迟 = {tts_first_ms:.0f} ms；合成音频 {dur:.1f}s @16k")

    print(f"[POC-02] 2/3 连接 FunASR {args.url}，流式回放 {len(pcm16_to_frames(pcm))} 帧×60ms…")
    result = await run_asr(args.url, pcm)

    print("[POC-02] 3/3 结果：")
    print(f"  online(流式中间)结果: {''.join(result['online_texts']) or '(无)'}")
    print(f"  offline 最终转写    : {result['final_text'] or '(超时未返回)'}")
    if result["final_ms"] is not None:
        print(f"  说完→最终转写时延  : {result['final_ms']:.0f} ms")
        ok = result["final_ms"] <= 1000 and bool(result["final_text"])
        print(f"  POC-02 判定（≤1s 出转写）: {'✅ 通过' if ok else '❌ 未达标'}")
    print(f"  TTS 首包延迟基线    : {tts_first_ms:.0f} ms")
    return 0 if result["final_text"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
