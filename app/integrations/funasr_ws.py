"""FunASR 流式 ASR Provider（WSS 连本地 Docker 容器，2pass 模式）。

参考来源：教师样板 C:/Users/yang/Desktop/asr/src/asr/providers/asr/funasr_ws.py
（协议参数一致：chunk_size/chunk_interval/look_back），适配本项目改动：
- URL 与证书策略来自 app.core.config.Settings（funasr_url / funasr_insecure）；
- 日志走 app.core.logging.get_logger（结构化 JSON，带 trace_id 上下文）；
- 未配置 funasr_url 时构造即抛 ProviderUnavailable（服务层转文字降级，验收项 12）。
"""

import asyncio
import json
import ssl
import time
from collections.abc import AsyncIterator
from typing import Any

import websockets

from app.core.logging import get_logger
from app.integrations import ASRProvider, ASRResult, ProviderUnavailable

logger = get_logger(__name__)

# finish() 之后等待 offline 结果的死线，绝不能无限等待——否则上层会话永久卡死
# （实测事故）。正常 offline 1.5~2s 回包：
# - 有过任何结果（真语音必有 partial 流）：死线放宽到 10s，容忍长句/CPU 高载解码慢；
# - 零结果（纯噪声/无声段，服务端永不回包）：3s 快速放弃。此前的单一 10s 死线
#   造成「噪声误入 ASR → 白等 10s」的语音黑洞——期间 _processing=True，feed_pcm
#   全部丢弃，用户后续说话丢失（Day5 实测事故）。
_RESULT_WAIT_SECONDS = 10.0
_NO_RESULT_WAIT_SECONDS = 3.0

# 与教师样板一致的 2pass 流式参数；audio_fs=16000 对应前端 60ms 帧
_PROTOCOL_CONFIG: dict[str, Any] = {
    "mode": "2pass",
    "chunk_size": [5, 10, 5],
    "chunk_interval": 10,
    "encoder_chunk_look_back": 4,
    "decoder_chunk_look_back": 1,
    "audio_fs": 16000,
    "wav_format": "pcm",
    "wav_name": "kangban-voice-session",
    "itn": True,
}


class FunASRWSProvider(ASRProvider):
    """FunASR WSS 流式识别（一次会话 = 一个 utterance）。"""

    def __init__(self, url: str | None, insecure: bool = True) -> None:
        if not url:
            raise ProviderUnavailable("FUNASR_URL 未配置，FunASR 语音链路不可用")
        self.url = url
        self.insecure = insecure
        self._ws: Any = None
        self._results: asyncio.Queue[ASRResult] = asyncio.Queue()
        self._closed = False
        self._finished_at: float | None = None  # finish() 时刻，结果等待死线基准
        self._saw_result = False  # 是否收到过任何结果（死线选择依据，见常量注释）
        self._receiver_task: asyncio.Task | None = None

    async def start_session(self) -> None:
        ssl_ctx: ssl.SSLContext | None = None
        if self.url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if self.insecure:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        self._ws = await websockets.connect(
            self.url,
            ssl=ssl_ctx,
            subprotocols=["binary"],
            ping_interval=None,
            max_size=2**20,
        )
        await self._ws.send(json.dumps({**_PROTOCOL_CONFIG, "is_speaking": True}))
        self._receiver_task = asyncio.create_task(self._receive_loop())

    async def feed_audio(self, audio_16k: bytes) -> None:
        if self._ws is None or self._closed:
            return
        await self._ws.send(audio_16k)

    async def finish(self) -> None:
        self._finished_at = time.monotonic()
        if self._ws is not None and not self._closed:
            await self._ws.send(json.dumps({"is_speaking": False, "is_end": True}))

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                data = json.loads(raw)
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                self._saw_result = True
                mode = data.get("mode", "")
                # 本镜像（funasr-runtime-sdk-online-cpu-0.1.13）的 2pass-offline
                # 结果不携带 is_final 字段（POC-02 实测），mode 含 offline 即终句；
                # 标准实现若带 is_final 亦尊重之。
                is_final = bool(data.get("is_final", False)) or "offline" in mode
                result = ASRResult(
                    text=data.get("text", ""),
                    is_final=is_final,
                    mode=mode,
                    confidence=float(data.get("confidence", 0.0)),
                )
                await self._results.put(result)
                if is_final and "offline" in result.mode:
                    break  # 2pass-offline 终句即会话终点
        except Exception as exc:  # noqa: BLE001 —— 接收循环异常统一收敛为会话关闭
            logger.error("funasr_receive_error", extra={"event": "funasr_receive_error", "error": str(exc)})
        finally:
            self._closed = True

    async def results(self) -> AsyncIterator[ASRResult]:
        while not self._closed or not self._results.empty():
            try:
                yield await asyncio.wait_for(self._results.get(), timeout=1.0)
            except TimeoutError:
                # finish() 后超时仍无结果：零结果句（纯噪声/静音，服务端不会回应）
                # 3s 即放弃；有 partial 流的句放宽到 10s（长句解码慢）。到死线必须
                # 放弃本句，绝不能无限循环卡死上层会话（实测事故）。
                if self._finished_at is not None:
                    waited = time.monotonic() - self._finished_at
                    deadline = (
                        _RESULT_WAIT_SECONDS if self._saw_result else _NO_RESULT_WAIT_SECONDS
                    )
                    if waited > deadline:
                        logger.warning(
                            "funasr result timeout",
                            extra={
                                "event": "funasr_result_timeout",
                                "wait_s": round(waited, 1),
                                "saw_result": self._saw_result,
                            },
                        )
                        return
                continue

    async def close(self) -> None:
        self._closed = True
        if self._receiver_task is not None:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
