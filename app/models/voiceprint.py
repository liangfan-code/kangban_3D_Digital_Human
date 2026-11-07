"""voiceprints ORM：声纹向量记录，含 pgvector 向量列（Day1 不指定维度）。"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid
from app.models.enums import VoiceprintStatus


class Voiceprint(Base):
    """声纹记录：必属一个 elder；向量加密方案待 POC，Day1 不实现。"""

    __tablename__ = "voiceprints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}')".format(
                VoiceprintStatus.ACTIVE, VoiceprintStatus.REVOKED, VoiceprintStatus.EXPIRED
            ),
            name="ck_voiceprints_status",
        ),
        Index("ix_voiceprints_elder_status", "elder_id", "status"),
    )

    voiceprint_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("elders.elder_id"), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sample_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=VoiceprintStatus.ACTIVE,
        server_default=VoiceprintStatus.ACTIVE,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 仅 created_at，无 updated_at（声纹记录不原地更新）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
