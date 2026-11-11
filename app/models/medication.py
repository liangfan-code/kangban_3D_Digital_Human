"""medication_plans ORM：用药计划，必属一个 elder。"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import MedicationPlanStatus


class MedicationPlan(TimestampMixin, Base):
    __tablename__ = "medication_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}', '{3}', '{4}')".format(
                MedicationPlanStatus.PLANNED,
                MedicationPlanStatus.ACTIVE,
                MedicationPlanStatus.PAUSED,
                MedicationPlanStatus.COMPLETED,
                MedicationPlanStatus.CANCELLED,
            ),
            name="ck_medication_plans_status",
        ),
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_medication_plans_date_range",
        ),
        Index("ix_medication_plans_elder_status", "elder_id", "status"),
        Index("ix_medication_plans_workorder", "workorder_id"),
    )

    plan_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    drug_name: Mapped[str] = mapped_column(String(160), nullable=False)
    dose: Mapped[str] = mapped_column(String(100), nullable=False)
    schedule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MedicationPlanStatus.PLANNED,
        server_default=MedicationPlanStatus.PLANNED,
    )
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    updated_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    workorder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workorders.workorder_id", ondelete="SET NULL"), nullable=True
    )
    # 医生内部备注：doctor/nurse/admin 可见，家属接口永不返回
    internal_note: Mapped[str | None] = mapped_column(Text, nullable=True)


