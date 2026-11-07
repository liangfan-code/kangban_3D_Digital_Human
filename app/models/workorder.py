"""workorders ORM：护工工单，必属一个 elder。

无 staff 表，assignee_ref 暂存外部引用，不伪造外键。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import WorkorderPriority, WorkorderStatus


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
        Index("ix_workorders_status_created_at", "status", "created_at"),
        Index("ix_workorders_elder_status", "elder_id", "status"),
    )

    workorder_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
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
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
