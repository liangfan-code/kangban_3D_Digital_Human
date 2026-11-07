"""长者档案请求/响应结构。"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ElderStatus, Gender


class ElderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    birth_date: date | None = None
    gender: Gender | None = None
    persona_id: uuid.UUID
    room_id: uuid.UUID | None = None
    bed_no: int | None = Field(default=None, ge=1, le=2)


class ElderUpdate(BaseModel):
    """PUT 局部更新：room_id 与 bed_no 必须成对出现（同时提供或同时省略）。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    birth_date: date | None = None
    gender: Gender | None = None
    persona_id: uuid.UUID | None = None
    room_id: uuid.UUID | None = None
    bed_no: int | None = Field(default=None, ge=1, le=2)
    status: ElderStatus | None = None

    @model_validator(mode="after")
    def _room_bed_paired(self) -> "ElderUpdate":
        if (self.room_id is None) != (self.bed_no is None):
            raise ValueError("room_id 与 bed_no 必须成对提供或同时省略")
        return self


class ElderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    elder_id: uuid.UUID
    name: str
    birth_date: date | None
    gender: str | None
    persona_id: uuid.UUID
    room_id: uuid.UUID | None
    bed_no: int | None
    status: str
    created_at: datetime
    updated_at: datetime
