"""CAMPPlus 声纹 Provider（sherpa-onnx 本地 CPU 推理，仅提取与比对）。

参考来源：教师样板 C:/Users/yang/Desktop/asr/src/asr/providers/voiceprint/sherpa_gate.py，
适配本项目改动（D2 决策）：
- 不做任何存储/身份判定（样板用 speaker.json 文件，本项目用 voiceprints 表 +
  pgvector，入库与匹配归 app/services/voice.py）；
- 提供向量提取（192 维归一化）、TEN-VAD 纯语音过滤、余弦相似度三个原子能力；
- 模型目录来自 Settings.sherpa_model_dir，未配置/缺文件抛 ProviderUnavailable；
- 模型文件 SHA-256 指纹暴露给服务层写 voiceprints.embedding_model，
  模型更换后旧向量不可直接比对。
"""

import hashlib
from pathlib import Path

import numpy as np
import sherpa_onnx

from app.core.logging import get_logger
from app.integrations import ProviderUnavailable, VoiceprintProvider

logger = get_logger(__name__)

RATE = 16000
CAMPLUS_MODEL_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
TEN_VAD_MODEL_NAME = "ten-vad.onnx"
EMBEDDING_DIM = 192


def _pcm_to_float(audio_16k: bytes) -> np.ndarray:
    return np.frombuffer(audio_16k, dtype=np.int16).astype(np.float32) / 32768.0


class SherpaVoiceprintProvider(VoiceprintProvider):
    """CAMPPlus 声纹提取 + TEN-VAD 语音段过滤（存储无关）。"""

    def __init__(
        self,
        model_dir: str | Path,
        verify_seconds: float = 2.0,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.verify_seconds = verify_seconds
        campplus_path = self.model_dir / CAMPLUS_MODEL_NAME
        if not campplus_path.is_file():
            raise ProviderUnavailable(f"声纹模型缺失: {campplus_path}")

        self._fingerprint = hashlib.sha256(campplus_path.read_bytes()).hexdigest()
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(campplus_path), num_threads=1
            )
        )

    # ---- 声纹向量 ----

    def extract_embedding(self, audio_16k: bytes) -> list[float]:
        """16kHz int16 PCM → 192 维归一化向量（内部先过 VAD 取纯语音段）。"""
        speech = self.speech_only(audio_16k)
        audio = _pcm_to_float(speech)
        stream = self.extractor.create_stream()
        stream.accept_waveform(RATE, np.ascontiguousarray(audio, dtype=np.float32))
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise ValueError("有效语音不足，无法提取声纹")
        return self._normalize(self.extractor.compute(stream)).tolist()

    # ---- TEN-VAD 语音段过滤 ----

    def speech_only(self, audio_16k: bytes) -> bytes:
        """过滤静音，返回纯语音 PCM（16kHz int16）；无语音抛 ValueError。"""
        vad_path = self.model_dir / TEN_VAD_MODEL_NAME
        if not vad_path.is_file():
            raise ProviderUnavailable(f"VAD 模型缺失: {vad_path}")
        config = sherpa_onnx.VadModelConfig()
        config.ten_vad.model = str(vad_path)
        config.ten_vad.min_silence_duration = 0.8
        config.ten_vad.max_speech_duration = 31.0
        config.sample_rate = RATE
        vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)

        audio = _pcm_to_float(audio_16k)
        segments: list[np.ndarray] = []
        for i in range(0, len(audio), 256):
            vad.accept_waveform(audio[i : i + 256])
            while not vad.empty():
                segments.append(np.array(vad.front.samples))
                vad.pop()
        vad.flush()
        while not vad.empty():
            segments.append(np.array(vad.front.samples))
            vad.pop()
        if not segments:
            raise ValueError("未检测到语音")
        speech = np.concatenate(segments)
        if len(speech) < self.verify_seconds * RATE:
            raise ValueError(f"有效语音短于 {self.verify_seconds} 秒，请多说几秒")
        return (speech * 32768.0).astype(np.int16).tobytes()

    # ---- 相似度 ----

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        va = SherpaVoiceprintProvider._normalize_list(a)
        vb = SherpaVoiceprintProvider._normalize_list(b)
        return float(np.dot(va, vb))

    # ---- 模型指纹 ----

    def model_fingerprint(self) -> str:
        return self._fingerprint

    # ---- 内部工具 ----

    @staticmethod
    def _normalize(vector: np.ndarray) -> np.ndarray:
        vector = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = np.linalg.norm(vector)
        if not np.isfinite(vector).all() or norm < 1e-8:
            raise ValueError("声纹向量非法")
        return vector / norm

    @staticmethod
    def _normalize_list(vector: list[float]) -> np.ndarray:
        return SherpaVoiceprintProvider._normalize(np.array(vector, dtype=np.float32))
