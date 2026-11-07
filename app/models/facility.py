"""rooms、devices ORM：设施核心表。"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import DeviceStatus, DeviceType, RoomStatus


class Room(TimestampMixin, Base):
    """房间：单人间 bed_count=1，双人间 bed_count=2。"""

    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint("bed_count IN (1, 2)", name="ck_rooms_bed_count"),
        CheckConstraint(
            "status IN ('active', 'maintenance', 'inactive')",
            name="ck_rooms_status",
        ),
        UniqueConstraint(
            "building_code", "floor", "room_number", name="uq_rooms_building_floor_number"
        ),
        Index("ix_rooms_status", "status"),
    )

    room_id: Mapped[uuid.UUID] = pk_uuid()
    building_code: Mapped[str] = mapped_column(String(32), nullable=False)
    floor: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    room_number: Mapped[str] = mapped_column(String(32), nullable=False)
    bed_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RoomStatus.ACTIVE, server_default=RoomStatus.ACTIVE
    )


class Device(TimestampMixin, Base):
    """设备（平板等）：必属一个房间；bed_no 空表示房间级设备；Day1 不存 JWT。"""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("bed_no IS NULL OR bed_no IN (1, 2)", name="ck_devices_bed_no"),
        CheckConstraint(
            "device_type IN ('{0}')".format(DeviceType.TABLET),
            name="ck_devices_device_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled', 'retired')",
            name="ck_devices_status",
        ),
        Index("ix_devices_room_id_status", "room_id", "status"),
    )

    device_id: Mapped[uuid.UUID] = pk_uuid()
    device_code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rooms.room_id"), nullable=False
    )
    bed_no: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    device_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DeviceType.TABLET, server_default=DeviceType.TABLET
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DeviceStatus.PENDING,
        server_default=DeviceStatus.PENDING,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
