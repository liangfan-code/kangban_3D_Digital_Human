"""JWT 签发与验签：人 JWT 与设备 Token 共用密钥，但以 token_type 强校验互不通用。

- 人 JWT：短期（默认 60 分钟），纯验签，不查库；payload 含 sub/role/token_type。
- 设备 Token：长期（默认 90 天），payload 含 sub/tid/token_kind；
  本模块只做签名层校验，吊销与过期查库逻辑在 device_token.py。

任何校验失败不返回具体原因差异，由依赖层统一映射为 401/403。
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt

from app.core.config import get_settings

USER_TOKEN_TYPE = "user"
DEVICE_TOKEN_TYPE = "device"
_DEVICE_TOKEN_KIND = "access"
_ALGORITHM = "HS256"


class TokenTypeMismatch(Exception):
    """Token 类型与目标接口不匹配（如人 JWT 调设备接口）；由依赖层映射为 403。"""


def _get_secret() -> str:
    secret = get_settings().jwt_secret_key
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY 未配置：请在 .env 中生成并填入后重启服务")
    return secret


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_user_access_token(account_id: str, role: str) -> str:
    """签发人账号 JWT（60 分钟内有效，纯验签使用）。"""
    settings = get_settings()
    now = _utc_now()
    payload: dict[str, Any] = {
        "sub": account_id,
        "role": role,
        "token_type": USER_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
        "jti": uuid4().hex,
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def create_device_access_token(device_id: str, token_id: str, expires_at: datetime) -> str:
    """签发设备长期 Token；expires_at 以 device_tokens 表记录为准（单一事实来源）。"""
    now = _utc_now()
    payload: dict[str, Any] = {
        "sub": device_id,
        "token_type": DEVICE_TOKEN_TYPE,
        "token_kind": _DEVICE_TOKEN_KIND,
        "tid": token_id,
        "iat": now,
        "exp": expires_at,
        "jti": uuid4().hex,
    }
    return jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    """验签并校验 token_type；签名非法/过期抛 InvalidTokenError，类型不符抛 TokenTypeMismatch。"""
    payload: dict[str, Any] = jwt.decode(token, _get_secret(), algorithms=[_ALGORITHM])
    if payload.get("token_type") != expected_type:
        raise TokenTypeMismatch("token type mismatch")
    return payload
