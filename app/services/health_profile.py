"""健康档案领域服务：档案 CRUD（ACL+范围双层校验）、审计流水、摘要留档、待录入清单。

权限语义（Day2 v2 计划 §3.1/§3.2）：
- 第一层：现有范围判定（护工按楼栋、医生/家属按有效关联、管理员恒过）；
- 第二层：health_grants ACL（四个 bool），叠加放行——两层都过才可见/可编辑；
- 管理员默认全量；category=major 一律 data_level=confidential，由服务端派生；
- recorded_role 由服务端注入（当前身份角色），不信任客户端。
"""

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.logging import get_logger
from app.models import (
    Account,
    Device,
    Elder,
    ElderHealthRecord,
    HealthAuditLog,
    HealthGrant,
    HealthSummary,
    Room,
)
from app.models.enums import (
    AccountRole,
    AssignmentStatus,
    DeviceStatus,
    ElderStatus,
    HealthAuditAction,
    HealthDataLevel,
    HealthRecordCategory,
    HealthSeverity,
)
from app.schemas.health_profile import (
    HealthGrantCreate,
    HealthGrantUpdate,
    HealthRecordCreate,
    HealthRecordUpdate,
)

logger = get_logger(__name__)

# 每日应测体征项（护工「今日待录入」清单口径）
EXPECTED_DAILY_VITALS = ("blood_pressure", "blood_sugar", "weight")


class HealthAccessDenied(Exception):
    """范围判定通过但 ACL 未授权（403）。"""


class GrantConflict(Exception):
    """同账号同长者的授权已存在且 active（409）。"""


class GrantNotFound(Exception):
    """授权记录不存在（404）。"""


class GrantValidationError(Exception):
    """授权目标校验失败（账号角色不符 / 长者非在住等，422）。"""


# ---- ACL 基础 ----


