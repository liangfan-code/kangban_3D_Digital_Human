"""health_events ORM（Day8 段1）：一天一位长者一条健康采集事件。

状态机（数据库为权威）::

    collecting ──字段集齐且绿色──▶ archived
        │
        ├──黄色──▶ pending_confirm ──confirm──▶ confirmed
        └──红色──▶ pending_confirm ──escalate─▶ escalated
                                       └─reject──▶ archived

约束：
- ``thread_id`` 唯一（``health:{elder_id}:{YYYY-MM-DD}``），同日重入只更新不新建；
- 只存结构化数值与理由码，不存整段对话与音频；
- ``reasons`` 只放规则码（如 ``systolic_ge_180``），不放长者原话。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import HealthEventLevel, HealthEventStatus


class HealthEvent(TimestampMixin, Base):
    __tablename__ = "health_events"
    __table_args__ = (
        Index("ux_health_events_thread", "thread_id", unique=True),
        Index("ix_health_events_elder_created", "elder_id", "created_at"),
        Index("ix_health_events_status_created", "status", "created_at"),
    )

    event_id: Mapped[uuid.UUID] = pk_uuid()
    thread_id: Mapped[str] = mapped_column(String(96), nullable=False)
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=HealthEventLevel.GREEN,
        server_default=HealthEventLevel.GREEN,
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=HealthEventStatus.COLLECTING,
        server_default=HealthEventStatus.COLLECTING,
    )
    bp_systolic: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    bp_diastolic: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    sleep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mood: Mapped[str | None] = mapped_column(String(24), nullable=True)
    chief_complaint: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasons: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    workorder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workorders.workorder_id", ondelete="SET NULL"), nullable=True
    )
    # 医院转诊单号；默认 mock，真实对接需单独授权
    referral_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def reason_list(self) -> list[str]:
        """JSONB 反序列化兜底：NULL/脏数据按空列表处理。"""
        raw: Any = self.reasons
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw]
