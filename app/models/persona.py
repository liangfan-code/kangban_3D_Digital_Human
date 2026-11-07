"""personas ORM：数字人人设模板，可被多个 elders 引用。"""

import uuid

from sqlalchemy import Boolean, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid


class Persona(TimestampMixin, Base):
    __tablename__ = "personas"
    __table_args__ = (
        UniqueConstraint("code", name="uq_personas_code"),
        Index("ix_personas_is_active_name", "is_active", "name"),
    )

    persona_id: Mapped[uuid.UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    personality: Mapped[str | None] = mapped_column(Text, nullable=True)
    greeting: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

