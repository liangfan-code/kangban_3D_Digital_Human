"""0009_media_history：娱乐点播历史（开始/结束/完成度）

Revision ID: 0009_media_history
Revises: 0008_medication_order_flow
Create Date: 2026-11-12

为什么建这张表：点播一旦真实播放，就需要能回答「谁在什么时候听了什么、
听完了没有」——`media_library` 只是目录，`conversation_messages` 只有文本，
都无法承载播放态；因此单独建 `media_history` 记录开始/结束与完成度，
供「换一段」时旧记录补结束、以及后续按长者统计使用。

设计依据：docs/Day7-改造方案.md §7.3、docs/Day7-改造提示词.md 段 6。
不回改 0001-0008（ADR-013）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_media_history"
down_revision: Union[str, None] = "0008_medication_order_flow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "media_history",
        sa.Column(
            "history_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
        ),
        sa.Column(
            "elder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="CASCADE", name="fk_media_history_elder"),
            nullable=False,
        ),
        sa.Column(
            "media_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "media_library.media_id",
                ondelete="CASCADE",
                name="fk_media_history_media",
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_TS_NOW,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_rate", sa.Numeric(4, 3), nullable=True),
        sa.CheckConstraint(
            "completion_rate IS NULL OR (completion_rate >= 0 AND completion_rate <= 1)",
            name="ck_media_history_completion_rate",
        ),
    )
    op.create_index(
        "ix_media_history_elder_started",
        "media_history",
        ["elder_id", sa.text("started_at DESC")],
    )
    op.create_index("ix_media_history_media", "media_history", ["media_id"])


def downgrade() -> None:
    op.drop_index("ix_media_history_media", table_name="media_history")
    op.drop_index("ix_media_history_elder_started", table_name="media_history")
    op.drop_table("media_history")
