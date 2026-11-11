"""0008_medication_order_flow：用药计划闭环 + 服药任务双表 + 按账号已读

Revision ID: 0008_medication_order_flow
Revises: 0007_workorder_flow
Create Date: 2026-11-11

为什么建双表：`workorders` 表达的是"派单"语义，无法表达每个时段的服药任务；
LangGraph 状态与运行日志不可查询，家属服药历史缺少落点。因此拆为：
- `medication_orders`：每个计划每个时段一条任务单，只存当前态；
- `medication_events`：追加型服药事实，只 INSERT，支持家属历史与审计；
- `workorder_reads`：按账号已读回执（本迁移只建表，写入在后续段）。

设计依据：docs/Day7-改造方案.md §4.2、docs/Day7-改造提示词.md 段 1。
不回改 0001-0007（ADR-013）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_medication_order_flow"
down_revision: Union[str, None] = "0007_workorder_flow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")


def upgrade() -> None:
    # ---- 1. medication_plans 增加作者/确认/工单关联（全部可空） ----
    op.add_column(
        "medication_plans",
        sa.Column("created_by_account_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "medication_plans",
        sa.Column("updated_by_account_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "medication_plans",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "medication_plans",
        sa.Column("confirmed_by_account_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "medication_plans",
        sa.Column("workorder_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column("medication_plans", sa.Column("internal_note", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_medication_plans_created_by_accounts",
        "medication_plans",
        "accounts",
        ["created_by_account_id"],
        ["account_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_medication_plans_updated_by_accounts",
        "medication_plans",
        "accounts",
        ["updated_by_account_id"],
        ["account_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_medication_plans_confirmed_by_accounts",
        "medication_plans",
        "accounts",
        ["confirmed_by_account_id"],
        ["account_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_medication_plans_workorder",
        "medication_plans",
        "workorders",
        ["workorder_id"],
        ["workorder_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_medication_plans_workorder", "medication_plans", ["workorder_id"]
    )

    # ---- 2. medication_orders：每个计划每个时段一条任务单，只存当前态 ----
    op.create_table(
        "medication_orders",
        sa.Column(
            "order_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("medication_plans.plan_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "elder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_source", sa.String(16), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'calling', 'confirmed', 'missed', 'skipped', 'cancelled')",
            name="ck_medication_orders_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_medication_orders_attempt_count"
        ),
        sa.CheckConstraint(
            "confirmed_source IS NULL OR confirmed_source IN ('voice', 'text')",
            name="ck_medication_orders_confirmed_source",
        ),
        sa.UniqueConstraint(
            "plan_id", "scheduled_at", name="uq_medication_orders_plan_scheduled"
        ),
    )
    op.create_index(
        "ix_medication_orders_elder_scheduled",
        "medication_orders",
        ["elder_id", sa.text("scheduled_at DESC")],
    )
    op.create_index(
        "ix_medication_orders_status_scheduled",
        "medication_orders",
        ["status", "scheduled_at"],
    )

    # ---- 3. medication_events：追加型事实，只 INSERT ----
    op.create_table(
        "medication_events",
        sa.Column(
            "event_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("medication_orders.order_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            UUID(as_uuid=True),
            sa.ForeignKey("medication_plans.plan_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "elder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "actor_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_role", sa.String(16), nullable=True),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("dedupe_key", sa.String(160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "event_type IN ('created', 'reminded', 'confirmed', 'denied', 'timeout', "
            "'missed', 'skipped', 'cancelled')",
            name="ck_medication_events_type",
        ),
        sa.CheckConstraint(
            "source IN ('scheduler', 'voice', 'text', 'staff', 'family', 'system')",
            name="ck_medication_events_source",
        ),
        sa.CheckConstraint(
            "actor_role IS NULL OR actor_role IN ('doctor', 'nurse', 'admin', 'family')",
            name="ck_medication_events_actor_role",
        ),
    )
    op.create_index(
        "uq_medication_events_dedupe_key",
        "medication_events",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.create_index(
        "ix_medication_events_order_created",
        "medication_events",
        ["order_id", "created_at"],
    )
    op.create_index(
        "ix_medication_events_plan_created",
        "medication_events",
        ["plan_id", "created_at"],
    )

    # ---- 4. workorders 增加派单类型与用药关联 ----
    op.add_column(
        "workorders",
        sa.Column(
            "order_kind", sa.String(16), nullable=False, server_default="one_off"
        ),
    )
    op.add_column(
        "workorders",
        sa.Column("medication_plan_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "workorders",
        sa.Column("medication_order_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column("workorders", sa.Column("effective_from", sa.Date(), nullable=True))
    op.add_column("workorders", sa.Column("effective_until", sa.Date(), nullable=True))
    op.add_column("workorders", sa.Column("dedupe_key", sa.String(160), nullable=True))
    op.create_foreign_key(
        "fk_workorders_medication_plan",
        "workorders",
        "medication_plans",
        ["medication_plan_id"],
        ["plan_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_workorders_medication_order",
        "workorders",
        "medication_orders",
        ["medication_order_id"],
        ["order_id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_workorders_order_kind", "workorders", "order_kind IN ('one_off', 'standing')"
    )
    op.create_index(
        "uq_workorders_dedupe_key",
        "workorders",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.create_index(
        "ix_workorders_medication_plan", "workorders", ["medication_plan_id"]
    )

    # ---- 5. workorder_reads：按账号已读回执 ----
    op.create_table(
        "workorder_reads",
        sa.Column(
            "workorder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workorders.workorder_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW
        ),
    )

    # ---- 6. 重建 workorder_events 事件类型（只扩 CHECK，不动数据） ----
    op.drop_constraint("ck_workorder_events_type", "workorder_events", type_="check")
    op.create_check_constraint(
        "ck_workorder_events_type",
        "workorder_events",
        "event_type IN ('created', 'read', 'disposition', 'closed', 'occurrence', "
        "'confirmed', 'revised')",
    )


def downgrade() -> None:
    # 新增事件类型无法映射回旧 CHECK，先移除本迁移才可能产生的两类事件
    op.execute(
        "DELETE FROM workorder_events WHERE event_type IN ('confirmed', 'revised')"
    )
    op.drop_constraint("ck_workorder_events_type", "workorder_events", type_="check")
    op.create_check_constraint(
        "ck_workorder_events_type",
        "workorder_events",
        "event_type IN ('created', 'read', 'disposition', 'closed', 'occurrence')",
    )

    op.drop_table("workorder_reads")
    op.drop_index("ix_workorders_medication_plan", table_name="workorders")
    op.drop_index("uq_workorders_dedupe_key", table_name="workorders")
    op.drop_constraint("ck_workorders_order_kind", "workorders", type_="check")
    op.drop_constraint(
        "fk_workorders_medication_order", "workorders", type_="foreignkey"
    )
    op.drop_constraint("fk_workorders_medication_plan", "workorders", type_="foreignkey")
    op.drop_column("workorders", "dedupe_key")
    op.drop_column("workorders", "effective_until")
    op.drop_column("workorders", "effective_from")
    op.drop_column("workorders", "medication_order_id")
    op.drop_column("workorders", "medication_plan_id")
    op.drop_column("workorders", "order_kind")

    op.drop_index("ix_medication_events_plan_created", table_name="medication_events")
    op.drop_index("ix_medication_events_order_created", table_name="medication_events")
    op.drop_index("uq_medication_events_dedupe_key", table_name="medication_events")
    op.drop_table("medication_events")

    op.drop_index("ix_medication_orders_status_scheduled", table_name="medication_orders")
    op.drop_index("ix_medication_orders_elder_scheduled", table_name="medication_orders")
    op.drop_table("medication_orders")

    op.drop_index("ix_medication_plans_workorder", table_name="medication_plans")
    op.drop_constraint("fk_medication_plans_workorder", "medication_plans", type_="foreignkey")
    op.drop_constraint(
        "fk_medication_plans_confirmed_by_accounts",
        "medication_plans",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_medication_plans_updated_by_accounts", "medication_plans", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_medication_plans_created_by_accounts", "medication_plans", type_="foreignkey"
    )
    op.drop_column("medication_plans", "internal_note")
    op.drop_column("medication_plans", "workorder_id")
    op.drop_column("medication_plans", "confirmed_by_account_id")
    op.drop_column("medication_plans", "confirmed_at")
    op.drop_column("medication_plans", "updated_by_account_id")
    op.drop_column("medication_plans", "created_by_account_id")
