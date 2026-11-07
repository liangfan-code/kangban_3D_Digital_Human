"""账号管理请求/响应结构（仅 admin；响应绝不包含 password_hash）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import AccountRole


class AccountCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=100)
    role: AccountRole
    phone: str | None = Field(default=None, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class AccountUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    role: AccountRole | None = None


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    username: str
    phone: str | None
    display_name: str
    role: str
    status: str
    created_at: datetime
    updated_at: datetime


class ResetPasswordResponse(BaseModel):
    """重置密码：新明文密码仅此一次返回，库内只存哈希。"""

    account_id: uuid.UUID
    new_password: str
