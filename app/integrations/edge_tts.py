"""Edge-TTS Provider（微软免费合成，无需 API Key）。

参考来源：教师样板 C:/Users/yang/Desktop/asr/src/asr/providers/edge_tts.py，
适配本项目改动：
- MP3 → 16kHz PCM 解码改用 PyAV（aiortc 既有依赖），替代样板的 pydub，
  免去 ffmpeg 外部安装；
- 首包延迟（tts_first_byte_ms）与总耗时在每次合成后写结构化日志，
  供 POC-02 回填与 Day5 延迟基线（大纲课时 3.3 硬要求）；
- 音色来自 Settings.edge_tts_voice，构造即校验 voice 参数合法性。
"""

import io
import time

import av
import numpy as np

from app.core.logging import get_logger
from app.integrations import TTSProvider

logger = get_logger(__name__)

TARGET_RATE = 16000


class EdgeTTSProvider(TTSProvider):
    """Edge-TTS 流式拉取 → PyAV 解码重采样为 16kHz mono int16 PCM。"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%") -> None:
        self.voice = voice
        self.rate = rate
        self.last_first_byte_ms: float | None = None  # 最近一次合成首包延迟
        self.last_total_ms: float | None = None  # 最近一次合成总耗时

    def sample_rate(self) -> int:
        return TARGET_RATE

    async def synthesize(self, text: str) -> bytes:
        import edge_tts  # 延迟导入：未安装时不拖垮模块加载

        t0 = time.perf_counter()
        self.last_first_byte_ms = None
        mp3 = io.BytesIO()
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                if self.last_first_byte_ms is None:
                    self.last_first_byte_ms = (time.perf_counter() - t0) * 1000
                mp3.write(chunk["data"])
        pcm = await self._decode_mp3(mp3.getvalue())
        self.last_total_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "tts_synthesized",
            extra={
                "event": "tts_synthesized",
                "tts_first_byte_ms": round(self.last_first_byte_ms or 0, 1),
                "tts_total_ms": round(self.last_total_ms, 1),
                "pcm_bytes": len(pcm),
                "voice": self.voice,
            },
        )
        return pcm

    async def _decode_mp3(self, mp3_bytes: bytes) -> bytes:
        """MP3 → 16kHz mono s16 PCM（PyAV 重采样，纯内存零临时文件）。"""

        def _decode() -> bytes:
            container = av.open(io.BytesIO(mp3_bytes))
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=TARGET_RATE
            )
            out = io.BytesIO()
            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    out.write(resampled.to_ndarray().astype(np.int16).tobytes())
            return out.getvalue()

        import asyncio

        return await asyncio.to_thread(_decode)
