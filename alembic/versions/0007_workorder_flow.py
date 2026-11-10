"""0007_workorder_flow：敏感事件工单闭环（工单扩展 + 处理项 + 审计事件）

Revision ID: 0007_workorder_flow
Revises: 0006_conversation_messages
Create Date: 2026-11-10

设计依据：.trae/specs/create-sensitive-workorder-flow/spec.md。
- workorders 扩展来源/敏感类别/重复次数/关闭人/关闭备注/上下文快照；
- workorder_dispositions：按角色（doctor/nurse）各一条三态处理项；
- workorder_events：created/read/disposition/closed/occurrence 审计时间线。
不回改 0001-0006（ADR-013）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_workorder_flow"
down_revision: Union[str, None] = "0006_conversation_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    # ---- workorders 扩展列（历史行按默认值回填） ----
    op.add_column(
        "workorders",
        sa.Column("source", sa.String(16), nullable=False, server_default="sensitive"),
    )
    op.add_column("workorders", sa.Column("sensitive_category", sa.String(64), nullable=True))
    op.add_column(
        "workorders",
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("workorders", sa.Column("closed_by", UUID(as_uuid=True), nullable=True))
    op.add_column("workorders", sa.Column("closed_note", sa.Text(), nullable=True))
    op.add_column("workorders", sa.Column("context_snapshot", JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_workorders_closed_by_accounts",
        "workorders",
        "accounts",
        ["closed_by"],
        ["account_id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_workorders_source", "workorders", "source IN ('sensitive', 'manual')"
    )

    # ---- 处理项：doctor/nurse 各一条，三态 unread/read/handled ----
    op.create_table(
        "workorder_dispositions",
        sa.Column(
            "disposition_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "workorder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workorders.workorder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="unread"),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_text", sa.Text(), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint("role IN ('doctor', 'nurse')", name="ck_workorder_dispositions_role"),
        sa.CheckConstraint(
            "status IN ('unread', 'read', 'handled')",
            name="ck_workorder_dispositions_status",
        ),
        sa.UniqueConstraint(
            "workorder_id", "role", name="uq_workorder_dispositions_workorder_role"
        ),
    )
    op.create_index(
        "ix_workorder_dispositions_role_status",
        "workorder_dispositions",
        ["role", "status"],
    )

    # ---- 审计事件：只追加不修改 ----
    op.create_table(
        "workorder_events",
        sa.Column(
            "event_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "workorder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workorders.workorder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column(
            "actor_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_role", sa.String(16), nullable=True),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint(
            "event_type IN ('created', 'read', 'disposition', 'closed', 'occurrence')",
            name="ck_workorder_events_type",
        ),
        sa.CheckConstraint(
            "actor_role IS NULL OR actor_role IN ('doctor', 'nurse', 'admin', 'family')",
            name="ck_workorder_events_actor_role",
        ),
    )
    op.create_index(
        "ix_workorder_events_workorder_created",
        "workorder_events",
        ["workorder_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workorder_events_workorder_created", table_name="workorder_events")
    op.drop_table("workorder_events")
    op.drop_index(
        "ix_workorder_dispositions_role_status", table_name="workorder_dispositions"
    )
    op.drop_table("workorder_dispositions")
    op.drop_constraint("ck_workorders_source", "workorders", type_="check")
    op.drop_constraint("fk_workorders_closed_by_accounts", "workorders", type_="foreignkey")
    op.drop_column("workorders", "context_snapshot")
    op.drop_column("workorders", "closed_note")
    op.drop_column("workorders", "closed_by")
    op.drop_column("workorders", "occurrence_count")
    op.drop_column("workorders", "sensitive_category")
    op.drop_column("workorders", "source")
