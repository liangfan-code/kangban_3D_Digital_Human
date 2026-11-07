"""elders ORM：长者档案；room_id + bed_no 表达住户床位。

单人间床位 1，双人间床位 1 或 2；部分唯一 (room_id, bed_no) 防止同床重复占用。
跨表容量规则（bed_no 是否超出房间 bed_count）由后续写入服务校验。
"""

import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import ElderStatus, Gender


class Elder(TimestampMixin, Base):
    __tablename__ = "elders"
    __table_args__ = (
        CheckConstraint(
            "gender IS NULL OR gender IN ('{0}', '{1}', '{2}', '{3}')".format(
                Gender.FEMALE, Gender.MALE, Gender.OTHER, Gender.UNKNOWN
            ),
            name="ck_elders_gender",
        ),
        CheckConstraint("bed_no IS NULL OR bed_no IN (1, 2)", name="ck_elders_bed_no"),
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}')".format(
                ElderStatus.ACTIVE, ElderStatus.PAUSED, ElderStatus.DISCHARGED
            ),
            name="ck_elders_status",
        ),
        CheckConstraint(
            "(room_id IS NULL) = (bed_no IS NULL)",
            name="ck_elders_room_bed_pair",
        ),
        Index(
            "uq_elders_room_bed",
            "room_id",
            "bed_no",
            unique=True,
            postgresql_where=text("room_id IS NOT NULL AND bed_no IS NOT NULL"),
        ),
        Index("ix_elders_room_id_status", "room_id", "status"),
    )

    elder_id: Mapped[uuid.UUID] = pk_uuid()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    room_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("rooms.room_id"), nullable=True)
    bed_no: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    persona_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("personas.persona_id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ElderStatus.ACTIVE, server_default=ElderStatus.ACTIVE
    )

