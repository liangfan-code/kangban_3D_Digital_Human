"""0006_conversation_messages：对话逐轮消息表

Revision ID: 0006_conversation_messages
Revises: 0005_health_profile
Create Date: 2026-11-06

设计依据：《Day_plan/Day4_3D康伴数字人.md》§4.6 与《docs/ER_数据模型设计.md》conversation_messages。
- 每轮对话两条消息：user（终句转写）/ assistant（剥净动作标记的回复正文）。
- (thread_id, created_at) 复合索引支撑「最近 N 轮」读取；content 只存正文，不存音频。
不回改 0001-0005（ADR-013）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_conversation_messages"
down_revision: Union[str, None] = "0005_health_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    # 全局单调序列：同事务写入的多条消息 created_at 相同，需确定性排序
    # （同轮 user 必须排在 assistant 之前，LLM 历史顺序才正确）
    op.execute(
        "CREATE SEQUENCE conversation_messages_seq AS bigint START WITH 1 INCREMENT BY 1"
    )
    op.create_table(
        "conversation_messages",
        sa.Column(
            "message_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.thread_id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('conversation_messages_seq')"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
    )
    op.create_index(
        "ix_conversation_messages_thread_created",
        "conversation_messages",
        ["thread_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_messages_thread_created", table_name="conversation_messages"
    )
    op.drop_table("conversation_messages")
    op.execute("DROP SEQUENCE IF EXISTS conversation_messages_seq")
