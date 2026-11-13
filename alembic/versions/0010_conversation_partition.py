"""会话分区基础：记忆萃取游标落库 + 落库失败可见表

Revision ID: 0010_conversation_partition
Revises: 0009_media_history
Create Date: 2026-11-13

- conversations.memory_upto_seq：长期记忆萃取游标（DB 为准，Redis 仅会话内缓存，
  避免 24h TTL 过期后旧会话重复萃取）
- conversation_write_failures：对话落库失败可见（不阻断通话主链路的兜底记录）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0010_conversation_partition"
down_revision: str | None = "0009_media_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("memory_upto_seq", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "conversation_write_failures",
        sa.Column("failure_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "thread_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.thread_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=120), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_write_failures_thread_created",
        "conversation_write_failures",
        ["thread_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_write_failures_thread_created", table_name="conversation_write_failures")
    op.drop_table("conversation_write_failures")
    op.drop_column("conversations", "memory_upto_seq")
