"""0003_accounts_device_tokens：人账号表与设备凭证表

Revision ID: 0003_accounts_device_tokens
Revises: 0002_domain_tables
Create Date: 2026-11-04

设计依据：《Day_plan/Day2_3D康伴数字人.md》第 5.1/5.2 节。
- accounts：四类角色统一账号表，密码只存 bcrypt 哈希。
- device_tokens：设备凭证，只存 SHA-256 哈希；register 一次性、access 可吊销。
不回改 0001/0002（ADR-013）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_accounts_device_tokens"
down_revision: Union[str, None] = "0002_domain_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="nurse"),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint(
            "role IN ('nurse', 'doctor', 'family', 'admin')",
            name="ck_accounts_role",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'locked')",
            name="ck_accounts_status",
        ),
        sa.UniqueConstraint("username", name="uq_accounts_username"),
        sa.UniqueConstraint("phone", name="uq_accounts_phone"),
    )
    op.create_index("ix_accounts_role_status", "accounts", ["role", "status"])

    op.create_table(
        "device_tokens",
        sa.Column(
            "token_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "device_id",
            UUID(as_uuid=True),
            sa.ForeignKey("devices.device_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("token_kind", sa.String(16), nullable=False, server_default="register"),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_kind IN ('register', 'access')",
            name="ck_device_tokens_kind",
        ),
        sa.UniqueConstraint("token_hash", name="uq_device_tokens_token_hash"),
    )
    op.create_index(
        "ix_device_tokens_device_kind_revoked",
        "device_tokens",
        ["device_id", "token_kind", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_tokens_device_kind_revoked", table_name="device_tokens")
    op.drop_table("device_tokens")
    op.drop_index("ix_accounts_role_status", table_name="accounts")
    op.drop_table("accounts")
