"""健康事件请求/响应契约（Day8 段1）。

``elder_id`` 只出现在路径参数，由服务端鉴权；正文不接受 elder_id。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthEventCreate(BaseModel):
    source: Literal["nightly", "voice", "manual"] = "manual"
    bp_systolic: int | None = Field(default=None, ge=50, le=250)
    bp_diastolic: int | None = Field(default=None, ge=30, le=150)
    sleep_minutes: int | None = Field(default=None, ge=0, le=1440)
    mood: str | None = Field(default=None, max_length=24)
    chief_complaint: str | None = Field(default=None, max_length=500)


class HealthEventResponse(BaseModel):
    event_id: uuid.UUID
    thread_id: str
    elder_id: uuid.UUID
    level: str
    status: str
    reply: str
    required_role: str | None
    missing_fields: list[str]


class HealthEventPendingItem(BaseModel):
    event_id: uuid.UUID
    elder_id: uuid.UUID
    elder_name: str
    room_label: str | None
    level: str
    status: str
    summary: str
    required_role: str | None
    created_at: datetime | None


class HealthEventPendingResponse(BaseModel):
    items: list[HealthEventPendingItem]
    total: int
    page: int
    page_size: int


class HealthEventDecisionRequest(BaseModel):
    action: Literal["confirm", "escalate", "reject"]
    note: str | None = Field(default=None, max_length=500)


class HealthEventDecisionResponse(BaseModel):
    event_id: uuid.UUID
    status: str
    level: str
    workorder_id: uuid.UUID | None
    referral_id: str | None


class HealthEventDetailResponse(BaseModel):
    event_id: uuid.UUID
    thread_id: str
    elder_id: uuid.UUID
    elder_name: str | None
    room_label: str | None
    level: str
    status: str
    bp_systolic: int | None
    bp_diastolic: int | None
    sleep_minutes: int | None
    mood: str | None
    chief_complaint: str | None
    reasons: list[str]
    required_role: str | None
    workorder_id: uuid.UUID | None
    referral_id: str | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime | None