async def get_active_grant(
    session: AsyncSession, account_id: UUID, elder_id: UUID
) -> HealthGrant | None:
    return (
        await session.execute(
            select(HealthGrant).where(
                HealthGrant.account_id == account_id,
                HealthGrant.elder_id == elder_id,
                HealthGrant.status == AssignmentStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()


async def viewer_level(session: AsyncSession, user: UserIdentity, elder_id: UUID) -> str | None:
    """返回查看者健康数据级别：confidential / basic / None（无任何授权）。

    管理员默认全量；其余角色一律按 ACL 授权（护工/医生/家属均需放权，决策 D2）。
    """
    if user.role == AccountRole.ADMIN:
        return HealthDataLevel.CONFIDENTIAL
    grant = await get_active_grant(session, user.account_id, elder_id)
    if grant is None:
        return None
    if grant.can_view_confidential:
        return HealthDataLevel.CONFIDENTIAL
    if grant.can_view_basic:
        return HealthDataLevel.BASIC
    return None


def derive_data_level(category: str) -> str:
    """服务端派生 data_level：major→confidential，其余→basic；不信任客户端。"""
    if category == HealthRecordCategory.MAJOR:
        return HealthDataLevel.CONFIDENTIAL
    return HealthDataLevel.BASIC


async def _require_edit_permission(
    session: AsyncSession, user: UserIdentity, elder_id: UUID, category: str
) -> None:
    """编辑权限按 ACL 的 can_edit_* 显式判定，查看权限不隐含编辑权限。"""
    if user.role == AccountRole.ADMIN:
        return
    grant = await get_active_grant(session, user.account_id, elder_id)
    if grant is None:
        raise HealthAccessDenied("无健康档案访问授权")
    needed = derive_data_level(category)
    if needed == HealthDataLevel.CONFIDENTIAL and not grant.can_edit_confidential:
        raise HealthAccessDenied("无重大疾病信息编辑授权")
    if needed == HealthDataLevel.BASIC and not grant.can_edit_basic:
        raise HealthAccessDenied("无基础健康信息编辑授权")


def _recorded_role(user: UserIdentity) -> str:
    return user.role if user.role in (AccountRole.NURSE, AccountRole.DOCTOR) else AccountRole.ADMIN


async def _write_audit(
    session: AsyncSession,
    record: ElderHealthRecord,
    action: str,
    old_value: dict | None,
    new_value: dict | None,
    operator_id: UUID,
) -> None:
    session.add(
        HealthAuditLog(
            record_id=record.record_id,
            elder_id=record.elder_id,
            action=action,
            old_value=old_value,
            new_value=new_value,
            operator_id=operator_id,
        )
    )


def _snapshot(record: ElderHealthRecord) -> dict:
    return {
        "category": record.category,
        "item_key": record.item_key,
        "value_text": record.value_text,
        "severity": record.severity,
        "data_level": record.data_level,
    }


def _filter_records(records: list[ElderHealthRecord], level: str) -> list[ElderHealthRecord]:
    """接口层过滤：level=basic 时丢弃全部 confidential 记录（护工任何路径取不到 major）。"""
    if level == HealthDataLevel.CONFIDENTIAL:
        return records
    return [r for r in records if r.data_level == HealthDataLevel.BASIC]


async def list_profile(
    session: AsyncSession, user: UserIdentity, elder: Elder
) -> tuple[str, list[ElderHealthRecord], dict]:
    """返回 (viewer_level, 过滤后记录, 长者上下文)；无任何授权抛 HealthAccessDenied。"""
    level = await viewer_level(session, user, elder.elder_id)
    if level is None:
        raise HealthAccessDenied("无健康档案访问授权")
    records = (
        await session.execute(
            select(ElderHealthRecord)
            .where(ElderHealthRecord.elder_id == elder.elder_id)
            .order_by(ElderHealthRecord.category, ElderHealthRecord.created_at)
        )
    ).scalars().all()
    context = await elder_context(session, elder)
    return level, list(_filter_records(list(records), level)), context


async def elder_context(session: AsyncSession, elder: Elder) -> dict:
    """长者展示上下文：房间/楼栋/在用设备（家属端要求展示全部安置信息）。"""
    room_label = None
    building = None
    if elder.room_id is not None:
        room = (
            await session.execute(select(Room).where(Room.room_id == elder.room_id))
        ).scalar_one_or_none()
        if room is not None:
            room_label = f"{room.building_code}栋{room.room_number}房{elder.bed_no or ''}床"
            building = room.building_code
        devices = (
            await session.execute(
                select(Device).where(
                    Device.room_id == elder.room_id,
                    Device.status == DeviceStatus.ACTIVE,
                )
            )
        ).scalars().all()
        device_codes = [d.device_code for d in devices]
    else:
        device_codes = []
    return {
        "elder_name": elder.name,
        "room_label": room_label,
        "building_code": building,
        "device_codes": device_codes,
    }


async def create_record(
    session: AsyncSession, user: UserIdentity, elder: Elder, data: HealthRecordCreate
) -> ElderHealthRecord:
    await _require_edit_permission(session, user, elder.elder_id, data.category)
    record = ElderHealthRecord(
        elder_id=elder.elder_id,
        category=data.category,
        item_key=data.item_key,
        value_text=data.value_text,
        severity=data.severity,
        data_level=derive_data_level(data.category),
        recorded_by=user.account_id,
        recorded_role=_recorded_role(user),
    )
    session.add(record)
    await session.flush()
    await _write_audit(
        session, record, HealthAuditAction.CREATE, None, _snapshot(record), user.account_id
    )
    logger.info(
        "health record created",
        extra={
            "event": "health_record_created",
            "object_type": "elder_health_record",
            "object_id": str(record.record_id),
            "elder_id": str(elder.elder_id),
            "actor_id": str(user.account_id),
        },
    )
    return record


async def get_record_or_404(
    session: AsyncSession, record_id: UUID
) -> ElderHealthRecord:
    record = (
        await session.execute(
            select(ElderHealthRecord).where(ElderHealthRecord.record_id == record_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("健康记录不存在")
    return record


async def update_record(
    session: AsyncSession, user: UserIdentity, record: ElderHealthRecord, data: HealthRecordUpdate
) -> ElderHealthRecord:
    await _require_edit_permission(session, user, record.elder_id, record.category)
    old = _snapshot(record)
    if data.item_key is not None:
        record.item_key = data.item_key
    if data.value_text is not None:
        record.value_text = data.value_text
    if data.severity is not None:
        record.severity = data.severity
    await session.flush()
    await _write_audit(
        session, record, HealthAuditAction.UPDATE, old, _snapshot(record), user.account_id
    )
    logger.info(
        "health record updated",
        extra={
            "event": "health_record_updated",
            "object_type": "elder_health_record",
            "object_id": str(record.record_id),
            "actor_id": str(user.account_id),
        },
    )
    return record


# ---- 授权 ACL 管理（管理员放权，决策 D2） ----


async def upsert_grant(
    session: AsyncSession, actor: UserIdentity, data: HealthGrantCreate
) -> HealthGrant:
    """创建或更新授权：同账号同长者已有记录则覆盖四个 bool（吊销态一并复活）。"""
    account = (
        await session.execute(select(Account).where(Account.account_id == data.account_id))
    ).scalar_one_or_none()
    if account is None or account.role not in (
        AccountRole.NURSE,
        AccountRole.DOCTOR,
        AccountRole.FAMILY,
    ):
        raise GrantValidationError("账号不存在或角色不是护工/医生/家属")
    elder = (
        await session.execute(select(Elder).where(Elder.elder_id == data.elder_id))
    ).scalar_one_or_none()
    if elder is None or elder.status != ElderStatus.ACTIVE:
        raise GrantValidationError("长者不存在或非在住状态")
    if data.can_edit_confidential and not data.can_view_confidential:
        raise GrantValidationError("编辑权限必须包含对应查看权限")
    if data.can_edit_basic and not data.can_view_basic:
        raise GrantValidationError("编辑权限必须包含对应查看权限")

    existing = (
        await session.execute(
            select(HealthGrant).where(
                HealthGrant.account_id == data.account_id,
                HealthGrant.elder_id == data.elder_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.can_view_basic = data.can_view_basic
        existing.can_edit_basic = data.can_edit_basic
        existing.can_view_confidential = data.can_view_confidential
        existing.can_edit_confidential = data.can_edit_confidential
        existing.status = AssignmentStatus.ACTIVE
        existing.granted_by = actor.account_id
        await session.flush()
        return existing

    row = HealthGrant(
        account_id=data.account_id,
        elder_id=data.elder_id,
        can_view_basic=data.can_view_basic,
        can_edit_basic=data.can_edit_basic,
        can_view_confidential=data.can_view_confidential,
        can_edit_confidential=data.can_edit_confidential,
        granted_by=actor.account_id,
    )
    session.add(row)
    await session.flush()
    return row


async def update_grant(
    session: AsyncSession, actor: UserIdentity, grant: HealthGrant, data: HealthGrantUpdate
) -> HealthGrant:
    fields = data.model_dump(exclude_none=True)
    for key, value in fields.items():
        setattr(grant, key, value)
    if grant.can_edit_confidential and not grant.can_view_confidential:
        raise GrantValidationError("编辑权限必须包含对应查看权限")
    if grant.can_edit_basic and not grant.can_view_basic:
        raise GrantValidationError("编辑权限必须包含对应查看权限")
    grant.granted_by = actor.account_id
    await session.flush()
    return grant


async def suspend_grant(session: AsyncSession, grant_id: UUID) -> bool:
    """软吊销授权（status=suspended）；幂等。返回是否存在。"""
    grant = (
        await session.execute(select(HealthGrant).where(HealthGrant.grant_id == grant_id))
    ).scalar_one_or_none()
    if grant is None:
        return False
    if grant.status == AssignmentStatus.ACTIVE:
        grant.status = AssignmentStatus.SUSPENDED
        await session.flush()
        logger.info(
            "health grant suspended",
            extra={
                "event": "health_grant_suspended",
                "object_type": "health_grant",
                "object_id": str(grant_id),
            },
        )
    return True


# ---- 摘要留档与已阅回执 ----


def build_summary_text(elder: Elder, records: list[ElderHealthRecord]) -> str:
    """规则式聚合摘要：按分区拼接（服务端固定格式，非 LLM）。"""
    from zoneinfo import ZoneInfo

    from app.core.config import get_settings

    by_cat: dict[str, list[ElderHealthRecord]] = defaultdict(list)
    for r in records:
        by_cat[r.category].append(r)
    tz = ZoneInfo(get_settings().app_timezone)
    lines = [f"健康摘要（{elder.name}，生成于 {datetime.now(tz).strftime('%Y-%m-%d %H:%M')}）"]
    section_titles = {
        HealthRecordCategory.VITAL: "一、体征",
        HealthRecordCategory.BODY_PART: "二、身体部位",
        HealthRecordCategory.MEDICATION: "三、当前用药",
        HealthRecordCategory.CHRONIC: "四、慢性病",
        HealthRecordCategory.MAJOR: "五、重大疾病（隐私级）",
    }
    for cat in (
        HealthRecordCategory.VITAL,
        HealthRecordCategory.BODY_PART,
        HealthRecordCategory.MEDICATION,
        HealthRecordCategory.CHRONIC,
        HealthRecordCategory.MAJOR,
    ):
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(section_titles[cat])
        for r in items:
            mark = {"warning": "【预警】", "attention": "【需关注】"}.get(r.severity, "")
            lines.append(f"- {mark}{r.item_key}：{r.value_text}")
    return "\n".join(lines)


async def create_summary(
    session: AsyncSession, user: UserIdentity, elder: Elder
) -> HealthSummary:
    """医生生成健康摘要快照：版本自增留档（管理员亦可）。"""
    if user.role not in (AccountRole.DOCTOR, AccountRole.ADMIN):
        raise HealthAccessDenied("仅医生可生成健康摘要")
    level = await viewer_level(session, user, elder.elder_id)
    if level != HealthDataLevel.CONFIDENTIAL:
        raise HealthAccessDenied("无生成健康摘要权限（需重大疾病查看授权）")
    records = (
        await session.execute(
            select(ElderHealthRecord).where(ElderHealthRecord.elder_id == elder.elder_id)
        )
    ).scalars().all()
    max_version = (
        await session.execute(
            select(func.coalesce(func.max(HealthSummary.version), 0)).where(
                HealthSummary.elder_id == elder.elder_id
            )
        )
    ).scalar_one()
    summary = HealthSummary(
        elder_id=elder.elder_id,
        summary_text=build_summary_text(elder, list(records)),
        version=max_version + 1,
        generated_by=user.account_id,
    )
    session.add(summary)
    await session.flush()
    return summary


async def acknowledge_summary(
    session: AsyncSession, user: UserIdentity, summary: HealthSummary
) -> HealthSummary:
    """家属已阅回执：只记首个回执，幂等。"""
    if summary.acknowledged_at is None:
        summary.acknowledged_by = user.account_id
        summary.acknowledged_at = datetime.now(UTC)
        await session.flush()
    return summary


# ---- 护工：今日待录入清单 + 体征预警（增强项 B/E） ----


async def pending_measures(session: AsyncSession, user: UserIdentity) -> dict:
    """护工所辖楼栋内：当日未录体征项清单 + 存在 warning 体征的长者集合。"""
    if user.role != AccountRole.NURSE:
        raise HealthAccessDenied("仅护工可查询待录入清单")
    from app.models import BuildingAssignment

    buildings = (
        await session.execute(
            select(BuildingAssignment.building_code).where(
                BuildingAssignment.account_id == user.account_id,
                BuildingAssignment.status == AssignmentStatus.ACTIVE,
            )
        )
    ).scalars().all()
    if not buildings:
        return {"items": [], "warning_elder_ids": []}

    elders = (
        await session.execute(
            select(Elder)
            .join(Room, Elder.room_id == Room.room_id)
            .where(
                Room.building_code.in_(buildings),
                Elder.status == ElderStatus.ACTIVE,
            )
        )
    ).scalars().all()
    if not elders:
        return {"items": [], "warning_elder_ids": []}

    elder_ids = [e.elder_id for e in elders]
    # 「今日」按机构时区（app_timezone，默认 Asia/Shanghai）取当日零点，再换算 UTC 比较
    from zoneinfo import ZoneInfo

    from app.core.config import get_settings

    tz = ZoneInfo(get_settings().app_timezone)
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    today_records = (
        await session.execute(
            select(ElderHealthRecord).where(
                ElderHealthRecord.elder_id.in_(elder_ids),
                ElderHealthRecord.category == HealthRecordCategory.VITAL,
                ElderHealthRecord.created_at >= today_start,
            )
        )
    ).scalars().all()
    warning_records = (
        await session.execute(
            select(ElderHealthRecord.elder_id).where(
                ElderHealthRecord.elder_id.in_(elder_ids),
                ElderHealthRecord.category == HealthRecordCategory.VITAL,
                ElderHealthRecord.severity == HealthSeverity.WARNING,
            )
        )
    ).scalars().all()

    done: set[tuple[UUID, str]] = {(r.elder_id, r.item_key) for r in today_records}
    items = []
    for elder in elders:
        room_label = None
        if elder.room_id is not None:
            room = (
                await session.execute(select(Room).where(Room.room_id == elder.room_id))
            ).scalar_one_or_none()
            if room is not None:
                room_label = f"{room.building_code}栋{room.room_number}房{elder.bed_no or ''}床"
        for key in EXPECTED_DAILY_VITALS:
            if (elder.elder_id, key) not in done:
                items.append(
                    {
                        "elder_id": elder.elder_id,
                        "elder_name": elder.name,
                        "room_label": room_label,
                        "item_key": key,
                    }
                )
    return {"items": items, "warning_elder_ids": list(set(warning_records))}
