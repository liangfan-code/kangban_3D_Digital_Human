"""accounts ORM：人账号（护工/医生/家属/管理员统一账号表）。

角色语义差异由三张授权关联表表达；密码只存 bcrypt 哈希，明文不落任何位置。
"""

import uuid

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import AccountRole, AccountStatus


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "role IN ('{0}', '{1}', '{2}', '{3}')".format(
                AccountRole.NURSE, AccountRole.DOCTOR, AccountRole.FAMILY, AccountRole.ADMIN
            ),
            name="ck_accounts_role",
        ),
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}')".format(
                AccountStatus.ACTIVE, AccountStatus.DISABLED, AccountStatus.LOCKED
            ),
            name="ck_accounts_status",
        ),
        UniqueConstraint("username", name="uq_accounts_username"),
        UniqueConstraint("phone", name="uq_accounts_phone"),
        Index("ix_accounts_role_status", "role", "status"),
    )

    account_id: Mapped[uuid.UUID] = pk_uuid()
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AccountRole.NURSE, server_default=AccountRole.NURSE
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AccountStatus.ACTIVE,
        server_default=AccountStatus.ACTIVE,
    )
