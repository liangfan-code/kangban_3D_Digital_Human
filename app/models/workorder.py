"""workorders ORM：护工工单，必属一个 elder。

无 staff 表，assignee_ref 暂存外部引用，不伪造外键。
Day8：敏感事件工单闭环——工单扩展来源/敏感类别/重复次数/关闭信息/上下文快照，
新增处理项（doctor/nurse 三态）与审计事件表。
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import (
    WorkorderDispositionStatus,
    WorkorderOrderKind,
    WorkorderPriority,
    WorkorderSource,
    WorkorderStatus,
)


class Workorder(TimestampMixin, Base):
    __tablename__ = "workorders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}', '{3}', '{4}')".format(
                WorkorderStatus.OPEN,
                WorkorderStatus.ACKNOWLEDGED,
                WorkorderStatus.IN_PROGRESS,
                WorkorderStatus.RESOLVED,
                WorkorderStatus.CANCELLED,
            ),
            name="ck_workorders_status",
        ),
        CheckConstraint(
            "priority IN ('{0}', '{1}', '{2}', '{3}')".format(
                WorkorderPriority.LOW,
                WorkorderPriority.NORMAL,
                WorkorderPriority.HIGH,
                WorkorderPriority.URGENT,
            ),
            name="ck_workorders_priority",
        ),
        CheckConstraint(
            "source IN ('{0}', '{1}')".format(
                WorkorderSource.SENSITIVE, WorkorderSource.MANUAL
            ),
            name="ck_workorders_source",
        ),
        CheckConstraint(
            "order_kind IN ('{0}', '{1}')".format(
                WorkorderOrderKind.ONE_OFF, WorkorderOrderKind.STANDING
            ),
            name="ck_workorders_order_kind",
        ),
        Index("ix_workorders_status_created_at", "status", "created_at"),
        Index("ix_workorders_elder_status", "elder_id", "status"),
        Index("ix_workorders_medication_plan", "medication_plan_id"),
        Index(
            "uq_workorders_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    workorder_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=WorkorderSource.SENSITIVE,
        server_default=WorkorderSource.SENSITIVE,
    )
    sensitive_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=WorkorderStatus.OPEN,
        server_default=WorkorderStatus.OPEN,
    )
    priority: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=WorkorderPriority.NORMAL,
        server_default=WorkorderPriority.NORMAL,
    )
    assignee_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 触发前最近 5 轮对话 + 触发句 + 首条安全回复；不含音频、不存整段会话
    context_snapshot: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    closed_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=WorkorderOrderKind.ONE_OFF,
        server_default=WorkorderOrderKind.ONE_OFF,
    )
    medication_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("medication_plans.plan_id", ondelete="SET NULL"), nullable=True
    )
    medication_order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("medication_orders.order_id", ondelete="SET NULL"), nullable=True
    )
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 漏服工单幂等键：missed:{order_id}
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)


class WorkorderRead(Base):
    """按账号已读回执（联合主键）；已读按账号、处理仍按角色。"""

    __tablename__ = "workorder_reads"

    workorder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workorders.workorder_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkorderDisposition(TimestampMixin, Base):
    """工单处理项：按角色（doctor/nurse）各一条，记录三态、方案与操作人。"""

    __tablename__ = "workorder_dispositions"
    __table_args__ = (
        CheckConstraint(
            "role IN ('doctor', 'nurse')",
            name="ck_workorder_dispositions_role",
        ),
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}')".format(
                WorkorderDispositionStatus.UNREAD,
                WorkorderDispositionStatus.READ,
                WorkorderDispositionStatus.HANDLED,
            ),
            name="ck_workorder_dispositions_status",
        ),
        UniqueConstraint("workorder_id", "role", name="uq_workorder_dispositions_workorder_role"),
        Index("ix_workorder_dispositions_role_status", "role", "status"),
    )

    disposition_id: Mapped[uuid.UUID] = pk_uuid()
    workorder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workorders.workorder_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=WorkorderDispositionStatus.UNREAD,
        server_default=WorkorderDispositionStatus.UNREAD,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkorderEvent(Base):
    """工单审计事件：created/read/disposition/closed/occurrence/confirmed/revised，只追加不修改。"""

    __tablename__ = "workorder_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'read', 'disposition', 'closed', 'occurrence', "
            "'confirmed', 'revised')",
            name="ck_workorder_events_type",
        ),
        CheckConstraint(
            "actor_role IS NULL OR actor_role IN ('doctor', 'nurse', 'admin', 'family')",
            name="ck_workorder_events_actor_role",
        ),
        Index("ix_workorder_events_workorder_created", "workorder_id", "created_at"),
    )

    event_id: Mapped[uuid.UUID] = pk_uuid()
    workorder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workorders.workorder_id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
