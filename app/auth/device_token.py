"""设备凭证签发与校验。

- register 凭证：随机明文（secrets.token_urlsafe）仅返回一次，库内只存 SHA-256 哈希；
  注册成功即吊销（一次性）。
- access Token：PyJWT（见 jwt.py，payload.tid 指向库内记录）+ 查库校验未吊销未过期；
  校验通过后更新 last_used_at 与 devices.last_seen_at。

明文凭证不落库、不写日志；所有失败统一抛 DeviceCredentialError，由依赖层映射 401。
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import (
    DEVICE_TOKEN_TYPE,
    create_device_access_token,
    decode_token,
)
from app.core.config import get_settings
from app.models import Device, DeviceToken
from app.models.enums import DeviceStatus, DeviceTokenKind


class DeviceCredentialError(Exception):
    """设备凭证无效/过期/已吊销/与设备不匹配；由依赖层统一映射 401。"""


@dataclass(frozen=True)
class IssuedRegisterToken:
    """一次性注册凭证签发结果：明文仅此一次返回。"""

    token: DeviceToken
    plaintext: str


@dataclass(frozen=True)
class IssuedAccessToken:
    """设备长期 Token 签发结果：JWT 明文仅此一次返回。"""

    token: DeviceToken
    access_token: str
    expires_at: datetime


@dataclass(frozen=True)
class DeviceIdentity:
    """设备 Token 校验通过后的设备身份。"""

    device_id: UUID
    token_id: UUID


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def issue_register_token(session: AsyncSession, device_id: UUID) -> IssuedRegisterToken:
    """签发一次性注册凭证（默认 24 小时有效）。调用方负责 commit。"""
    settings = get_settings()
    plaintext = secrets.token_urlsafe(32)
    token = DeviceToken(
        device_id=device_id,
        token_kind=DeviceTokenKind.REGISTER,
        token_hash=_hash_token(plaintext),
        expires_at=_utc_now() + timedelta(hours=settings.device_registration_token_hours),
    )
    session.add(token)
    await session.flush()
    return IssuedRegisterToken(token=token, plaintext=plaintext)


async def consume_register_token(
    session: AsyncSession, register_token: DeviceToken, plaintext: str
) -> IssuedAccessToken:
    """校验注册凭证并换发长期设备 Token：凭证即吊销（一次性），新 access 凭证入库。

    调用方负责 commit。
    """
    now = _utc_now()
    if register_token.token_kind != DeviceTokenKind.REGISTER:
        raise DeviceCredentialError("invalid registration token")
    if register_token.token_hash != _hash_token(plaintext):
        raise DeviceCredentialError("invalid registration token")
    if register_token.revoked_at is not None or register_token.expires_at <= now:
        raise DeviceCredentialError("invalid registration token")

    register_token.revoked_at = now
    return await issue_access_token(session, register_token.device_id)


async def issue_access_token(session: AsyncSession, device_id: UUID) -> IssuedAccessToken:
    """为设备签发长期 access Token：库内存哈希，JWT 明文仅此一次返回。调用方负责 commit。"""
    settings = get_settings()
    expires_at = _utc_now() + timedelta(days=settings.device_token_expire_days)
    placeholder = secrets.token_urlsafe(32)
    token = DeviceToken(
        device_id=device_id,
        token_kind=DeviceTokenKind.ACCESS,
        token_hash=placeholder,
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()  # 取得 token_id 供 JWT tid 引用
    access_token = create_device_access_token(str(device_id), str(token.token_id), expires_at)
    token.token_hash = _hash_token(access_token)  # 以最终 JWT 串的哈希入库（唯一约束）
    await session.flush()
    return IssuedAccessToken(token=token, access_token=access_token, expires_at=expires_at)


async def validate_device_token(session: AsyncSession, token: str) -> DeviceIdentity:
    """设备 Token 校验：验签 + 查库（未吊销未过期、设备存在且 active）+ 更新使用时间。

    该函数内提交 last_used_at/last_seen_at（遥测性质，不参与业务事务）。
    """
    try:
        payload = decode_token(token, DEVICE_TOKEN_TYPE)
    except jwt.InvalidTokenError as exc:
        raise DeviceCredentialError("invalid device token") from exc
    # TokenTypeMismatch（人 Token 调设备接口）不在此捕获：上抛由依赖层映射为 403

    token_id = payload.get("tid")
    device_id = payload.get("sub")
    if not token_id or not device_id:
        raise DeviceCredentialError("invalid device token")

    row = (
        await session.execute(select(DeviceToken).where(DeviceToken.token_id == UUID(token_id)))
    ).scalar_one_or_none()
    now = _utc_now()
    if (
        row is None
        or row.token_kind != DeviceTokenKind.ACCESS
        or row.revoked_at is not None
        or row.expires_at <= now
        or str(row.device_id) != device_id
    ):
        raise DeviceCredentialError("invalid device token")

    device = (
        await session.execute(select(Device).where(Device.device_id == row.device_id))
    ).scalar_one_or_none()
    if device is None or device.status != DeviceStatus.ACTIVE:
        raise DeviceCredentialError("invalid device token")

    row.last_used_at = now
    device.last_seen_at = now
    await session.commit()

    return DeviceIdentity(device_id=row.device_id, token_id=row.token_id)


async def revoke_device_token(session: AsyncSession, token_id: UUID) -> bool:
    """吊销设备凭证（即时生效）；返回是否实际吊销了一条未吊销记录。调用方负责 commit。"""
    result = await session.execute(
        update(DeviceToken)
        .where(DeviceToken.token_id == token_id, DeviceToken.revoked_at.is_(None))
        .values(revoked_at=_utc_now())
    )
    return result.rowcount > 0
