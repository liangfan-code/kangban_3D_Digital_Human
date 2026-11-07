"""应用配置：Pydantic Settings 统一加载 .env 与进程环境变量。

Day1 基础配置启动必需；后续能力字段（LLM/ASR/TTS/JWT 等）声明为 Optional，
空占位不影响 Day1 启动，进入对应功能阶段后再做必填校验（ADR-012）。
"""

from functools import lru_cache
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Day1 基础配置 ----
    app_name: str = "康伴 3D 数字人康养陪伴系统"
    app_env: str = "local"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://kangban_dev:CHANGE_ME_LOCAL_ONLY@127.0.0.1:5432/kangban_dev"
    # pytest 专用独立测试库；未配置时测试套件在收集阶段报错并提示创建
    database_url_test: str | None = None
    redis_url: str = "redis://127.0.0.1:6379/0"
    sql_echo: bool = False

    # ---- System 2 大模型（后续 Optional）----
    llm_provider: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 1

    # ---- Embedding（后续 Optional）----
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None

    # ---- System One（后续 Optional）----
    system_one_provider: str | None = None
    system_one_model_path: str | None = None
    system_one_confidence_threshold: float = 0.85
    system_one_timeout_ms: int = 200

    # ---- ASR / TTS（后续 Optional）----
    asr_provider: str | None = None
    asr_api_key: str | None = None
    asr_base_url: str | None = None
    asr_model: str | None = None
    asr_dialect: str | None = None
    tts_provider: str | None = None
    tts_api_key: str | None = None
    tts_base_url: str | None = None
    tts_voice_id: str | None = None
    tts_dialect: str | None = None

    # ---- 方言规范化（后续 Optional）----
    dialect_normalization_enabled: bool = False
    dialect_normalization_model: str | None = None
    dialect_min_confidence: float | None = None

    # ---- 声纹（后续 Optional）----
    voiceprint_model_path: str | None = None
    # Day3：CAMPPlus 余弦相似度阈值（POC-04 提前执行结论基线 0.55，环境变量可调）
    voiceprint_threshold: float = 0.55
    voiceprint_encryption_key: str | None = None

    # ---- Day3 实时语音（全部 Optional，缺省可启动；FunASR/模型未配置时旧接口不受影响）----
    # FunASR 本地 Docker WSS 地址（compose.funasr.yaml 暴露 10096）
    funasr_url: str | None = None
    # 自签名证书跳过校验（仅本地）
    funasr_insecure: bool = False
    # Edge-TTS 朗读音色（适老偏温和女声）
    edge_tts_voice: str = "zh-CN-XiaoxiaoNeural"
    # 声纹验证通过后临时 Token 有效期（秒，仅内存不落库）
    voiceprint_token_ttl: int = 300
    # sherpa-onnx 本地模型目录（CAMPPlus / ten-vad，不入 Git）
    sherpa_model_dir: str | None = None
    # 生物识别演示登录开关（登录页"老人生物识别进入"）：指纹点击即进 = 登录后门，
    # 仅教学演示用；置 0 后前端按钮失效、后端接口 403，恢复严格设备 Token 鉴权
    biometric_demo_enabled: bool = True
    # STUN/TURN JSON 数组字符串，如 [{"urls":"turn:x.x.x.x:3478","username":"u","credential":"p"}]；
    # 空=本地 host 候选。Day1 预留的 WEBRTC_STUN/TURN_* 字段保留，正式部署择一使用
    rtc_ice_servers: str | None = None

    # ---- WebRTC（后续 Optional）----
    webrtc_stun_servers: str | None = None
    webrtc_turn_servers: str | None = None
    webrtc_turn_username: str | None = None
    webrtc_turn_credential: str | None = None

    # ---- JWT / 设备 Token（Day2 启用；密钥为空时签发/注册接口在运行期报错）----
    jwt_secret_key: str | None = None
    jwt_access_token_expire_minutes: int = 60
    device_token_expire_days: int = 90
    device_registration_token_hours: int = 24

    # ---- 数据保留 / 观测（后续 Optional）----
    audio_retention_hours: int = 72
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _empty_str_to_none(cls, data: Any) -> Any:
        """空字符串占位（如 JWT_ACCESS_TOKEN_EXPIRE_MINUTES=）按未配置处理，走字段默认值。"""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}
        return data


@lru_cache
def get_settings() -> Settings:
    """返回缓存的设置实例。"""
    return Settings()
