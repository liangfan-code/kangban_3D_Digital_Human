"""房间管理请求/响应结构（仅 admin）。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RoomStatus


class RoomCreate(BaseModel):
    building_code: str = Field(min_length=1, max_length=32)
    floor: int = Field(ge=0, le=127)
    room_number: str = Field(min_length=1, max_length=32)
    bed_count: int = Field(ge=1, le=2)


class RoomUpdate(BaseModel):
    building_code: str | None = Field(default=None, min_length=1, max_length=32)
    floor: int | None = Field(default=None, ge=0, le=127)
    room_number: str | None = Field(default=None, min_length=1, max_length=32)
    bed_count: int | None = Field(default=None, ge=1, le=2)
    status: RoomStatus | None = None


class RoomResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    room_id: UUID
    building_code: str
    floor: int
    room_number: str
    bed_count: int
    status: str
    created_at: datetime
    updated_at: datetime
