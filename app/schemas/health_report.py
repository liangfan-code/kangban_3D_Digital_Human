"""健康趋势与日报响应契约（Day8 段3）。"""

import uuid

from pydantic import BaseModel


class HealthTrendPoint(BaseModel):
    date: str
    bp_systolic: int | None = None
    bp_diastolic: int | None = None
    sleep_minutes: int | None = None
    mood: str | None = None


class HealthTrendResponse(BaseModel):
    elder_id: uuid.UUID
    days: int
    has_data: bool
    series: list[HealthTrendPoint]


class HealthDailyReportResponse(BaseModel):
    date: str
    elder_id: uuid.UUID
    elder_name: str
    level: str | None = None
    level_label: str | None = None
    pending_events: int = 0
    missed_medication: int = 0
    text: str
    # 朗读：默认不返回音频；请求 tts=1 且合成失败时 audio 为空并给出降级码
    audio_pcm_base64: str | None = None
    audio_sample_rate: int | None = None
    tts_degraded: str | None = None
