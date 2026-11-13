"""会话/记忆访问审计表

Revision ID: 0012_conversation_audit
Revises: 0011_conversation_retention
Create Date: 2026-11-13

- conversation_audit_logs：查看会话消息 / 记忆查询 / 管理员治理操作的审计留痕；
  只记元数据（actor/action/target），不记对话正文与记忆正文。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0012_conversation_audit"
down_revision: str | None = "0011_conversation_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_audit_logs",
        sa.Column("audit_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "actor_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_role", sa.String(length=24), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("detail", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_audit_logs_target_created",
        "conversation_audit_logs",
        ["target_type", "target_id", "created_at"],
    )
    op.create_index("ix_audit_logs_created", "conversation_audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created", table_name="conversation_audit_logs")
    op.drop_index("ix_audit_logs_target_created", table_name="conversation_audit_logs")
    op.drop_table("conversation_audit_logs")
