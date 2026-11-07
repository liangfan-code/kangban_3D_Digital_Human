"""数字人人设管理请求/响应结构（仅 admin）。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PersonaCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=500)
    personality: str | None = None
    greeting: str | None = None
    voice_id: str | None = Field(default=None, max_length=100)
    is_active: bool = True


class PersonaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=500)
    personality: str | None = None
    greeting: str | None = None
    voice_id: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None


class PersonaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    persona_id: UUID
    code: str
    name: str
    avatar_url: str | None
    personality: str | None
    greeting: str | None
    voice_id: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
