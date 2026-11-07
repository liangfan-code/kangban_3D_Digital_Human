"""health_records ORM：健康采集记录，必属一个 elder。"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, pk_uuid


class HealthRecord(CreatedAtMixin, Base):
    """健康记录：仅 created_at，无 updated_at（采集记录不原地修改）。"""

    __tablename__ = "health_records"
    __table_args__ = (
        CheckConstraint(
            "bp_systolic IS NULL OR bp_systolic BETWEEN 50 AND 250",
            name="ck_health_records_bp_systolic_range",
        ),
        CheckConstraint(
            "bp_diastolic IS NULL OR bp_diastolic BETWEEN 30 AND 150",
            name="ck_health_records_bp_diastolic_range",
        ),
        CheckConstraint(
            "sleep_minutes IS NULL OR sleep_minutes BETWEEN 0 AND 1440",
            name="ck_health_records_sleep_minutes_range",
        ),
        Index("ix_health_records_elder_recorded_at", "elder_id", "recorded_at"),
    )

    record_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bp_systolic: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    bp_diastolic: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    sleep_minutes: Mapped[int | None] = mapped_column(nullable=True)
    mood: Mapped[str | None] = mapped_column(String(24), nullable=True)
    chief_complaint: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
