"""健康采集事件服务（Day8 段1）。

职责：
- 一天一位长者一条事件（``health:{elder_id}:{YYYY-MM-DD}``），同日重入只更新；
- 合并本次答案 → 规则分级 → 调决策图（绿入库/黄建单/红建单+转诊）；
- 看板查询复用工单的可见范围口径（``visible_elder_ids``）；
- 人工决策（confirm/escalate/reject）以数据库状态为准，重复决策 409。

边界：不诊断、不给用药建议、不替代 SOS；日志只记 id/level/status。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.config import get_settings
from app.core.logging import get_logger
from app.graphs.health import HealthOutcome, describe_event, run_health_decision
from app.models import (
    Elder,
    HealthEvent,
    Room,
    Workorder,
    WorkorderDisposition,
    WorkorderEvent,
)
from app.models.enums import (
    AccountRole,
    HealthEventLevel,
    HealthEventSource,
    HealthEventStatus,
    WorkorderDispositionStatus,
    WorkorderPriority,
    WorkorderStatus,
)
from app.services import health_triage
from app.services.workorder import visible_elder_ids

logger = get_logger(__name__)

LEVEL_RANK = health_triage.LEVEL_RANK
ACTION_CONFIRM = "confirm"
ACTION_ESCALATE = "escalate"
ACTION_REJECT = "reject"
DECISION_ACTIONS = (ACTION_CONFIRM, ACTION_ESCALATE, ACTION_REJECT)

# 谁能处理哪一级：黄色护工/医生/管理员，红色医生/管理员
ALLOWED_DECIDERS: dict[str, frozenset[str]] = {
    HealthEventLevel.YELLOW: frozenset(
        {AccountRole.NURSE, AccountRole.DOCTOR, AccountRole.ADMIN}
    ),
    HealthEventLevel.RED: frozenset({AccountRole.DOCTOR, AccountRole.ADMIN}),
}

_WAITING_REPLY = {
    HealthEventStatus.PENDING_CONFIRM: "今天的情况已经告诉工作人员了，他们会来看看您。",
    HealthEventStatus.CONFIRMED: "已经记下了，工作人员会继续跟进。",
    HealthEventStatus.ESCALATED: "已经升级处理了，工作人员马上就来。",
    HealthEventStatus.ARCHIVED: "今天的情况我记下了，您好好休息。",
    HealthEventStatus.COLLECTING: "我再问您几个小问题。",
}


class HealthEventError(Exception):
    """健康事件领域异常基类。"""


class HealthEventNotFound(HealthEventError):
    """事件不存在或不在可见范围（上层映射 404）。"""


class HealthEventConflict(HealthEventError):
    """事件已决策（上层映射 409）。"""


class HealthEventForbidden(HealthEventError):
    """角色无权处理该级别（上层映射 403）。"""


@dataclass(frozen=True)
class IntakeResult:
    event: HealthEvent
    reply: str
    missing_fields: tuple[str, ...]
    required_role: str | None
    workorder_id: str | None
    referral_id: str | None
    workorder_created: bool = False


def _app_zone() -> ZoneInfo:
    return ZoneInfo(get_settings().app_timezone)


def thread_id_for(
    elder_id: uuid.UUID | str, day: Any | None = None
) -> str:
    """同日同长者固定一个线程；测试可显式传日期。"""
    local_day = day or datetime.now(_app_zone()).date()
    return f"health:{elder_id}:{local_day.isoformat()}"


def required_role_for(level: str) -> str | None:
    if level == HealthEventLevel.RED:
        return AccountRole.DOCTOR
    if level == HealthEventLevel.YELLOW:
        return AccountRole.NURSE
    return None


def event_summary(event: HealthEvent) -> str:
    """看板摘要：结构化数值，最多 60 字。"""
    return describe_event(event)[:60]


def _merge_reasons(existing: list[str], incoming: tuple[str, ...]) -> list[str]:
    merged = list(existing)
    for item in incoming:
        if item not in merged:
            merged.append(item)
    return merged


async def _by_thread(session: AsyncSession, thread_id: str) -> HealthEvent | None:
    return (
        await session.execute(
            select(HealthEvent).where(HealthEvent.thread_id == thread_id)
        )
    ).scalar_one_or_none()


async def _by_id(session: AsyncSession, event_id: uuid.UUID | str) -> HealthEvent:
    try:
        uid = uuid.UUID(str(event_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HealthEventNotFound("健康事件不存在") from exc
    event = await session.get(HealthEvent, uid)
    if event is None:
        raise HealthEventNotFound("健康事件不存在")
    return event


def _normalize_source(source: str) -> str:
    allowed = {item.value for item in HealthEventSource}
    return source if source in allowed else HealthEventSource.MANUAL.value


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


async def submit_intake(
    session: AsyncSession,
    *,
    elder_id: uuid.UUID,
    source: str = HealthEventSource.MANUAL.value,
    bp_systolic: object = None,
    bp_diastolic: object = None,
    sleep_minutes: object = None,
    mood: object = None,
    chief_complaint: object = None,
    now: datetime | None = None,
) -> IntakeResult:
    """提交一次采集：缺字段继续追问；集齐或出现红色信号即执行决策。"""
    source = _normalize_source(source)
    reference = (now or datetime.now(UTC)).astimezone(_app_zone())
    thread_id = thread_id_for(elder_id, reference.date())

    event = await _by_thread(session, thread_id)
    if event is None:
        event = HealthEvent(
            thread_id=thread_id,
            elder_id=elder_id,
            level=HealthEventLevel.GREEN,
            status=HealthEventStatus.COLLECTING,
        )
        session.add(event)
        await session.flush()

    previous_level = str(event.level)
    current_status = str(event.status)

    # 合并本次答案：None 不覆盖已采集值（多轮追问靠后一轮补全）
    systolic = _as_int(bp_systolic)
    diastolic = _as_int(bp_diastolic)
    sleep = _as_int(sleep_minutes)
    mood_text = _as_text(mood, 24)
    complaint = _as_text(chief_complaint, 500)
    if systolic is not None:
        event.bp_systolic = systolic
    if diastolic is not None:
        event.bp_diastolic = diastolic
    if sleep is not None:
        event.sleep_minutes = sleep
    if mood_text is not None:
        event.mood = mood_text
    if complaint is not None:
        event.chief_complaint = complaint
    await session.flush()

    triage = health_triage.triage(
        bp_systolic=event.bp_systolic,
        bp_diastolic=event.bp_diastolic,
        sleep_minutes=event.sleep_minutes,
        mood=event.mood,
        chief_complaint=event.chief_complaint,
    )
    merged_level = max(
        (previous_level, triage.level), key=lambda level: LEVEL_RANK.get(level, 0)
    )
    event.level = merged_level
    event.reasons = _merge_reasons(event.reason_list(), triage.reasons)

    should_decide = False
    if current_status == HealthEventStatus.COLLECTING:
        should_decide = not triage.needs_followup
    elif current_status == HealthEventStatus.ARCHIVED:
        should_decide = triage.level in (HealthEventLevel.YELLOW, HealthEventLevel.RED)
    else:  # pending_confirm / confirmed / escalated：只有升级才重新处理
        should_decide = LEVEL_RANK.get(triage.level, 0) > LEVEL_RANK.get(
            previous_level, 0
        )

    if not should_decide:
        await session.flush()
        reply = (
            triage.advice
            if current_status == HealthEventStatus.COLLECTING
            else _WAITING_REPLY.get(current_status, triage.advice)
        )
        return IntakeResult(
            event=event,
            reply=reply,
            missing_fields=(
                triage.missing_fields
                if current_status == HealthEventStatus.COLLECTING
                else ()
            ),
            required_role=required_role_for(str(event.level)),
            workorder_id=str(event.workorder_id) if event.workorder_id else None,
            referral_id=event.referral_id,
        )

    outcome: HealthOutcome = await run_health_decision(
        session,
        event=event,
        triage=triage,
        record_health=current_status
        in (HealthEventStatus.COLLECTING, HealthEventStatus.ARCHIVED),
        source=source,
    )
    await session.flush()
    logger.info(
        "健康事件已分级",
        extra={
            "event": "health_event_decided",
            "object_type": "health_event",
            "object_id": str(event.event_id),
            "elder_id": str(event.elder_id),
            "level": str(event.level),
            "status": str(event.status),
            "source": source,
        },
    )
    return IntakeResult(
        event=event,
        reply=outcome.reply,
        missing_fields=(),
        required_role=outcome.required_role or required_role_for(str(event.level)),
        workorder_id=outcome.workorder_id,
        referral_id=outcome.referral_id,
        workorder_created=outcome.workorder_created,
    )


async def _visible(
    session: AsyncSession, user: UserIdentity, event: HealthEvent
) -> None:
    visible = visible_elder_ids(user.role, user.account_id)
    if visible is None:
        return
    found = await session.scalar(
        select(HealthEvent.event_id).where(
            HealthEvent.event_id == event.event_id,
            HealthEvent.elder_id.in_(visible),
        )
    )
    if found is None:
        raise HealthEventNotFound("健康事件不存在")


def _room_label(
    building_code: str | None, room_number: str | None, bed_no: int | None
) -> str | None:
    if not building_code or not room_number:
        return None
    return f"{building_code}栋{room_number}房{bed_no or ''}床"


async def list_pending(
    session: AsyncSession,
    *,
    role: str,
    account_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """待确认事件分页：护工按楼栋、医生按关联长者、管理员全量。"""
    visible = visible_elder_ids(role, account_id)
    conditions = [HealthEvent.status == HealthEventStatus.PENDING_CONFIRM]
    if visible is not None:
        conditions.append(HealthEvent.elder_id.in_(visible))

    total = (
        await session.scalar(
            select(func.count()).select_from(HealthEvent).where(*conditions)
        )
    ) or 0
    rows = (
        await session.execute(
            select(HealthEvent, Elder.name, Room.building_code, Room.room_number, Elder.bed_no)
            .join(Elder, Elder.elder_id == HealthEvent.elder_id)
            .outerjoin(Room, Room.room_id == Elder.room_id)
            .where(*conditions)
            .order_by(HealthEvent.created_at.desc())
            .offset(max(0, page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = [
        {
            "event_id": str(event.event_id),
            "elder_id": str(event.elder_id),
            "elder_name": name,
            "room_label": _room_label(building, room_number, bed_no),
            "level": str(event.level),
            "status": str(event.status),
            "summary": event_summary(event),
            "required_role": required_role_for(str(event.level)),
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
        for event, name, building, room_number, bed_no in rows
    ]
    return items, int(total)


async def get_event_detail(
    session: AsyncSession, *, event_id: uuid.UUID | str, user: UserIdentity
) -> dict[str, Any]:
    event = await _by_id(session, event_id)
    await _visible(session, user, event)
    elder = await session.get(Elder, event.elder_id)
    room = (
        await session.get(Room, elder.room_id)
        if elder is not None and elder.room_id is not None
        else None
    )
    return {
        "event_id": str(event.event_id),
        "thread_id": event.thread_id,
        "elder_id": str(event.elder_id),
        "elder_name": elder.name if elder is not None else None,
        "room_label": _room_label(
            room.building_code if room else None,
            room.room_number if room else None,
            elder.bed_no if elder else None,
        ),
        "level": str(event.level),
        "status": str(event.status),
        "bp_systolic": event.bp_systolic,
        "bp_diastolic": event.bp_diastolic,
        "sleep_minutes": event.sleep_minutes,
        "mood": event.mood,
        "chief_complaint": event.chief_complaint,
        "reasons": event.reason_list(),
        "required_role": required_role_for(str(event.level)),
        "workorder_id": str(event.workorder_id) if event.workorder_id else None,
        "referral_id": event.referral_id,
        "decided_by": str(event.decided_by) if event.decided_by else None,
        "decided_at": event.decided_at.isoformat() if event.decided_at else None,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


async def _disposition(
    session: AsyncSession, workorder_id: uuid.UUID, role: str
) -> WorkorderDisposition | None:
    return (
        await session.execute(
            select(WorkorderDisposition).where(
                WorkorderDisposition.workorder_id == workorder_id,
                WorkorderDisposition.role == role,
            )
        )
    ).scalar_one_or_none()


async def decide(
    session: AsyncSession,
    *,
    event_id: uuid.UUID | str,
    action: str,
    user: UserIdentity,
    note: str | None = None,
) -> HealthEvent:
    """人工确认/升级/驳回；状态以 DB 为准，重复决策冲突。"""
    if action not in DECISION_ACTIONS:
        raise HealthEventConflict("不支持的处理动作")
    event = await _by_id(session, event_id)
    await _visible(session, user, event)
    if event.status != HealthEventStatus.PENDING_CONFIRM:
        raise HealthEventConflict("健康事件已处理")

    allowed = ALLOWED_DECIDERS.get(str(event.level))
    if allowed is not None and user.role not in allowed:
        raise HealthEventForbidden("当前角色无权处理该级别")

    now = datetime.now(UTC)
    workorder: Workorder | None = (
        await session.get(Workorder, event.workorder_id)
        if event.workorder_id is not None
        else None
    )

    if action == ACTION_CONFIRM:
        event.status = HealthEventStatus.CONFIRMED
        if workorder is not None:
            workorder.status = WorkorderStatus.IN_PROGRESS
    elif action == ACTION_ESCALATE:
        event.status = HealthEventStatus.ESCALATED
        if workorder is not None:
            workorder.status = WorkorderStatus.OPEN
            workorder.priority = WorkorderPriority.URGENT
    else:  # reject：判定误报，归档并关闭工单
        event.status = HealthEventStatus.ARCHIVED
        if workorder is not None:
            workorder.status = WorkorderStatus.CANCELLED
            workorder.resolved_at = now
            workorder.closed_by = user.account_id
            workorder.closed_note = note

    event.decided_by = user.account_id
    event.decided_at = now

    if workorder is not None:
        if user.role in (AccountRole.NURSE, AccountRole.DOCTOR):
            disposition = await _disposition(session, workorder.workorder_id, user.role)
            if disposition is not None:
                disposition.status = WorkorderDispositionStatus.HANDLED
                disposition.account_id = user.account_id
                disposition.handled_at = now
        session.add(
            WorkorderEvent(
                workorder_id=workorder.workorder_id,
                event_type="closed" if action == ACTION_REJECT else "disposition",
                actor_account_id=user.account_id,
                actor_role=user.role,
                payload={
                    "action": action,
                    "note": note,
                    "health_event_id": str(event.event_id),
                },
            )
        )
    await session.flush()
    logger.info(
        "健康事件人工决策",
        extra={
            "event": "health_event_decision",
            "object_type": "health_event",
            "object_id": str(event.event_id),
            "action": action,
            "level": str(event.level),
            "status": str(event.status),
            "actor_role": user.role,
        },
    )
    return event


__all__ = [
    "ALLOWED_DECIDERS",
    "DECISION_ACTIONS",
    "HealthEventConflict",
    "HealthEventError",
    "HealthEventForbidden",
    "HealthEventNotFound",
    "IntakeResult",
    "decide",
    "event_summary",
    "get_event_detail",
    "list_pending",
    "required_role_for",
    "submit_intake",
    "thread_id_for",
]
