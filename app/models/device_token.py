"""device_tokens ORM：设备凭证，只存 SHA-256 哈希，不存任何明文 Token。

register = 一次性注册凭证（使用后立即置 revoked_at）；access = 长期设备 Token（可吊销，即时生效）。
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid
from app.models.enums import DeviceTokenKind


class DeviceToken(Base):
    __tablename__ = "device_tokens"
    __table_args__ = (
        CheckConstraint(
            "token_kind IN ('{0}', '{1}')".format(
                DeviceTokenKind.REGISTER, DeviceTokenKind.ACCESS
            ),
            name="ck_device_tokens_kind",
        ),
        Index("ix_device_tokens_device_kind_revoked", "device_id", "token_kind", "revoked_at"),
    )

    token_id: Mapped[uuid.UUID] = pk_uuid()
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.device_id", ondelete="RESTRICT"), nullable=False
    )
    token_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DeviceTokenKind.REGISTER,
        server_default=DeviceTokenKind.REGISTER,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
