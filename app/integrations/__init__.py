"""厂商/模型适配器抽象层（TDD §4 职责，Day3 建）。

设计原则（Day3 规划 §2 步骤 5 + 教师样板 providers/__init__.py 移植口径）：
- ASR / TTS / 声纹全部抽象为接口，换厂商只改适配器，业务层零改动；
- 声纹 Provider 只负责「向量提取 + 相似度计算」，不做存储——
  入库（voiceprints 表 / pgvector）与身份判定归 app/services/voice.py（D2 决策，
  不使用教师样板的 speaker.json 文件方案）；
- 全部实现类必须支持「未配置/未安装」场景：依赖缺失抛 ProviderUnavailable，
  由服务层转降级路径，禁止拖垮应用启动（验收项 12：配置缺省可启动）。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


class ProviderUnavailable(Exception):
    """Provider 依赖未配置或未安装（如 FunASR URL 为空、模型目录缺失）。"""


@dataclass
class ASRResult:
    """FunASR 流式识别结果。"""

    text: str
    is_final: bool
    mode: str = ""  # online（2pass 部分结果）/ 2pass-offline（终句）
    confidence: float = 0.0


class ASRProvider(ABC):
    """语音识别 Provider 抽象（流式会话制）。"""

    @abstractmethod
    async def start_session(self) -> None:
        """初始化识别会话（如建立 WSS 连接并发送首帧配置）。"""

    @abstractmethod
    async def feed_audio(self, audio_16k: bytes) -> None:
        """喂入 16kHz mono int16 PCM 音频块。"""

    @abstractmethod
    async def finish(self) -> None:
        """标记音频结束，等待最终结果。"""

    @abstractmethod
    def results(self) -> AsyncIterator[ASRResult]:
        """异步迭代识别结果（部分结果 + 终句结果）。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭会话，释放资源。"""


class TTSProvider(ABC):
    """语音合成 Provider 抽象。"""

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """文本转 16kHz mono int16 PCM 字节流；实现须记录首包延迟（tts_first_byte_ms）。"""

    @abstractmethod
    def sample_rate(self) -> int:
        """输出音频采样率。"""


class VoiceprintProvider(ABC):
    """声纹 Provider 抽象（仅向量提取与比对，不含存储）。"""

    @abstractmethod
    def extract_embedding(self, audio_16k: bytes) -> list[float]:
        """16kHz int16 PCM → 192 维归一化声纹向量（CAMPPlus）。

        不足有效语音时抛 ValueError，由调用方转「语音太短」提示。
        """

    @abstractmethod
    def speech_only(self, audio_16k: bytes) -> bytes:
        """TEN-VAD 过滤静音段，返回纯语音 PCM；无语音抛 ValueError。"""

    @staticmethod
    @abstractmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """归一化向量的余弦相似度（等价点积，防御性重归一化）。"""

    @abstractmethod
    def model_fingerprint(self) -> str:
        """模型文件 SHA-256 指纹；voiceprints.embedding_model 校验用，
        模型更换后旧向量不可直接比对。"""
