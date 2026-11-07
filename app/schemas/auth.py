"""认证相关请求/响应结构。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """登录请求：username 字段可填用户名或手机号（登录备选）。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_in: int  # 秒


class DeviceRegisterRequest(BaseModel):
    """平板注册：设备编码 + 一次性注册凭证。"""

    device_code: str = Field(min_length=1, max_length=80)
    registration_token: str = Field(min_length=1, max_length=256)


class DeviceTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    device_id: UUID
    expires_at: datetime


class UserMeResponse(BaseModel):
    identity_type: str = "user"
    account_id: UUID
    username: str
    display_name: str
    role: str


class DeviceMeResponse(BaseModel):
    identity_type: str = "device"
    device_id: UUID
    device_code: str
    room_id: UUID
    bed_no: int | None = None
    status: str
