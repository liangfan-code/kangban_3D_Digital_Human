"""长者档案 CRUD 业务：床位成对、容量跨表校验、占用冲突 409。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.logging import get_logger
from app.models import Elder, Persona, Room
from app.models.enums import ElderStatus, RoomStatus
from app.schemas.elder import ElderCreate, ElderUpdate
from app.services.authorization import AccessDecision, can_access_elder

logger = get_logger(__name__)


class ElderValidationError(Exception):
    """业务校验失败（房间不可用、床位超容量等）；路由层映射 422。"""


class ElderConflict(Exception):
    """床位已被占用；路由层映射 409。"""


class ElderNotFound(Exception):
    """长者不存在；路由层映射 404。"""


class ElderPermissionDenied(Exception):
    """角色无权限；路由层映射 403。"""


async def _validate_placement(
    session: AsyncSession,
    room_id: UUID,
    bed_no: int | None,
    exclude_elder_id: UUID | None = None,
) -> None:
    room = (
        await session.execute(select(Room).where(Room.room_id == room_id))
    ).scalar_one_or_none()
    if room is None or room.status != RoomStatus.ACTIVE:
        raise ElderValidationError("房间不存在或非可用状态")
    if bed_no is not None and bed_no > room.bed_count:
        raise ElderValidationError("床位号超出房间床位数")

    stmt = select(Elder).where(Elder.room_id == room_id, Elder.bed_no == bed_no)
    if exclude_elder_id is not None:
        stmt = stmt.where(Elder.elder_id != exclude_elder_id)
    occupied = (await session.execute(stmt)).scalar_one_or_none()
    if occupied is not None:
        raise ElderConflict("该床位已被占用")


async def _validate_persona(session: AsyncSession, persona_id: UUID) -> None:
    persona = (
        await session.execute(select(Persona).where(Persona.persona_id == persona_id))
    ).scalar_one_or_none()
    if persona is None:
        raise ElderValidationError("数字人设不存在")
    if not persona.is_active:
        raise ElderValidationError("数字人设未启用")


async def create_elder(session: AsyncSession, data: ElderCreate, actor: UserIdentity) -> Elder:
    """创建长者档案。room_id 与 bed_no 必须成对（schema 层已保证 create 成对可为空）。"""
    await _validate_persona(session, data.persona_id)
    if data.room_id is not None:
        await _validate_placement(session, data.room_id, data.bed_no)

    elder = Elder(
        name=data.name,
        birth_date=data.birth_date,
        gender=data.gender,
        persona_id=data.persona_id,
        room_id=data.room_id,
        bed_no=data.bed_no,
        status=ElderStatus.ACTIVE,
    )
    session.add(elder)
    await session.flush()
    logger.info(
        "elder created",
        extra={
            "event": "elder_created",
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
            "object_type": "elder",
            "object_id": str(elder.elder_id),
            "result": "success",
        },
    )
    return elder


async def update_elder(
    session: AsyncSession, elder: Elder, data: ElderUpdate, actor: UserIdentity
) -> Elder:
    """更新长者档案：变更床位时按新值做容量与占用校验（排除自身）。"""
    update_fields = data.model_dump(exclude_unset=True)

    if "persona_id" in update_fields and update_fields["persona_id"] != elder.persona_id:
        await _validate_persona(session, update_fields["persona_id"])

    if "room_id" in update_fields and update_fields["room_id"] != elder.room_id:
        await _validate_placement(session, update_fields["room_id"], update_fields["bed_no"])
    elif "bed_no" in update_fields and update_fields["bed_no"] != elder.bed_no:
        await _validate_placement(
            session, elder.room_id, update_fields["bed_no"], exclude_elder_id=elder.elder_id
        )

    for field, value in update_fields.items():
        setattr(elder, field, value)

    await session.flush()
    await session.refresh(elder)  # 取回 onupdate 生成值，避免响应序列化时触发同步 IO
    logger.info(
        "elder updated",
        extra={
            "event": "elder_updated",
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
            "object_type": "elder",
            "object_id": str(elder.elder_id),
            "result": "success",
        },
    )
    return elder


async def get_elder_for_user(
    session: AsyncSession, user: UserIdentity, elder_id: UUID
) -> Elder:
    """按身份取长者：范围外 404 隐藏存在性，无权限角色 403。"""
    elder = (
        await session.execute(select(Elder).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    if elder is None:
        raise ElderNotFound("长者不存在")

    decision = await can_access_elder(session, user, elder)
    if decision == AccessDecision.ALLOWED:
        return elder
    if decision == AccessDecision.FORBIDDEN:
        raise ElderPermissionDenied("无权限访问该长者")
    raise ElderNotFound("长者不存在")


async def _visible_elder_ids(session: AsyncSession, user: UserIdentity) -> set[UUID] | None:
    """非 admin 角色的可见长者 id 集合；admin 返回 None（表示不过滤）。

    与 can_access_elder 同一语义：nurse 按楼栋、doctor/family 按有效关联（active 且未过期）。
    """
    from datetime import UTC, datetime

    from app.models import BuildingAssignment, DoctorElderAssignment, FamilyElderLink
    from app.models.enums import AccountRole as _AR
    from app.models.enums import AssignmentStatus as _AS

    if user.role == _AR.ADMIN:
        return None

    if user.role == _AR.NURSE:
        rows = await session.execute(
            select(BuildingAssignment.building_code).where(
                BuildingAssignment.account_id == user.account_id,
                BuildingAssignment.status == _AS.ACTIVE,
            )
        )
        buildings = set(rows.scalars().all())
        if not buildings:
            return set()
        room_rows = await session.execute(
            select(Room.room_id).where(Room.building_code.in_(buildings))
        )
        room_ids = set(room_rows.scalars().all())
        if not room_ids:
            return set()
        elder_rows = await session.execute(
            select(Elder.elder_id).where(Elder.room_id.in_(room_ids))
        )
        return set(elder_rows.scalars().all())

    if user.role == _AR.DOCTOR:
        model = DoctorElderAssignment
    elif user.role == _AR.FAMILY:
        model = FamilyElderLink
    else:
        return set()

    rows = await session.execute(
        select(model).where(model.account_id == user.account_id, model.status == _AS.ACTIVE)
    )
    now = datetime.now(UTC)
    return {
        row.elder_id
        for row in rows.scalars().all()
        if row.valid_until is None or row.valid_until > now
    }


async def list_elders_for_user(session: AsyncSession, user: UserIdentity) -> list[Elder]:
    """按角色收敛的长者列表：admin 全量，nurse/doctor/family 仅范围内。"""
    visible = await _visible_elder_ids(session, user)
    stmt = select(Elder).order_by(Elder.created_at)
    if visible is not None:
        if not visible:
            return []
        stmt = stmt.where(Elder.elder_id.in_(visible))
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def deactivate_elder(
    session: AsyncSession, elder: Elder, actor: UserIdentity
) -> None:
    """软删长者（仅 admin）：置 discharged 并清空床位释放占用；幂等。"""
    if elder.status != ElderStatus.DISCHARGED:
        elder.status = ElderStatus.DISCHARGED
        elder.room_id = None
        elder.bed_no = None
        await session.flush()
        logger.info(
            "elder deactivated",
            extra={
                "event": "elder_deactivated",
                "actor_id": str(actor.account_id),
                "object_type": "elder",
                "object_id": str(elder.elder_id),
                "result": "success",
            },
        )
