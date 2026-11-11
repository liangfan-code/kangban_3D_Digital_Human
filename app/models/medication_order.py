"""medication_orders / medication_events ORM。

- medication_orders：每个用药计划每个时段一条服药任务单，只存当前态；
- medication_events：追加型服药事实，只 INSERT，服务家属服药历史与审计。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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
    AccountRole,
    MedicationEventSource,
    MedicationEventType,
    MedicationOrderStatus,
)


class MedicationOrder(TimestampMixin, Base):
    """服药任务单：幂等边界为 (plan_id, scheduled_at)。"""

    __tablename__ = "medication_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}', '{3}', '{4}', '{5}')".format(
                MedicationOrderStatus.PENDING,
                MedicationOrderStatus.CALLING,
                MedicationOrderStatus.CONFIRMED,
                MedicationOrderStatus.MISSED,
                MedicationOrderStatus.SKIPPED,
                MedicationOrderStatus.CANCELLED,
            ),
            name="ck_medication_orders_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_medication_orders_attempt_count"),
        CheckConstraint(
            "confirmed_source IS NULL OR confirmed_source IN ('voice', 'text')",
            name="ck_medication_orders_confirmed_source",
        ),
        UniqueConstraint("plan_id", "scheduled_at", name="uq_medication_orders_plan_scheduled"),
        Index(
            "ix_medication_orders_elder_scheduled",
            "elder_id",
            text("scheduled_at DESC"),
        ),
        Index("ix_medication_orders_status_scheduled", "status", "scheduled_at"),
    )

    order_id: Mapped[uuid.UUID] = pk_uuid()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medication_plans.plan_id", ondelete="CASCADE"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="CASCADE"), nullable=False
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MedicationOrderStatus.PENDING,
        server_default=MedicationOrderStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MedicationEvent(Base):
    """服药事实事件：只 INSERT；dedupe_key 由部分唯一索引兜底。"""

    __tablename__ = "medication_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('{0}', '{1}', '{2}', '{3}', '{4}', '{5}', '{6}', '{7}')".format(
                MedicationEventType.CREATED,
                MedicationEventType.REMINDED,
                MedicationEventType.CONFIRMED,
                MedicationEventType.DENIED,
                MedicationEventType.TIMEOUT,
                MedicationEventType.MISSED,
                MedicationEventType.SKIPPED,
                MedicationEventType.CANCELLED,
            ),
            name="ck_medication_events_type",
        ),
        CheckConstraint(
            "source IN ('{0}', '{1}', '{2}', '{3}', '{4}', '{5}')".format(
                MedicationEventSource.SCHEDULER,
                MedicationEventSource.VOICE,
                MedicationEventSource.TEXT,
                MedicationEventSource.STAFF,
                MedicationEventSource.FAMILY,
                MedicationEventSource.SYSTEM,
            ),
            name="ck_medication_events_source",
        ),
        CheckConstraint(
            "actor_role IS NULL OR actor_role IN ('{0}', '{1}', '{2}', '{3}')".format(
                AccountRole.DOCTOR,
                AccountRole.NURSE,
                AccountRole.ADMIN,
                AccountRole.FAMILY,
            ),
            name="ck_medication_events_actor_role",
        ),
        Index(
            "uq_medication_events_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
        Index("ix_medication_events_order_created", "order_id", "created_at"),
        Index("ix_medication_events_plan_created", "plan_id", "created_at"),
    )

    event_id: Mapped[uuid.UUID] = pk_uuid()
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medication_orders.order_id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medication_plans.plan_id", ondelete="CASCADE"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 只放元数据：attempt / channel / reason_code / workorder_id
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
