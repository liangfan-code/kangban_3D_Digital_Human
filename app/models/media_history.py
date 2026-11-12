"""media_history ORM：长者点播的开始/结束与完成度。

只追加与补结束，不做业务引用；`elder_id` 始终来自服务端上下文。
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class MediaHistory(Base):
    __tablename__ = "media_history"
    __table_args__ = (
        CheckConstraint(
            "completion_rate IS NULL OR (completion_rate >= 0 AND completion_rate <= 1)",
            name="ck_media_history_completion_rate",
        ),
        Index(
            "ix_media_history_elder_started",
            "elder_id",
            text("started_at DESC"),
        ),
        Index("ix_media_history_media", "media_id"),
    )

    history_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("elders.elder_id", ondelete="CASCADE", name="fk_media_history_elder"),
        nullable=False,
    )
    media_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "media_library.media_id", ondelete="CASCADE", name="fk_media_history_media"
        ),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completion_rate: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
