"""用药计划领域服务。

职责：
- 计划 CRUD（仅绑定医生可写）与状态机；
- 确认后创建长期用药工单，并把计划与工单双向关联；
- 修改生效中的计划时写 revised 事件（原值→新值），供家属修改记录；
- 家属只读列表与修改记录（字段白名单，internal_note 整条不出）。

所有函数只写库/返回 ORM 对象，不自行推送 WS；推送由调用方在 commit 之后完成。
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.logging import get_logger
from app.models import Elder, MedicationPlan, Workorder, WorkorderEvent
from app.models.enums import AccountRole, MedicationPlanStatus
from app.schemas.medication import (
    FamilyPlanHistoryItem,
    MedicationPlanCreate,
    MedicationPlanUpdate,
)
from app.services import workorder as workorder_service
from app.services.authorization import AccessDecision, can_access_elder
from app.services.medication_order import cancel_orders_for_plan

logger = get_logger(__name__)

EDITABLE_FIELDS = (
    "drug_name",
    "dose",
    "schedule",
    "instructions",
    "internal_note",
    "start_date",
    "end_date",
)
# 家属修改记录可见字段：internal_note 永不透出
FAMILY_VISIBLE_CHANGE_FIELDS = frozenset(EDITABLE_FIELDS) - {"internal_note"}
REVISABLE_STATUSES = (
    MedicationPlanStatus.PLANNED,
    MedicationPlanStatus.ACTIVE,
    MedicationPlanStatus.PAUSED,
)
NON_DRAFT_STATUSES = (
    MedicationPlanStatus.ACTIVE,
    MedicationPlanStatus.PAUSED,
    MedicationPlanStatus.CANCELLED,
)


class MedicationPlanError(Exception):
    """用药计划领域异常基类。"""


class MedicationPlanNotFound(MedicationPlanError):
    """计划不存在或超出当前身份可见范围。"""


class MedicationPlanForbidden(MedicationPlanError):
    """角色无写权限。"""


class MedicationPlanConflict(MedicationPlanError):
    """当前状态不允许该操作。"""


class MedicationPlanValidationError(MedicationPlanError):
    """日期/字段组合不合法。"""


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


async def _load_elder(session: AsyncSession, elder_id: uuid.UUID) -> Elder:
    elder = await session.get(Elder, elder_id)
    if elder is None:
        raise MedicationPlanNotFound("长者不存在")
    return elder


async def _ensure_doctor(session: AsyncSession, actor: UserIdentity, elder: Elder) -> None:
    """仅绑定医生可写：角色错 403，未绑定/过期按 404 隐藏存在性。"""
    if actor.role != AccountRole.DOCTOR:
        raise MedicationPlanForbidden("仅医生可写用药计划")
    decision = await can_access_elder(session, actor, elder)
    if decision != AccessDecision.ALLOWED:
        raise MedicationPlanNotFound("长者不存在")


async def _ensure_viewer(session: AsyncSession, actor: UserIdentity, elder: Elder) -> None:
    decision = await can_access_elder(session, actor, elder)
    if decision == AccessDecision.FORBIDDEN:
        raise MedicationPlanForbidden("无权限查看该长者的用药计划")
    if decision != AccessDecision.ALLOWED:
        raise MedicationPlanNotFound("长者不存在")


async def _load_plan(session: AsyncSession, plan_id: uuid.UUID) -> MedicationPlan:
    plan = await session.get(MedicationPlan, plan_id)
    if plan is None:
        raise MedicationPlanNotFound("用药计划不存在")
    return plan


def _validate_merged_dates(start: date, end: date | None) -> None:
    if end is not None and end < start:
        raise MedicationPlanValidationError("end_date 不能早于 start_date")


async def create_plan(
    session: AsyncSession,
    *,
    actor: UserIdentity,
    elder_id: uuid.UUID,
    payload: MedicationPlanCreate,
) -> MedicationPlan:
    elder = await _load_elder(session, elder_id)
    await _ensure_doctor(session, actor, elder)
    _validate_merged_dates(payload.start_date, payload.end_date)

    plan = MedicationPlan(
        elder_id=elder_id,
        drug_name=payload.drug_name,
        dose=payload.dose,
        schedule=payload.schedule.model_dump(),
        instructions=payload.instructions,
        internal_note=payload.internal_note,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status=MedicationPlanStatus.PLANNED,
        created_by_account_id=actor.account_id,
        updated_by_account_id=actor.account_id,
    )
    session.add(plan)
    await session.flush()
    logger.info(
        "用药计划草稿已创建",
        extra={
            "event": "medication_plan_created",
            "object_type": "medication_plan",
            "object_id": str(plan.plan_id),
            "elder_id": str(elder_id),
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
            "status": plan.status,
        },
    )
    return plan


async def list_plans(
    session: AsyncSession,
    *,
    actor: UserIdentity,
    elder_id: uuid.UUID,
    status: str | None = None,
) -> list[MedicationPlan]:
    elder = await _load_elder(session, elder_id)
    await _ensure_viewer(session, actor, elder)

    stmt = select(MedicationPlan).where(MedicationPlan.elder_id == elder_id)
    if actor.role == AccountRole.NURSE:
        # 护工看不到医生草稿（planned）
        stmt = stmt.where(MedicationPlan.status != MedicationPlanStatus.PLANNED)
    if status is not None:
        stmt = stmt.where(MedicationPlan.status == status)
    stmt = stmt.order_by(MedicationPlan.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_plan(
    session: AsyncSession, *, actor: UserIdentity, plan_id: uuid.UUID
) -> MedicationPlan:
    plan = await _load_plan(session, plan_id)
    elder = await _load_elder(session, plan.elder_id)
    await _ensure_viewer(session, actor, elder)
    if actor.role == AccountRole.NURSE and plan.status == MedicationPlanStatus.PLANNED:
        raise MedicationPlanNotFound("用药计划不存在")
    return plan


async def _next_plan_version(
    session: AsyncSession, workorder_id: uuid.UUID
) -> int:
    payloads = (
        await session.execute(
            select(WorkorderEvent.payload).where(
                WorkorderEvent.workorder_id == workorder_id,
                WorkorderEvent.event_type.in_(("confirmed", "revised")),
            )
        )
    ).scalars().all()
    versions = [
        int(payload["plan_version"])
        for payload in payloads
        if isinstance(payload, dict) and str(payload.get("plan_version", "")).isdigit()
    ]
    return (max(versions) if versions else 0) + 1


async def update_plan(
    session: AsyncSession,
    *,
    actor: UserIdentity,
    plan_id: uuid.UUID,
    payload: MedicationPlanUpdate,
) -> tuple[MedicationPlan, int | None, list[str]]:
    """返回 (计划, 新版本号, 变更字段)；无实际变更时版本号为 None。"""
    plan = await _load_plan(session, plan_id)
    elder = await _load_elder(session, plan.elder_id)
    await _ensure_doctor(session, actor, elder)
    if plan.status not in REVISABLE_STATUSES:
        raise MedicationPlanConflict("当前状态的用药计划不可修改")

    data = payload.model_dump(exclude_unset=True)
    if any(
        field in data and data[field] is None
        for field in ("drug_name", "dose", "schedule", "start_date")
    ):
        raise MedicationPlanValidationError("必填字段不可置空")
    if "schedule" in data and data["schedule"] is not None:
        data["schedule"] = payload.schedule.model_dump() if payload.schedule else None
    _validate_merged_dates(
        data.get("start_date", plan.start_date),
        data.get("end_date", plan.end_date),
    )

    changes: list[dict[str, Any]] = []
    for field in EDITABLE_FIELDS:
        if field not in data:
            continue
        before = getattr(plan, field)
        after = data[field]
        if before == after:
            continue
        if field == "internal_note":
            # 内部备注只暴露"有变化"，不记录值
            changes.append({"field": "internal_note", "changed": True})
        else:
            changes.append(
                {"field": field, "before": _jsonable(before), "after": _jsonable(after)}
            )
        setattr(plan, field, after)

    if not changes:
        return plan, None, []

    plan.updated_by_account_id = actor.account_id
    version: int | None = None
    if plan.workorder_id is not None:
        version = await _next_plan_version(session, plan.workorder_id)
        session.add(
            WorkorderEvent(
                workorder_id=plan.workorder_id,
                event_type="revised",
                actor_account_id=actor.account_id,
                actor_role=actor.role,
                payload={"plan_version": version, "changes": changes},
            )
        )
    await session.flush()
    logger.info(
        "用药计划已修改",
        extra={
            "event": "medication_plan_updated",
            "object_type": "medication_plan",
            "object_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
            "changed_fields": [item["field"] for item in changes],
        },
    )
    return plan, version, [item["field"] for item in changes]


async def confirm_plan(
    session: AsyncSession, *, actor: UserIdentity, plan_id: uuid.UUID
) -> tuple[MedicationPlan, Workorder]:
    """planned → active；生成一张长期工单并写入 confirmed 事件。"""
    plan = await _load_plan(session, plan_id)
    elder = await _load_elder(session, plan.elder_id)
    await _ensure_doctor(session, actor, elder)
    if plan.status != MedicationPlanStatus.PLANNED:
        raise MedicationPlanConflict("只有草稿状态的用药计划可以确认")

    plan.status = MedicationPlanStatus.ACTIVE
    plan.confirmed_at = datetime.now(UTC)
    plan.confirmed_by_account_id = actor.account_id
    plan.updated_by_account_id = actor.account_id
    await session.flush()

    workorder, _ = await workorder_service.create_medication_plan_work_order(
        session, plan=plan, doctor_account_id=actor.account_id
    )
    plan.workorder_id = workorder.workorder_id
    session.add(
        WorkorderEvent(
            workorder_id=workorder.workorder_id,
            event_type="confirmed",
            actor_account_id=actor.account_id,
            actor_role=actor.role,
            payload={
                "plan_version": 1,
                "workorder_id": str(workorder.workorder_id),
            },
        )
    )
    await session.flush()
    logger.info(
        "用药计划已确认",
        extra={
            "event": "medication_plan_confirmed",
            "object_type": "medication_plan",
            "object_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
            "workorder_id": str(workorder.workorder_id),
        },
    )
    return plan, workorder


async def pause_plan(
    session: AsyncSession, *, actor: UserIdentity, plan_id: uuid.UUID
) -> MedicationPlan:
    plan = await _load_plan(session, plan_id)
    elder = await _load_elder(session, plan.elder_id)
    await _ensure_doctor(session, actor, elder)
    if plan.status not in (MedicationPlanStatus.PLANNED, MedicationPlanStatus.ACTIVE):
        raise MedicationPlanConflict("当前状态的用药计划不可停用")
    plan.status = MedicationPlanStatus.PAUSED
    plan.updated_by_account_id = actor.account_id
    await session.flush()
    logger.info(
        "用药计划已停用",
        extra={
            "event": "medication_plan_paused",
            "object_type": "medication_plan",
            "object_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
        },
    )
    return plan


async def resume_plan(
    session: AsyncSession, *, actor: UserIdentity, plan_id: uuid.UUID
) -> MedicationPlan:
    plan = await _load_plan(session, plan_id)
    elder = await _load_elder(session, plan.elder_id)
    await _ensure_doctor(session, actor, elder)
    if plan.status != MedicationPlanStatus.PAUSED:
        raise MedicationPlanConflict("只有已停用的用药计划可以恢复")
    plan.status = MedicationPlanStatus.ACTIVE
    plan.updated_by_account_id = actor.account_id
    await session.flush()
    logger.info(
        "用药计划已恢复",
        extra={
            "event": "medication_plan_resumed",
            "object_type": "medication_plan",
            "object_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
        },
    )
    return plan


async def cancel_plan(
    session: AsyncSession, *, actor: UserIdentity, plan_id: uuid.UUID
) -> tuple[MedicationPlan, Workorder | None]:
    """取消计划：取消今后任务单，并由系统关闭长期工单（绕过 handled 门禁）。"""
    plan = await _load_plan(session, plan_id)
    elder = await _load_elder(session, plan.elder_id)
    await _ensure_doctor(session, actor, elder)
    if plan.status not in REVISABLE_STATUSES:
        raise MedicationPlanConflict("当前状态的用药计划不可取消")

    plan.status = MedicationPlanStatus.CANCELLED
    plan.updated_by_account_id = actor.account_id
    await cancel_orders_for_plan(session, plan_id=plan.plan_id)
    workorder = await workorder_service.close_standing_work_order_for_plan(
        session,
        plan_id=plan.plan_id,
        actor_account_id=actor.account_id,
        reason="用药计划已取消，长期工单自动关闭",
    )
    await session.flush()
    logger.info(
        "用药计划已取消",
        extra={
            "event": "medication_plan_cancelled",
            "object_type": "medication_plan",
            "object_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
        },
    )
    return plan, workorder


async def list_family_plans(
    session: AsyncSession,
    *,
    actor: UserIdentity,
    elder_id: uuid.UUID,
    status: str | None = None,
) -> list[MedicationPlan]:
    elder = await _load_elder(session, elder_id)
    await _ensure_viewer(session, actor, elder)
    # 家属只读：草稿不可见；请求 planned 时返回空列表
    if status == MedicationPlanStatus.PLANNED:
        return []
    stmt = select(MedicationPlan).where(
        MedicationPlan.elder_id == elder_id,
        MedicationPlan.status.in_(NON_DRAFT_STATUSES),
    )
    if status is not None:
        stmt = stmt.where(MedicationPlan.status == status)
    stmt = stmt.order_by(MedicationPlan.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


def _sanitize_changes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if field not in FAMILY_VISIBLE_CHANGE_FIELDS:
            continue
        if "before" in item or "after" in item:
            sanitized.append(
                {
                    "field": field,
                    "before": item.get("before"),
                    "after": item.get("after"),
                }
            )
    return sanitized


async def family_plan_history(
    session: AsyncSession,
    *,
    actor: UserIdentity,
    elder_id: uuid.UUID,
    plan_id: uuid.UUID,
) -> list[FamilyPlanHistoryItem]:
    elder = await _load_elder(session, elder_id)
    await _ensure_viewer(session, actor, elder)
    plan = await _load_plan(session, plan_id)
    if plan.elder_id != elder_id or plan.status == MedicationPlanStatus.PLANNED:
        raise MedicationPlanNotFound("用药计划不存在")
    if plan.workorder_id is None:
        return []

    events = (
        await session.execute(
            select(WorkorderEvent)
            .where(
                WorkorderEvent.workorder_id == plan.workorder_id,
                WorkorderEvent.event_type.in_(("confirmed", "revised")),
            )
            .order_by(WorkorderEvent.created_at.asc())
        )
    ).scalars().all()

    items: list[FamilyPlanHistoryItem] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        raw_version = payload.get("plan_version")
        version = int(raw_version) if str(raw_version).isdigit() else None
        changes = (
            []
            if event.event_type == "confirmed"
            else _sanitize_changes(payload.get("changes"))
        )
        items.append(
            FamilyPlanHistoryItem(
                time=event.created_at,
                actor=event.actor_role,
                plan_version=version,
                changes=changes,
            )
        )
    # 同一事务内 created_at 可能相同，按计划版本兜底排序（1=确认，2+=修改）
    items.sort(key=lambda item: (item.plan_version or 0, item.time))
    return items


def family_view(plan: MedicationPlan) -> dict[str, Any]:
    """家属字段白名单：永不含 internal_note / 账号 UUID / workorder_id。"""
    return {
        "plan_id": plan.plan_id,
        "elder_id": plan.elder_id,
        "drug_name": plan.drug_name,
        "dose": plan.dose,
        "schedule": plan.schedule,
        "instructions": plan.instructions,
        "start_date": plan.start_date,
        "end_date": plan.end_date,
        "status": plan.status,
        "confirmed_at": plan.confirmed_at,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }
