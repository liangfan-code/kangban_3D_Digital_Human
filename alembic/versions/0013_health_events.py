"""健康采集事件表

Revision ID: 0013_health_events
Revises: 0012_conversation_audit
Create Date: 2026-11-13

- health_events：一天一位长者一条健康采集事件，承载三级分级与人工确认状态；
- thread_id 唯一（health:{elder_id}:{YYYY-MM-DD}），同日重入只更新不新建；
- 只新增表与索引，不 ALTER 既有表。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0013_health_events"
down_revision: str | None = "0012_conversation_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "health_events",
        sa.Column(
            "event_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("thread_id", sa.String(length=96), nullable=False),
        sa.Column(
            "elder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "level", sa.String(length=16), nullable=False, server_default="green"
        ),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="collecting"
        ),
        sa.Column("bp_systolic", sa.SmallInteger(), nullable=True),
        sa.Column("bp_diastolic", sa.SmallInteger(), nullable=True),
        sa.Column("sleep_minutes", sa.Integer(), nullable=True),
        sa.Column("mood", sa.String(length=24), nullable=True),
        sa.Column("chief_complaint", sa.Text(), nullable=True),
        sa.Column("reasons", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "workorder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workorders.workorder_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("referral_id", sa.String(length=64), nullable=True),
        sa.Column(
            "decided_by",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ux_health_events_thread", "health_events", ["thread_id"], unique=True
    )
    op.create_index(
        "ix_health_events_elder_created",
        "health_events",
        ["elder_id", "created_at"],
    )
    op.create_index(
        "ix_health_events_status_created",
        "health_events",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_health_events_status_created", table_name="health_events")
    op.drop_index("ix_health_events_elder_created", table_name="health_events")
    op.drop_index("ux_health_events_thread", table_name="health_events")
    op.drop_table("health_events")
