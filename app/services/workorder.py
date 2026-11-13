"""工单领域服务（Day8）：敏感事件建单、三态处理项、终审关闭、可见范围查询与上下文快照。

边界：
- 只做业务编排与状态机，不处理 HTTP；
- 可见范围：医生按 doctor_elder_assignments、护工按楼栋（rooms.building_code）、
  管理员全量、家属由上层传 elder_id 且在此校验 family_elder_links；
- 日志只记 workorder_id / sensitive_category / actor_role，禁止记录对话原文与方案全文。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import (
    Account,
    BuildingAssignment,
    DoctorElderAssignment,
    Elder,
    FamilyElderLink,
    MedicationPlan,
    Room,
    Workorder,
    WorkorderDisposition,
    WorkorderEvent,
    WorkorderRead,
)
from app.models.enums import (
    AccountRole,
    AssignmentStatus,
    MedicationPlanStatus,
    WorkorderDispositionStatus,
    WorkorderOrderKind,
    WorkorderPriority,
    WorkorderSource,
    WorkorderStatus,
)

logger = get_logger(__name__)

# 工单类型与终态
SENSITIVE_TYPE = "sensitive"
MEDICATION_PLAN_TYPE = "medication_plan"
MISSED_MEDICATION_TYPE = "missed_medication"
HEALTH_ALERT_TYPE = "health_alert"
CLOSED_STATUSES = (WorkorderStatus.RESOLVED, WorkorderStatus.CANCELLED)
STAFF_ROLES = (AccountRole.DOCTOR, AccountRole.NURSE)
SNAPSHOT_MAX_TURNS = 5
MISSED_REASON_CODES = frozenset(
    {"retry_exhausted", "timeout_exhausted", "startup_backfill", "no_device"}
)
# 这些计划状态下，长期用药工单不允许管理员手工关闭
STANDING_ACTIVE_PLAN_STATUSES = (
    MedicationPlanStatus.PLANNED,
    MedicationPlanStatus.ACTIVE,
    MedicationPlanStatus.PAUSED,
)


class WorkOrderError(Exception):
    """工单领域异常基类。"""


class WorkOrderNotFound(WorkOrderError):
    """工单/处理项/关联不存在（上层映射 404，不暴露存在性）。"""


class WorkOrderConflict(WorkOrderError):
    """状态机冲突或重复提交（上层映射 409）。"""


class WorkOrderForbidden(WorkOrderError):
    """角色与处理项不匹配或越权（上层映射 403）。"""


class WorkOrderValidationError(WorkOrderError):
    """入参不合法（如方案为空，上层映射 400/422）。"""


# ---- 内部工具 ----


def _now() -> datetime:
    return datetime.now(UTC)


def _add_event(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    event_type: str,
    actor_account_id: UUID | None = None,
    actor_role: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """追加一条工单审计事件（只记元数据，不记对话原文/方案全文）。"""
    session.add(
        WorkorderEvent(
            workorder_id=workorder_id,
            event_type=event_type,
            actor_account_id=actor_account_id,
            actor_role=actor_role,
            payload=payload,
            # 显式写时间：server_default=now() 在同事务内取值相同，
            # 会让同一批事件（created/read/disposition/closed）排序不确定
            created_at=datetime.now(UTC),
        )
    )


async def _load_account(session: AsyncSession, account_id: UUID) -> Account:
    account = await session.get(Account, account_id)
    if account is None:
        raise WorkOrderNotFound("账号不存在")
    return account


def _require_staff_role(account: Account, role: str) -> None:
    if role not in STAFF_ROLES or account.role != role:
        raise WorkOrderForbidden("当前角色无权操作该处理项")


async def _load_work_order(session: AsyncSession, workorder_id: UUID) -> Workorder:
    workorder = await session.get(Workorder, workorder_id)
    if workorder is None:
        raise WorkOrderNotFound("工单不存在")
    return workorder


async def _load_disposition(
    session: AsyncSession, workorder_id: UUID, role: str
) -> WorkorderDisposition:
    disposition = (
        await session.execute(
            select(WorkorderDisposition).where(
                WorkorderDisposition.workorder_id == workorder_id,
                WorkorderDisposition.role == role,
            )
        )
    ).scalar_one_or_none()
    if disposition is None:
        raise WorkOrderNotFound("工单处理项不存在")
    return disposition


def visible_elder_ids(role: str, account_id: UUID) -> Any | None:
    """按角色返回可见 elder_id 子查询；管理员返回 None 表示不限。

    护工无楼栋字段，经 elders.room_id → rooms.building_code 关联其授权楼栋。
    Day8 起健康事件看板复用同一口径，故由私有函数提升为公开函数（行为不变）。
    """
    if role == AccountRole.ADMIN:
        return None
    if role == AccountRole.DOCTOR:
        return select(DoctorElderAssignment.elder_id).where(
            DoctorElderAssignment.account_id == account_id,
            DoctorElderAssignment.status == AssignmentStatus.ACTIVE,
            or_(
                DoctorElderAssignment.valid_until.is_(None),
                DoctorElderAssignment.valid_until > func.now(),
            ),
        )
    if role == AccountRole.NURSE:
        buildings = select(BuildingAssignment.building_code).where(
            BuildingAssignment.account_id == account_id,
            BuildingAssignment.status == AssignmentStatus.ACTIVE,
        )
        return (
            select(Elder.elder_id)
            .join(Room, Room.room_id == Elder.room_id)
            .where(Room.building_code.in_(buildings))
        )
    raise WorkOrderForbidden("当前角色无权查询工单")


async def _assert_family_link(
    session: AsyncSession, account_id: UUID, elder_id: UUID
) -> None:
    """家属必须存在有效绑定，否则按不存在处理（404，不暴露存在性）。"""
    link = (
        await session.execute(
            select(FamilyElderLink).where(
                FamilyElderLink.account_id == account_id,
                FamilyElderLink.elder_id == elder_id,
                FamilyElderLink.status == AssignmentStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if link is None or (link.valid_until is not None and link.valid_until <= _now()):
        raise WorkOrderNotFound("工单不存在")


def _like_pattern(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入 % / _ 变成通配。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# 注意：工单事件推送（publish_workorder_event）一律由 HTTP 路由层或语音服务
# 在数据库事务 commit() 之后统一发布，服务层不做事务内推送（Day7 段1.5）。


# ---- 建单 ----


async def create_sensitive_work_order(
    session: AsyncSession,
    *,
    elder_id: UUID,
    category: str,
    device_id: UUID | str | None = None,
    trigger_text: str,
    context_snapshot: list[dict[str, Any]] | None = None,
) -> tuple[Workorder, bool]:
    """敏感事件建单；同老人+同类别存在未关闭工单时只累加次数。

    返回 (工单, 是否新建)。
    """
    category = (category or "").strip() or "unspecified"

    elder = await session.get(Elder, elder_id)
    if elder is None:
        raise WorkOrderNotFound("长者不存在")

    existing = (
        await session.execute(
            select(Workorder)
            .where(
                Workorder.elder_id == elder_id,
                Workorder.sensitive_category == category,
                Workorder.status.not_in(CLOSED_STATUSES),
            )
            .order_by(Workorder.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None:
        # 去重：不新建工单，累加发生次数并刷新触发原句
        existing.occurrence_count = (existing.occurrence_count or 1) + 1
        existing.description = trigger_text
        _add_event(
            session,
            workorder_id=existing.workorder_id,
            event_type="occurrence",
            actor_role=None,
            payload={
                "occurrence_count": existing.occurrence_count,
                "sensitive_category": category,
                "device_id": str(device_id) if device_id else None,
            },
        )
        await session.flush()
        logger.info(
            "敏感事件工单重复触发",
            extra={
                "event": "workorder_occurrence",
                "object_type": "workorder",
                "object_id": str(existing.workorder_id),
                "sensitive_category": category,
                "occurrence_count": existing.occurrence_count,
            },
        )
        return existing, False

    workorder = Workorder(
        elder_id=elder_id,
        type=SENSITIVE_TYPE,
        source=WorkorderSource.SENSITIVE,
        sensitive_category=category,
        occurrence_count=1,
        status=WorkorderStatus.OPEN,
        priority=WorkorderPriority.URGENT,
        title=f"敏感事件：{category}",
        description=trigger_text,
        context_snapshot=context_snapshot or None,
    )
    session.add(workorder)
    await session.flush()

    for role in STAFF_ROLES:
        session.add(
            WorkorderDisposition(
                workorder_id=workorder.workorder_id,
                role=role,
                status=WorkorderDispositionStatus.UNREAD,
            )
        )
    _add_event(
        session,
        workorder_id=workorder.workorder_id,
        event_type="created",
        payload={
            "source": WorkorderSource.SENSITIVE.value,
            "sensitive_category": category,
            "device_id": str(device_id) if device_id else None,
        },
    )
    await session.flush()
    logger.info(
        "敏感事件工单已创建",
        extra={
            "event": "workorder_created",
            "object_type": "workorder",
            "object_id": str(workorder.workorder_id),
            "sensitive_category": category,
        },
    )
    return workorder, True


# ---- 健康预警工单（Day8 段1） ----


async def create_health_alert_work_order(
    session: AsyncSession,
    *,
    elder_id: UUID,
    level: str,
    title: str,
    description: str,
    event_id: UUID | None = None,
) -> tuple[Workorder, bool]:
    """健康事件建单：同一事件同一级别只建一张（dedupe_key 唯一索引兜底）。

    命中 dedupe_key 时仅复用未完结（open/acknowledged/in_progress）的工单；
    事件归档后重开提交会拿到已关闭旧单，此时追加序号建新单，避免把
    已解决/已取消的工单"复活"绑到新流程上。
    """
    dedupe_key = f"health_event:{event_id}:{level}" if event_id is not None else None
    if dedupe_key is not None:
        existing = (
            await session.execute(
                select(Workorder).where(Workorder.dedupe_key == dedupe_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status in (
                WorkorderStatus.OPEN,
                WorkorderStatus.ACKNOWLEDGED,
                WorkorderStatus.IN_PROGRESS,
            ):
                return existing, False
            suffix = 2
            while True:  # dedupe_key 唯一索引：为重开流程生成未占用的序号键
                candidate = f"{dedupe_key}:{suffix}"
                dup = (
                    await session.execute(
                        select(Workorder).where(Workorder.dedupe_key == candidate)
                    )
                ).scalar_one_or_none()
                if dup is None:
                    break
                suffix += 1
            dedupe_key = candidate

    elder = await session.get(Elder, elder_id)
    if elder is None:
        raise WorkOrderNotFound("长者不存在")

    workorder = Workorder(
        elder_id=elder_id,
        type=HEALTH_ALERT_TYPE,
        source=WorkorderSource.MANUAL,
        status=WorkorderStatus.OPEN,
        priority=(
            WorkorderPriority.URGENT if level == "red" else WorkorderPriority.HIGH
        ),
        title=title,
        description=description,
        dedupe_key=dedupe_key,
        context_snapshot=[
            {
                "type": HEALTH_ALERT_TYPE,
                "level": level,
                "event_id": str(event_id) if event_id is not None else None,
            }
        ],
    )
    session.add(workorder)
    await session.flush()

    for role in STAFF_ROLES:
        session.add(
            WorkorderDisposition(
                workorder_id=workorder.workorder_id,
                role=role,
                status=WorkorderDispositionStatus.UNREAD,
            )
        )
    _add_event(
        session,
        workorder_id=workorder.workorder_id,
        event_type="created",
        payload={
            "type": HEALTH_ALERT_TYPE,
            "level": level,
            "event_id": str(event_id) if event_id is not None else None,
        },
    )
    await session.flush()
    logger.info(
        "健康预警工单已创建",
        extra={
            "event": "workorder_created",
            "object_type": "workorder",
            "object_id": str(workorder.workorder_id),
            "type": HEALTH_ALERT_TYPE,
            "level": level,
        },
    )
    return workorder, True


# ---- 用药计划长期工单 / 漏服工单 ----


def _medication_plan_description(plan: MedicationPlan) -> str:
    """长期工单描述：只放药名/剂量/服用时间/说明，绝不放 internal_note。"""
    schedule = plan.schedule or {}
    times = "、".join(schedule.get("times") or [])
    parts = [
        f"药品：{plan.drug_name}",
        f"剂量：{plan.dose}",
        f"服用时间：{times or '未设置'}",
    ]
    if plan.instructions:
        parts.append(f"用药说明：{plan.instructions}")
    return "；".join(parts)


async def create_medication_plan_work_order(
    session: AsyncSession,
    *,
    plan: MedicationPlan,
    doctor_account_id: UUID,
) -> tuple[Workorder, bool]:
    """创建/复用长期用药工单；同一 plan_id 未关闭的 standing 工单只保留一张。"""
    existing = (
        await session.execute(
            select(Workorder)
            .where(
                Workorder.medication_plan_id == plan.plan_id,
                Workorder.order_kind == WorkorderOrderKind.STANDING,
                Workorder.status.not_in(CLOSED_STATUSES),
            )
            .order_by(Workorder.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    workorder = Workorder(
        elder_id=plan.elder_id,
        type=MEDICATION_PLAN_TYPE,
        source=WorkorderSource.MANUAL,
        priority=WorkorderPriority.NORMAL,
        status=WorkorderStatus.OPEN,
        title=f"长期用药计划：{plan.drug_name}",
        description=_medication_plan_description(plan),
        order_kind=WorkorderOrderKind.STANDING,
        medication_plan_id=plan.plan_id,
        effective_from=plan.start_date,
        effective_until=plan.end_date,
    )
    session.add(workorder)
    await session.flush()

    now = _now()
    session.add_all(
        [
            WorkorderDisposition(
                workorder_id=workorder.workorder_id,
                role=AccountRole.DOCTOR,
                status=WorkorderDispositionStatus.HANDLED,
                account_id=doctor_account_id,
                plan_text="发起长期用药计划",
                handled_at=now,
            ),
            WorkorderDisposition(
                workorder_id=workorder.workorder_id,
                role=AccountRole.NURSE,
                status=WorkorderDispositionStatus.UNREAD,
            ),
        ]
    )
    _add_event(
        session,
        workorder_id=workorder.workorder_id,
        event_type="created",
        payload={
            "source": "doctor_medication_plan",
            "order_kind": WorkorderOrderKind.STANDING.value,
            "archive_roles": [AccountRole.ADMIN.value],
            "plan_id": str(plan.plan_id),
        },
    )
    await session.flush()
    logger.info(
        "长期用药工单已创建",
        extra={
            "event": "medication_plan_workorder_created",
            "object_type": "workorder",
            "object_id": str(workorder.workorder_id),
            "plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "actor_id": str(doctor_account_id),
            "actor_role": AccountRole.DOCTOR,
        },
    )
    return workorder, True


async def create_missed_medication_work_order(
    session: AsyncSession,
    *,
    order: Any,
    reason_code: str,
) -> tuple[Workorder, bool]:
    """全仓唯一漏服建单入口；dedupe_key + 部分唯一索引共同防重。"""
    if reason_code not in MISSED_REASON_CODES:
        raise WorkOrderValidationError("漏服原因码不合法")

    plan = await session.get(MedicationPlan, order.plan_id)
    if plan is None:
        raise WorkOrderNotFound("用药计划不存在")

    dedupe_key = f"missed:{order.order_id}"

    def _accumulate(existing: Workorder) -> tuple[Workorder, bool]:
        existing.occurrence_count = (existing.occurrence_count or 1) + 1
        _add_event(
            session,
            workorder_id=existing.workorder_id,
            event_type="occurrence",
            payload={
                "occurrence_count": existing.occurrence_count,
                "reason_code": reason_code,
            },
        )
        return existing, False

    existing = (
        await session.execute(
            select(Workorder).where(Workorder.dedupe_key == dedupe_key).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        result = _accumulate(existing)
        await session.flush()
        return result

    workorder = Workorder(
        elder_id=order.elder_id,
        type=MISSED_MEDICATION_TYPE,
        source=WorkorderSource.MANUAL,
        priority=WorkorderPriority.HIGH,
        status=WorkorderStatus.OPEN,
        title=f"用药漏服：{plan.drug_name}",
        description=(
            f"时段：{order.scheduled_at.isoformat()}；"
            f"尝试次数：{order.attempt_count or 0}；原因码：{reason_code}"
        ),
        order_kind=WorkorderOrderKind.ONE_OFF,
        medication_plan_id=order.plan_id,
        medication_order_id=order.order_id,
        dedupe_key=dedupe_key,
    )
    try:
        async with session.begin_nested():
            session.add(workorder)
            await session.flush()
    except IntegrityError:
        # 并发下唯一键兜底：回滚保存点后重查并累加次数
        existing = (
            await session.execute(
                select(Workorder).where(Workorder.dedupe_key == dedupe_key).limit(1)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        result = _accumulate(existing)
        await session.flush()
        return result

    session.add_all(
        [
            WorkorderDisposition(
                workorder_id=workorder.workorder_id,
                role=role,
                status=WorkorderDispositionStatus.UNREAD,
            )
            for role in STAFF_ROLES
        ]
    )
    _add_event(
        session,
        workorder_id=workorder.workorder_id,
        event_type="created",
        payload={
            "source": "missed_medication",
            "order_kind": WorkorderOrderKind.ONE_OFF.value,
            "reason_code": reason_code,
            "medication_order_id": str(order.order_id),
        },
    )
    await session.flush()
    logger.info(
        "漏服工单已创建",
        extra={
            "event": "workorder_created",
            "object_type": "workorder",
            "object_id": str(workorder.workorder_id),
            "medication_order_id": str(order.order_id),
            "plan_id": str(order.plan_id),
            "elder_id": str(order.elder_id),
            "reason_code": reason_code,
        },
    )
    return workorder, True


async def close_standing_work_order_for_plan(
    session: AsyncSession,
    *,
    plan_id: UUID,
    actor_account_id: UUID,
    reason: str,
) -> Workorder | None:
    """计划停用/取消时系统关闭长期工单：绕过双角色 handled 门禁，只写库不推送。"""
    workorder = (
        await session.execute(
            select(Workorder)
            .where(
                Workorder.medication_plan_id == plan_id,
                Workorder.order_kind == WorkorderOrderKind.STANDING,
                Workorder.status.not_in(CLOSED_STATUSES),
            )
            .order_by(Workorder.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if workorder is None:
        return None

    account = await _load_account(session, actor_account_id)
    actor_role = (
        account.role
        if account.role in (AccountRole.DOCTOR, AccountRole.ADMIN)
        else AccountRole.DOCTOR
    )
    workorder.status = WorkorderStatus.CANCELLED
    workorder.resolved_at = _now()
    workorder.closed_by = actor_account_id
    workorder.closed_note = reason
    _add_event(
        session,
        workorder_id=workorder.workorder_id,
        event_type="closed",
        actor_account_id=actor_account_id,
        actor_role=actor_role,
        payload={"reason": reason, "by": "plan_cancel"},
    )
    await session.flush()
    logger.info(
        "长期用药工单已随计划关闭",
        extra={
            "event": "medication_plan_workorder_closed",
            "object_type": "workorder",
            "object_id": str(workorder.workorder_id),
            "plan_id": str(plan_id),
            "actor_id": str(actor_account_id),
            "actor_role": actor_role,
        },
    )
    return workorder


# ---- 处理项状态机 ----


async def mark_read(
    session: AsyncSession, *, workorder_id: UUID, role: str, account_id: UUID
) -> WorkorderDisposition:
    """按账号写已读回执 + 该角色处理项 unread→read（幂等）；handled 不回退。

    语义写死：已读按账号（workorder_reads 回执，展示以此为准）、处理按角色
    （disposition 三态，handled 仍是关闭门禁）。
    """
    account = await _load_account(session, account_id)
    _require_staff_role(account, role)
    workorder = await _load_work_order(session, workorder_id)
    if workorder.status in CLOSED_STATUSES:
        raise WorkOrderConflict("工单已关闭，不可再变更")
    disposition = await _load_disposition(session, workorder_id, role)

    # 1) 按账号插入回执（联合主键幂等，ON CONFLICT DO NOTHING）
    await _insert_read_receipt(
        session, workorder_id=workorder_id, account_id=account_id
    )

    # 2) 处理项状态机：仅 unread→read；已 read/handled 不再变更、不回退
    if disposition.status == WorkorderDispositionStatus.UNREAD:
        disposition.status = WorkorderDispositionStatus.READ
        disposition.read_at = _now()
        disposition.account_id = account_id
        if workorder.status == WorkorderStatus.OPEN:
            workorder.status = WorkorderStatus.ACKNOWLEDGED
        _add_event(
            session,
            workorder_id=workorder_id,
            event_type="read",
            actor_account_id=account_id,
            actor_role=role,
        )
    await session.flush()
    logger.info(
        "工单处理项已读",
        extra={
            "event": "workorder_read_receipt",
            "object_type": "workorder",
            "object_id": str(workorder_id),
            "actor_id": str(account_id),
            "actor_role": role,
        },
    )
    return disposition


async def _insert_read_receipt(
    session: AsyncSession, *, workorder_id: UUID, account_id: UUID
) -> bool:
    """插入按账号已读回执；已存在时幂等跳过。返回是否新插入。"""
    stmt = (
        pg_insert(WorkorderRead)
        .values(workorder_id=workorder_id, account_id=account_id, read_at=_now())
        .on_conflict_do_nothing(index_elements=["workorder_id", "account_id"])
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def submit_disposition(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    role: str,
    account_id: UUID,
    plan_text: str,
) -> WorkorderDisposition:
    """提交处理方案（read/unread→handled）；重复提交抛冲突。"""
    plan = (plan_text or "").strip()
    if not plan:
        raise WorkOrderValidationError("处理方案不能为空")

    account = await _load_account(session, account_id)
    _require_staff_role(account, role)
    workorder = await _load_work_order(session, workorder_id)
    if workorder.status in CLOSED_STATUSES:
        raise WorkOrderConflict("工单已关闭，不可再提交方案")
    disposition = await _load_disposition(session, workorder_id, role)
    if disposition.status == WorkorderDispositionStatus.HANDLED:
        raise WorkOrderConflict("该处理项已处理，不能重复提交")

    disposition.status = WorkorderDispositionStatus.HANDLED
    disposition.plan_text = plan
    disposition.handled_at = _now()
    disposition.account_id = account_id
    workorder.status = WorkorderStatus.IN_PROGRESS
    _add_event(
        session,
        workorder_id=workorder_id,
        event_type="disposition",
        actor_account_id=account_id,
        actor_role=role,
        payload={"role": role, "plan_length": len(plan)},
    )
    await session.flush()
    logger.info(
        "工单处理方案已提交",
        extra={
            "event": "workorder_disposition",
            "object_type": "workorder",
            "object_id": str(workorder_id),
            "actor_id": str(account_id),
            "actor_role": role,
        },
    )
    return disposition


# ---- 管理员终审关闭 ----


async def close_work_order(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    admin_account_id: UUID,
    note: str | None = None,
) -> Workorder:
    """双角色处理项都 handled 后才允许关闭；否则冲突并列出未完成角色。"""
    account = await _load_account(session, admin_account_id)
    if account.role != AccountRole.ADMIN:
        raise WorkOrderForbidden("仅管理员可关闭工单")

    workorder = await _load_work_order(session, workorder_id)
    if workorder.status in CLOSED_STATUSES:
        raise WorkOrderConflict("工单已关闭，不可重复关闭")

    if (
        workorder.order_kind == WorkorderOrderKind.STANDING
        and workorder.medication_plan_id is not None
    ):
        plan = await session.get(MedicationPlan, workorder.medication_plan_id)
        if plan is not None and plan.status in STANDING_ACTIVE_PLAN_STATUSES:
            raise WorkOrderConflict("长期用药工单随用药计划停用/取消自动关闭")

    rows = (
        await session.execute(
            select(WorkorderDisposition).where(
                WorkorderDisposition.workorder_id == workorder_id
            )
        )
    ).scalars().all()
    handled = {
        row.role
        for row in rows
        if row.status == WorkorderDispositionStatus.HANDLED
    }
    pending = [role for role in STAFF_ROLES if role not in handled]
    if pending:
        raise WorkOrderConflict(f"尚有角色未完成处理：{'、'.join(pending)}")

    workorder.status = WorkorderStatus.RESOLVED
    workorder.resolved_at = _now()
    workorder.closed_by = admin_account_id
    workorder.closed_note = note
    _add_event(
        session,
        workorder_id=workorder_id,
        event_type="closed",
        actor_account_id=admin_account_id,
        actor_role=AccountRole.ADMIN,
        payload={"roles_completed": [str(role) for role in STAFF_ROLES]},
    )
    await session.flush()
    logger.info(
        "工单已终审关闭",
        extra={
            "event": "workorder_closed",
            "object_type": "workorder",
            "object_id": str(workorder_id),
            "actor_id": str(admin_account_id),
            "actor_role": AccountRole.ADMIN,
        },
    )
    return workorder


# ---- 查询 ----


async def list_dispositions(
    session: AsyncSession, workorder_ids: list[UUID]
) -> dict[UUID, list[WorkorderDisposition]]:
    """批量加载处理项，按工单分组（角色升序）；空入参不发查询。"""
    grouped: dict[UUID, list[WorkorderDisposition]] = {
        workorder_id: [] for workorder_id in workorder_ids
    }
    if not workorder_ids:
        return grouped
    rows = (
        await session.execute(
            select(WorkorderDisposition)
            .where(WorkorderDisposition.workorder_id.in_(workorder_ids))
            .order_by(WorkorderDisposition.role)
        )
    ).scalars().all()
    for row in rows:
        grouped[row.workorder_id].append(row)
    return grouped


async def get_work_order(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    role: str,
    account_id: UUID,
) -> Workorder:
    """按角色可见范围加载单条工单；范围外/未绑定家属一律 404（不暴露存在性）。"""
    workorder = await _load_work_order(session, workorder_id)
    if role == AccountRole.FAMILY:
        await _assert_family_link(session, account_id, workorder.elder_id)
        return workorder
    visible = visible_elder_ids(role, account_id)
    if visible is not None:
        allowed = (
            await session.execute(
                select(Workorder.workorder_id).where(
                    Workorder.workorder_id == workorder_id,
                    Workorder.elder_id.in_(visible),
                )
            )
        ).scalar_one_or_none()
        if allowed is None:
            raise WorkOrderNotFound("工单不存在")
    return workorder


def _mine_conditions(role: str, account_id: UUID, mine: str | None) -> list[Any]:
    """构造 ``mine`` 筛选条件（仅 staff 语义，非法值/非 staff 角色 → 422）。

    - unread：工单未关闭 且 无本人回执 且 本人角色处理项未 handled；
    - read：工单未关闭 且 有本人回执 且 本人角色处理项未 handled；
    - handled：本人角色处理项已 handled（关闭工单保留，不因终态丢失）。

    unread/read 排除已关闭工单：取消不回写处理项状态，关闭工单的回执
    也写不进去（mark_read 409），不排除则永远卡在未读/已读，与
    ``count_open_by_role`` 口径一致。
    """
    if role not in STAFF_ROLES:
        raise WorkOrderValidationError("当前角色不支持 mine 筛选")

    role_unhandled = exists(
        select(WorkorderDisposition.disposition_id).where(
            WorkorderDisposition.workorder_id == Workorder.workorder_id,
            WorkorderDisposition.role == role,
            WorkorderDisposition.status != WorkorderDispositionStatus.HANDLED,
        )
    )
    role_handled = exists(
        select(WorkorderDisposition.disposition_id).where(
            WorkorderDisposition.workorder_id == Workorder.workorder_id,
            WorkorderDisposition.role == role,
            WorkorderDisposition.status == WorkorderDispositionStatus.HANDLED,
        )
    )
    own_receipt = exists(
        select(WorkorderRead.account_id).where(
            WorkorderRead.workorder_id == Workorder.workorder_id,
            WorkorderRead.account_id == account_id,
        )
    )
    open_only = Workorder.status.not_in(CLOSED_STATUSES)

    if mine == "unread":
        return [open_only, ~own_receipt, role_unhandled]
    if mine == "read":
        return [open_only, own_receipt, role_unhandled]
    if mine == "handled":
        return [role_handled]
    raise WorkOrderValidationError("mine 仅支持 unread | read | handled")


async def list_work_orders(
    session: AsyncSession,
    *,
    role: str,
    account_id: UUID,
    elder_id: UUID | None = None,
    q: str | None = None,
    status: str | None = None,
    mine: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Workorder], int]:
    """按角色可见范围返回工单列表与总数；q 匹配长者姓名/标题/类别/触发原句/方案全文。

    ``mine`` 仅 staff 语义（unread=本人未读且角色未 handled / read=本人已读且
    角色未 handled / handled=角色已处理）；其他值抛 422（上层映射）。
    """
    conditions: list[Any] = []

    if role == AccountRole.FAMILY:
        if elder_id is None:
            raise WorkOrderNotFound("工单不存在")
        await _assert_family_link(session, account_id, elder_id)
    else:
        visible = visible_elder_ids(role, account_id)
        if visible is not None:
            conditions.append(Workorder.elder_id.in_(visible))

    if elder_id is not None:
        conditions.append(Workorder.elder_id == elder_id)
    if status and status in set(WorkorderStatus):
        conditions.append(Workorder.status == status)
    if mine is not None:
        conditions.extend(_mine_conditions(role, account_id, mine))

    keyword = (q or "").strip()
    if keyword:
        pattern = _like_pattern(keyword)
        elder_name_match = exists(
            select(Elder.elder_id).where(
                Elder.elder_id == Workorder.elder_id,
                Elder.name.ilike(pattern, escape="\\"),
            )
        )
        plan_match = exists(
            select(WorkorderDisposition.disposition_id).where(
                WorkorderDisposition.workorder_id == Workorder.workorder_id,
                WorkorderDisposition.plan_text.ilike(pattern, escape="\\"),
            )
        )
        conditions.append(
            or_(
                Workorder.title.ilike(pattern, escape="\\"),
                Workorder.sensitive_category.ilike(pattern, escape="\\"),
                Workorder.description.ilike(pattern, escape="\\"),
                elder_name_match,
                plan_match,
            )
        )

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(Workorder).where(*conditions)
            )
        ).scalar_one()
    )

    sort_key = sort if sort in ("created_at", "priority") else "created_at"
    desc = order != "asc"
    if sort_key == "priority":
        priority_rank = case(
            (Workorder.priority == WorkorderPriority.URGENT, 4),
            (Workorder.priority == WorkorderPriority.HIGH, 3),
            (Workorder.priority == WorkorderPriority.NORMAL, 2),
            else_=1,
        )
        order_by = [
            priority_rank.desc() if desc else priority_rank.asc(),
            Workorder.created_at.desc(),
        ]
    else:
        order_by = [Workorder.created_at.desc() if desc else Workorder.created_at.asc()]

    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))
    stmt = (
        select(Workorder)
        .where(*conditions)
        .order_by(*order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def count_open_by_role(
    session: AsyncSession, *, role: str, account_id: UUID
) -> int:
    """「该账号未读且该角色处理项未 handled 且工单未关闭」的数量。

    已读按账号：本人存在 workorder_reads 回执即不计入；非 staff 返回 0
    （admin/family 不发 counts）。
    """
    if role not in STAFF_ROLES:
        return 0
    account = await _load_account(session, account_id)
    _require_staff_role(account, role)

    conditions: list[Any] = [Workorder.status.not_in(CLOSED_STATUSES)]
    visible = visible_elder_ids(role, account_id)
    if visible is not None:
        conditions.append(Workorder.elder_id.in_(visible))
    conditions.append(
        exists(
            select(WorkorderDisposition.disposition_id).where(
                WorkorderDisposition.workorder_id == Workorder.workorder_id,
                WorkorderDisposition.role == role,
                WorkorderDisposition.status != WorkorderDispositionStatus.HANDLED,
            )
        )
    )
    conditions.append(
        ~exists(
            select(WorkorderRead.account_id).where(
                WorkorderRead.workorder_id == Workorder.workorder_id,
                WorkorderRead.account_id == account_id,
            )
        )
    )

    return int(
        (
            await session.execute(
                select(func.count()).select_from(Workorder).where(*conditions)
            )
        ).scalar_one()
    )


async def list_receipts(
    session: AsyncSession, *, account_id: UUID, workorder_ids: list[UUID]
) -> dict[UUID, datetime]:
    """批量加载某账号的已读回执时间，供列表/详情填充 is_read/read_at。"""
    receipts: dict[UUID, datetime] = {}
    if not workorder_ids:
        return receipts
    rows = (
        await session.execute(
            select(WorkorderRead.workorder_id, WorkorderRead.read_at).where(
                WorkorderRead.account_id == account_id,
                WorkorderRead.workorder_id.in_(workorder_ids),
            )
        )
    ).all()
    for workorder_id, read_at in rows:
        receipts[workorder_id] = read_at
    return receipts


# ---- 上下文快照 ----


def build_context_snapshot(
    messages: list[dict[str, Any]],
    trigger_text: str,
    assistant_reply: str | None,
    max_turns: int = SNAPSHOT_MAX_TURNS,
) -> list[dict[str, Any]]:
    """取触发前最近 N 轮（最多 2N 条 user/assistant）+ 触发句 + 首条安全回复。

    不足 N 轮按实际返回；不补造内容、不含音频。
    """
    turns = max(1, int(max_turns or SNAPSHOT_MAX_TURNS))
    history = [
        message
        for message in (messages or [])
        if message.get("role") in ("user", "assistant")
    ][-(turns * 2) :]

    # 历史消息缺时间戳时用触发时刻兜底，保证快照每项都有 at
    trigger_at = _iso(history[-1].get("created_at")) if history else None
    trigger_at = trigger_at or _now().isoformat()
    snapshot = [
        {
            "role": message.get("role"),
            "content": message.get("content") or "",
            "at": _iso(message.get("created_at")) or trigger_at,
        }
        for message in history
    ]
    snapshot.append({"role": "user", "content": trigger_text, "at": trigger_at})
    if assistant_reply:
        snapshot.append(
            {"role": "assistant", "content": assistant_reply, "at": trigger_at}
        )
    return snapshot
