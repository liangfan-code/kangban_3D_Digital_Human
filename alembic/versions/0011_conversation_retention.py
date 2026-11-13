"""会话数据治理：保留期到期标记 + 归档表

Revision ID: 0011_conversation_retention
Revises: 0010_conversation_partition
Create Date: 2026-11-13

- conversations.expires_at：到期时间（NULL = 永不清理；治理任务按此列与
  conversation_retention_days 判定到期）
- conversation_archives：会话归档表（原会话行删除后的完整拷贝，含消息正文 JSONB；
  thread_id 不设外键——原行会被物理删除）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0011_conversation_retention"
down_revision: str | None = "0010_conversation_partition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "conversation_archives",
        sa.Column("archive_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # 原会话标识（无外键：归档后原行物理删除）
        sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
        sa.Column("elder_id", UUID(as_uuid=True), nullable=True),
        sa.Column("device_id", UUID(as_uuid=True), nullable=True),
        sa.Column("session_seq", sa.Integer(), nullable=False),
        sa.Column("identity_source", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("messages", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("thread_id", name="uq_conversation_archives_thread"),
    )
    op.create_index(
        "ix_conversation_archives_elder_started",
        "conversation_archives",
        ["elder_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_archives_elder_started", table_name="conversation_archives")
    op.drop_table("conversation_archives")
    op.drop_column("conversations", "expires_at")
