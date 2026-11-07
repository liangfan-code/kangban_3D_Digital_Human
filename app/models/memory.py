"""memory_chunks ORM：长期记忆块，含 pgvector 向量列（Day1 不指定维度）。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
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
from app.models.enums import MemoryStatus, MemoryVisibility


class MemoryChunk(TimestampMixin, Base):
    """长期记忆块：必属一个 elder；embedding 维度待向量模型确定后由后续迁移固定。"""

    __tablename__ = "memory_chunks"
    __table_args__ = (
        CheckConstraint(
            "category IN ('preference', 'health', 'event', 'relationship', 'routine', 'other')",
            name="ck_memory_chunks_category",
        ),
        CheckConstraint(
            "visibility IN ('private', 'care_team')",
            name="ck_memory_chunks_visibility",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'suppressed', 'deleted')",
            name="ck_memory_chunks_status",
        ),
        Index(
            "ix_memory_chunks_elder_status_category_created",
            "elder_id",
            "status",
            "category",
            "created_at",
        ),
        Index("ix_memory_chunks_elder_visibility", "elder_id", "visibility"),
    )

    chunk_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MemoryVisibility.PRIVATE,
        server_default=MemoryVisibility.PRIVATE,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MemoryStatus.ACTIVE, server_default=MemoryStatus.ACTIVE
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
