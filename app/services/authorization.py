"""范围判定与授权关联表管理。

访问判定语义（Day2 文档 §3）：
- 管理员恒通过；
- 护工：按长者所在房间的楼栋，对照其 active 楼栋集合；
- 医生：按 doctor_elder_assignments（active 且未过期）；
- 范围外返回"不存在"（404，隐藏存在性）；角色本身无权限（家属、其他）返回 403。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.logging import get_logger
from app.models import (
    Account,
    BuildingAssignment,
    DoctorElderAssignment,
    Elder,
    FamilyElderLink,
    Room,
)
from app.models.enums import AccountRole, AssignmentStatus, ElderStatus, RoomStatus
from app.schemas.assignment import (
    BuildingAssignmentCreate,
    DoctorAssignmentCreate,
    FamilyLinkCreate,
)

logger = get_logger(__name__)


class ObjectNotFound(Exception):
    """目标账号/楼栋/长者不存在或不可用。"""


class AssignmentConflict(Exception):
    """同账号同对象已存在有效关联。"""


class ObjectInactive(Exception):
    """目标长者已非在住状态。"""


class AssignmentValidationError(Exception):
    """关联校验失败（角色不符、有效期已过等）。"""


class AccessDecision:
    ALLOWED = "allowed"
    HIDDEN = "hidden"  # 404，隐藏存在性
    FORBIDDEN = "forbidden"  # 403，角色无权限


async def can_access_elder(
    session: AsyncSession, user: UserIdentity, elder: Elder
) -> str:
    """返回 AccessDecision 常量之一。"""
    if user.role == AccountRole.ADMIN:
        return AccessDecision.ALLOWED

    if user.role == AccountRole.NURSE:
        if elder.room_id is None:
            return AccessDecision.HIDDEN
        room = (
            await session.execute(select(Room).where(Room.room_id == elder.room_id))
        ).scalars().first()
        if room is None:
            return AccessDecision.HIDDEN
        rows = await session.execute(
            select(BuildingAssignment.building_code).where(
                BuildingAssignment.account_id == user.account_id,
                BuildingAssignment.status == AssignmentStatus.ACTIVE,
            )
        )
        allowed_buildings = set(rows.scalars().all())
        return (
            AccessDecision.ALLOWED
            if room.building_code in allowed_buildings
            else AccessDecision.HIDDEN
        )

    if user.role == AccountRole.DOCTOR:
        assignment = (
            await session.execute(
                select(DoctorElderAssignment).where(
                    DoctorElderAssignment.account_id == user.account_id,
                    DoctorElderAssignment.elder_id == elder.elder_id,
                    DoctorElderAssignment.status == AssignmentStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        if assignment is None:
            return AccessDecision.HIDDEN
        if assignment.valid_until is not None and assignment.valid_until <= datetime.now(UTC):
            return AccessDecision.HIDDEN
        return AccessDecision.ALLOWED

    if user.role == AccountRole.FAMILY:
        # 阶段一：家属放开为关联长者只读（active 且未过期），范围外 404 隐藏存在性
        link = (
            await session.execute(
                select(FamilyElderLink).where(
                    FamilyElderLink.account_id == user.account_id,
                    FamilyElderLink.elder_id == elder.elder_id,
                    FamilyElderLink.status == AssignmentStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        if link is None:
            return AccessDecision.HIDDEN
        if link.valid_until is not None and link.valid_until <= datetime.now(UTC):
            return AccessDecision.HIDDEN
        return AccessDecision.ALLOWED

    # 其余角色：无长者查询权限
    return AccessDecision.FORBIDDEN


# ---- 三张授权关联表管理（管理员） ----


async def create_building_assignment(
    session: AsyncSession, data: BuildingAssignmentCreate
) -> BuildingAssignment:
    account = (
        await session.execute(select(Account).where(Account.account_id == data.account_id))
    ).scalar_one_or_none()
    if account is None or account.role != AccountRole.NURSE:
        raise AssignmentValidationError("账号不存在或角色不是护工")
    room = (
        (await session.execute(
            select(Room).where(
                Room.building_code == data.building_code,
                Room.status == RoomStatus.ACTIVE,
            )
        ))
        .scalars()
        .first()
    )
    if room is None:
        raise ObjectNotFound("楼栋不存在")

    existing = (
        await session.execute(
            select(BuildingAssignment).where(
                BuildingAssignment.account_id == data.account_id,
                BuildingAssignment.building_code == data.building_code,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == AssignmentStatus.ACTIVE:
            raise AssignmentConflict("该护工已授权该楼栋")
        existing.status = AssignmentStatus.ACTIVE  # 软吊销后重新激活
        await session.flush()
        logger.info(
            "building assignment reactivated",
            extra={
                "event": "assignment_reactivated",
                "object_type": "building_assignment",
                "object_id": str(existing.assignment_id),
            },
        )
        return existing

    row = BuildingAssignment(
        account_id=data.account_id, building_code=data.building_code
    )
    session.add(row)
    await session.flush()
    logger.info(
        "building assignment created",
        extra={
            "event": "assignment_created",
            "object_type": "building_assignment",
            "object_id": str(row.assignment_id),
            "actor_id": str(data.account_id),
        },
    )
    return row


async def create_doctor_assignment(
    session: AsyncSession, data: DoctorAssignmentCreate
) -> DoctorElderAssignment:
    account = (
        await session.execute(select(Account).where(Account.account_id == data.account_id))
    ).scalar_one_or_none()
    if account is None or account.role != AccountRole.DOCTOR:
        raise AssignmentValidationError("账号不存在或角色不是医生")
    elder = (
        await session.execute(select(Elder).where(Elder.elder_id == data.elder_id))
    ).scalar_one_or_none()
    if elder is None:
        raise ObjectNotFound("长者不存在")
    if elder.status != ElderStatus.ACTIVE:
        raise ObjectInactive("长者非在住状态")
    if data.valid_until is not None and data.valid_until <= datetime.now(UTC):
        raise AssignmentValidationError("valid_until 必须晚于当前时间")

    existing = (
        await session.execute(
            select(DoctorElderAssignment).where(
                DoctorElderAssignment.account_id == data.account_id,
                DoctorElderAssignment.elder_id == data.elder_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == AssignmentStatus.ACTIVE:
            raise AssignmentConflict("该医生已关联该长者")
        existing.status = AssignmentStatus.ACTIVE
        existing.valid_until = data.valid_until
        await session.flush()
        return existing

    row = DoctorElderAssignment(
        account_id=data.account_id,
        elder_id=data.elder_id,
        valid_until=data.valid_until,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "doctor assignment created",
        extra={
            "event": "assignment_created",
            "object_type": "doctor_elder_assignment",
            "object_id": str(row.assignment_id),
        },
    )
    return row


async def create_family_link(session: AsyncSession, data: FamilyLinkCreate) -> FamilyElderLink:
    account = (
        await session.execute(select(Account).where(Account.account_id == data.account_id))
    ).scalar_one_or_none()
    if account is None or account.role != AccountRole.FAMILY:
        raise AssignmentValidationError("账号不存在或角色不是家属")
    elder = (
        await session.execute(select(Elder).where(Elder.elder_id == data.elder_id))
    ).scalar_one_or_none()
    if elder is None:
        raise ObjectNotFound("长者不存在")
    if elder.status != ElderStatus.ACTIVE:
        raise ObjectInactive("长者非在住状态")
    if data.valid_until is not None and data.valid_until <= datetime.now(UTC):
        raise AssignmentValidationError("valid_until 必须晚于当前时间")

    existing = (
        await session.execute(
            select(FamilyElderLink).where(
                FamilyElderLink.account_id == data.account_id,
                FamilyElderLink.elder_id == data.elder_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == AssignmentStatus.ACTIVE:
            raise AssignmentConflict("该家属已关联该长者")
        existing.status = AssignmentStatus.ACTIVE
        existing.relation = data.relation
        existing.valid_until = data.valid_until
        await session.flush()
        return existing

    row = FamilyElderLink(
        account_id=data.account_id,
        elder_id=data.elder_id,
        relation=data.relation,
        valid_until=data.valid_until,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "family link created",
        extra={
            "event": "assignment_created",
            "object_type": "family_elder_link",
            "object_id": str(row.assignment_id),
        },
    )
    return row


async def suspend_assignment(
    session: AsyncSession, kind: str, object_id: UUID
) -> bool:
    """软吊销关联（status=suspended）；返回是否存在该记录。幂等：已吊销同样返回 True。"""
    from app.models.enums import AssignmentStatus as _AS

    model = {
        "building": BuildingAssignment,
        "doctor": DoctorElderAssignment,
        "family": FamilyElderLink,
    }[kind]
    pk = {
        "building": BuildingAssignment.assignment_id,
        "doctor": DoctorElderAssignment.assignment_id,
        "family": FamilyElderLink.assignment_id,
    }[kind]

    row = (
        await session.execute(select(model).where(pk == object_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.status == _AS.ACTIVE:
        row.status = _AS.SUSPENDED
        await session.flush()
        logger.info(
            "assignment suspended",
            extra={
                "event": "assignment_suspended",
                "object_type": kind + "_assignment",
                "object_id": str(object_id),
            },
        )
    return True
