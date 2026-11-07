"""房间管理业务（仅 admin）：唯一约束冲突 409、容量收缩校验、软删前置检查。"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.logging import get_logger
from app.models import Device, Elder, Room
from app.models.enums import DeviceStatus, ElderStatus, RoomStatus
from app.schemas.room import RoomCreate, RoomUpdate

logger = get_logger(__name__)


class RoomValidationError(Exception):
    """业务校验失败；路由层映射 422。"""


class RoomConflict(Exception):
    """楼栋+楼层+房号重复；路由层映射 409。"""


class RoomOccupied(Exception):
    """房间仍有在住长者或活跃设备，禁止软删/缩容；路由层映射 409。"""


class RoomNotFound(Exception):
    """房间不存在；路由层映射 404。"""


async def _ensure_unique(
    session: AsyncSession,
    building_code: str,
    floor: int,
    room_number: str,
    exclude_room_id: uuid.UUID | None = None,
) -> None:
    stmt = select(Room).where(
        Room.building_code == building_code,
        Room.floor == floor,
        Room.room_number == room_number,
    )
    if exclude_room_id is not None:
        stmt = stmt.where(Room.room_id != exclude_room_id)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise RoomConflict("该楼栋楼层下房号已存在")


async def create_room(session: AsyncSession, data: RoomCreate, actor: UserIdentity) -> Room:
    await _ensure_unique(session, data.building_code, data.floor, data.room_number)
    room = Room(
        building_code=data.building_code,
        floor=data.floor,
        room_number=data.room_number,
        bed_count=data.bed_count,
        status=RoomStatus.ACTIVE,
    )
    session.add(room)
    await session.flush()
    logger.info(
        "room created",
        extra={
            "event": "room_created",
            "actor_id": str(actor.account_id),
            "object_type": "room",
            "object_id": str(room.room_id),
            "result": "success",
        },
    )
    return room


async def update_room(
    session: AsyncSession, room: Room, data: RoomUpdate, actor: UserIdentity
) -> Room:
    fields = data.model_dump(exclude_unset=True)

    if any(k in fields for k in ("building_code", "floor", "room_number")):
        await _ensure_unique(
            session,
            fields.get("building_code", room.building_code),
            fields.get("floor", room.floor),
            fields.get("room_number", room.room_number),
            exclude_room_id=room.room_id,
        )

    if "bed_count" in fields and fields["bed_count"] != room.bed_count:
        new_count = fields["bed_count"]
        max_elder_bed = (
            await session.execute(
                select(func.max(Elder.bed_no)).where(
                    Elder.room_id == room.room_id, Elder.status == ElderStatus.ACTIVE
                )
            )
        ).scalar_one()
        if max_elder_bed is not None and max_elder_bed > new_count:
            raise RoomValidationError("床位数不能小于当前在住长者的最大床号")
        max_device_bed = (
            await session.execute(
                select(func.max(Device.bed_no)).where(
                    Device.room_id == room.room_id,
                    Device.status == DeviceStatus.ACTIVE,
                    Device.bed_no.is_not(None),
                )
            )
        ).scalar_one()
        if max_device_bed is not None and max_device_bed > new_count:
            raise RoomValidationError("床位数不能小于当前绑定设备的最大床号")

    for key, value in fields.items():
        setattr(room, key, value)
    await session.flush()
    await session.refresh(room)
    logger.info(
        "room updated",
        extra={
            "event": "room_updated",
            "actor_id": str(actor.account_id),
            "object_type": "room",
            "object_id": str(room.room_id),
            "result": "success",
        },
    )
    return room


async def deactivate_room(session: AsyncSession, room: Room, actor: UserIdentity) -> None:
    """软删：置 inactive（幂等）。房间内有在住长者或活跃设备时拒绝。"""
    if room.status != RoomStatus.INACTIVE:
        elder = (
            (
                await session.execute(
                    select(Elder).where(
                        Elder.room_id == room.room_id, Elder.status == ElderStatus.ACTIVE
                    )
                )
            )
            .scalars()
            .first()
        )
        if elder is not None:
            raise RoomOccupied("房间内仍有在住长者，不能停用")
        device = (
            (
                await session.execute(
                    select(Device).where(
                        Device.room_id == room.room_id, Device.status == DeviceStatus.ACTIVE
                    )
                )
            )
            .scalars()
            .first()
        )
        if device is not None:
            raise RoomOccupied("房间内仍有活跃设备，不能停用")
        room.status = RoomStatus.INACTIVE
        await session.flush()
        logger.info(
            "room deactivated",
            extra={
                "event": "room_deactivated",
                "actor_id": str(actor.account_id),
                "object_type": "room",
                "object_id": str(room.room_id),
                "result": "success",
            },
        )


async def get_room_or_404(session: AsyncSession, room_id: uuid.UUID) -> Room:
    room = (
        await session.execute(select(Room).where(Room.room_id == room_id))
    ).scalar_one_or_none()
    if room is None:
        raise RoomNotFound("房间不存在")
    return room
