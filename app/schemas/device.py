"""设备绑定与自查请求/响应结构。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DeviceStatus


class BindingUpdate(BaseModel):
    """设备绑定：房间必填，bed_no 省略表示房间级设备。"""

    room_id: uuid.UUID
    bed_no: int | None = Field(default=None, ge=1, le=2)


class DeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: uuid.UUID
    device_code: str
    room_id: uuid.UUID | None
    bed_no: int | None
    status: str


class RegistrationTokenResponse(BaseModel):
    """一次性注册凭证：明文仅此一次返回。"""

    device_id: uuid.UUID
    registration_token: str
    expires_at: datetime


class DeviceCreate(BaseModel):
    """建设备：room_id 必填（devices.room_id 非空），初始状态 pending。"""

    device_code: str = Field(min_length=1, max_length=80)
    room_id: uuid.UUID
    bed_no: int | None = Field(default=None, ge=1, le=2)


class DeviceUpdate(BaseModel):
    """更新设备：device_code 不可改；retired 仅经 DELETE 软删设置（含 Token 吊销）。"""

    room_id: uuid.UUID | None = None
    bed_no: int | None = Field(default=None, ge=1, le=2)
    status: DeviceStatus | None = None


class DeviceTokenItem(BaseModel):
    """设备凭证列表项：绝不包含明文或哈希。"""

    model_config = ConfigDict(from_attributes=True)

    token_id: uuid.UUID
    token_kind: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class ElderCandidate(BaseModel):
    elder_id: uuid.UUID
    name: str
    bed_no: int


class DeviceSelfResponse(BaseModel):
    """devices/me 自查：单人间返回 elder（锁定），双人间返回 candidates。"""

    device_id: uuid.UUID
    device_code: str
    room_id: uuid.UUID
    building_code: str
    room_number: str
    bed_count: int
    mode: str  # single / double
    elder: ElderCandidate | None = None
    candidates: list[ElderCandidate] = Field(default_factory=list)
