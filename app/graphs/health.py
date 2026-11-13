"""健康事件决策图（Day8 段1）。

结构::

    START ──▶ evaluate ──green──▶ record_green ──▶ END
                    ├──yellow──▶ open_yellow ──▶ END
                    └──red─────▶ open_red ─────▶ END

约定（沿用 ``app/graphs/medication.py``）：
- 不使用 ``interrupt`` / ``Command(resume=...)``：人工确认走 HTTP decision 接口，
  ``health_events.status`` 是权威状态；图只做一次判定，DB 负责跨请求恢复；
- 图按请求构造，``AsyncSession`` 由闭包注入节点，不持有全局会话；
- 绿色写 ``health_records`` 并归档；黄色建 ``health_alert`` 工单等护工确认；
  红色建紧急工单 + 医院转诊（默认 mock）等医生确认；
- LangGraph 未安装时退化为同语义的直接分派，行为一致。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import HealthEvent, HealthRecord, Workorder
from app.models.enums import HealthEventLevel, HealthEventStatus, WorkorderStatus
from app.services import hospital as hospital_service
from app.services import workorder as workorder_service
from app.services.health_triage import GREEN, RED, YELLOW, TriageResult

try:  # LangGraph 是可选运行依赖：未安装时走直接分派
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时走降级
    END = START = None  # type: ignore[assignment]
    StateGraph = None  # type: ignore[assignment]
    LANGGRAPH_AVAILABLE = False

logger = get_logger(__name__)

GREEN_REPLY = "今天的情况我记下了，您好好休息。"
YELLOW_REPLY = "今天的数据我有点担心，已经告诉护工了，他们会来看看您。"
RED_REPLY = "这个情况我马上通知医生，您先坐着别动，我这就叫人。"

TITLE_YELLOW = "健康预警：需要留意"
TITLE_RED = "健康预警：需要医生确认"


class HealthGraphError(RuntimeError):
    """健康事件不存在或数据异常。"""


class HealthState(TypedDict, total=False):
    event_id: str
    level: str
    source: str
    record_health: bool
    status: str
    reply: str
    required_role: str | None
    workorder_id: str | None
    referral_id: str | None
    workorder_created: bool


@dataclass(frozen=True)
class HealthOutcome:
    status: str
    reply: str
    required_role: str | None
    workorder_id: str | None
    referral_id: str | None
    workorder_created: bool = False


def _event_uuid(state: HealthState) -> uuid.UUID:
    try:
        return uuid.UUID(str(state.get("event_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HealthGraphError("健康事件 id 非法") from exc


async def _load_event(session: AsyncSession, state: HealthState) -> HealthEvent:
    event = await session.get(HealthEvent, _event_uuid(state))
    if event is None:
        raise HealthGraphError("健康事件不存在")
    return event


def _record_health(session: AsyncSession, event: HealthEvent, state: HealthState) -> None:
    """黄色/红色同样入 health_records，供 14 天趋势使用。"""
    session.add(
        HealthRecord(
            elder_id=event.elder_id,
            recorded_at=datetime.now(UTC),
            bp_systolic=event.bp_systolic,
            bp_diastolic=event.bp_diastolic,
            sleep_minutes=event.sleep_minutes,
            mood=event.mood,
            chief_complaint=event.chief_complaint,
            source=str(state.get("source") or "manual")[:24],
        )
    )


def describe_event(event: HealthEvent) -> str:
    """工单描述：只放结构化采集值，不放大段对话。"""
    parts: list[str] = []
    if event.bp_systolic is not None or event.bp_diastolic is not None:
        parts.append(f"血压 {event.bp_systolic or '-'}/{event.bp_diastolic or '-'}")
    if event.sleep_minutes is not None:
        parts.append(f"睡眠 {event.sleep_minutes} 分钟")
    if event.mood:
        parts.append(f"情绪 {event.mood}")
    if event.chief_complaint:
        complaint = event.chief_complaint.strip()
        parts.append(f"主诉 {complaint[:60]}")
    return "；".join(parts) or "健康采集数据"


async def _node_green(session: AsyncSession, state: HealthState) -> dict[str, Any]:
    event = await _load_event(session, state)
    if state.get("record_health"):
        _record_health(session, event, state)
    event.status = HealthEventStatus.ARCHIVED
    event.level = HealthEventLevel.GREEN
    event.decided_at = datetime.now(UTC)
    return {
        "status": HealthEventStatus.ARCHIVED,
        "reply": GREEN_REPLY,
        "required_role": None,
        "workorder_id": None,
        "referral_id": None,
    }


async def _node_yellow(session: AsyncSession, state: HealthState) -> dict[str, Any]:
    event = await _load_event(session, state)
    if state.get("record_health"):
        # record_health=False（升级重开）时不落新体征记录：趋势/日报沿用已录数据口径
        _record_health(session, event, state)
    workorder, created = await workorder_service.create_health_alert_work_order(
        session,
        elder_id=event.elder_id,
        level=YELLOW,
        title=TITLE_YELLOW,
        description=describe_event(event),
        event_id=event.event_id,
    )
    event.level = HealthEventLevel.YELLOW
    event.status = HealthEventStatus.PENDING_CONFIRM
    event.workorder_id = workorder.workorder_id
    event.decided_at = None
    return {
        "status": HealthEventStatus.PENDING_CONFIRM,
        "reply": YELLOW_REPLY,
        "required_role": "nurse",
        "workorder_id": str(workorder.workorder_id),
        "referral_id": event.referral_id,
        "workorder_created": created,
    }


async def _node_red(session: AsyncSession, state: HealthState) -> dict[str, Any]:
    event = await _load_event(session, state)
    if state.get("record_health"):
        # record_health=False（升级重开）时不落新体征记录：趋势/日报沿用已录数据口径
        _record_health(session, event, state)
    # 黄→红升级重开时，关闭该事件遗留的 OPEN 黄色工单，避免看板出现两单且黄单无人处理
    prior_open = (
        await session.execute(
            select(Workorder).where(
                Workorder.dedupe_key == f"health_event:{event.event_id}:{YELLOW}",
                Workorder.status.in_(
                    (
                        WorkorderStatus.OPEN,
                        WorkorderStatus.ACKNOWLEDGED,
                        WorkorderStatus.IN_PROGRESS,
                    )
                ),
            )
        )
    ).scalars().all()
    for stale in prior_open:
        stale.status = WorkorderStatus.CANCELLED
        stale.resolved_at = datetime.now(UTC)
    workorder, created = await workorder_service.create_health_alert_work_order(
        session,
        elder_id=event.elder_id,
        level=RED,
        title=TITLE_RED,
        description=describe_event(event),
        event_id=event.event_id,
    )

    referral_id = event.referral_id
    if referral_id is None:
        adapter = hospital_service.get_hospital_adapter()
        try:
            referral = await adapter.create_referral(
                elder_id=event.elder_id,
                event_id=event.event_id,
                level=RED,
            )
            referral_id = referral.referral_id
        except hospital_service.HospitalUnavailable as exc:
            # 不静默失败：建单继续，转诊单号留空并记录降级
            logger.warning(
                "医院转诊失败，继续建单并等待医生确认",
                extra={
                    "event": "hospital_referral_degraded",
                    "object_type": "health_event",
                    "object_id": str(event.event_id),
                    "elder_id": str(event.elder_id),
                    "error": str(exc),
                },
            )

    event.level = HealthEventLevel.RED
    event.status = HealthEventStatus.PENDING_CONFIRM
    event.workorder_id = workorder.workorder_id
    event.referral_id = referral_id
    event.decided_at = None
    return {
        "status": HealthEventStatus.PENDING_CONFIRM,
        "reply": RED_REPLY,
        "required_role": "doctor",
        "workorder_id": str(workorder.workorder_id),
        "referral_id": referral_id,
        "workorder_created": created,
    }


async def _node_evaluate(session: AsyncSession, state: HealthState) -> dict[str, Any]:  # noqa: ARG001
    return {"status": "evaluated"}


_NODES = {
    GREEN: _node_green,
    YELLOW: _node_yellow,
    RED: _node_red,
}


def _build_graph(session: AsyncSession) -> Any:
    # 必须用 async def 包装：LangGraph 不会 await「返回协程的 lambda」
    async def evaluate(state: HealthState) -> dict[str, Any]:
        return await _node_evaluate(session, state)

    async def record_green(state: HealthState) -> dict[str, Any]:
        return await _node_green(session, state)

    async def open_yellow(state: HealthState) -> dict[str, Any]:
        return await _node_yellow(session, state)

    async def open_red(state: HealthState) -> dict[str, Any]:
        return await _node_red(session, state)

    graph = StateGraph(HealthState)
    graph.add_node("evaluate", evaluate)
    graph.add_node("record_green", record_green)
    graph.add_node("open_yellow", open_yellow)
    graph.add_node("open_red", open_red)
    graph.add_edge(START, "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        lambda state: state.get("level") or GREEN,
        {
            GREEN: "record_green",
            YELLOW: "open_yellow",
            RED: "open_red",
        },
    )
    graph.add_edge("record_green", END)
    graph.add_edge("open_yellow", END)
    graph.add_edge("open_red", END)
    return graph.compile()


async def run_health_decision(
    session: AsyncSession,
    *,
    event: HealthEvent,
    triage: TriageResult,
    record_health: bool,
    source: str,
) -> HealthOutcome:
    """按分级执行分支；事件行已由调用方合并本次答案并 flush。"""
    state: HealthState = {
        "event_id": str(event.event_id),
        "level": triage.level,
        "source": source,
        "record_health": record_health,
    }
    if LANGGRAPH_AVAILABLE:
        final = await _build_graph(session).ainvoke(state)
    else:
        final = await _NODES[triage.level](session, state)
    return HealthOutcome(
        status=str(final.get("status") or event.status),
        reply=str(final.get("reply") or GREEN_REPLY),
        required_role=final.get("required_role"),
        workorder_id=final.get("workorder_id"),
        referral_id=final.get("referral_id"),
        workorder_created=bool(final.get("workorder_created")),
    )


__all__ = [
    "GREEN_REPLY",
    "RED_REPLY",
    "YELLOW_REPLY",
    "HealthGraphError",
    "HealthOutcome",
    "HealthState",
    "LANGGRAPH_AVAILABLE",
    "describe_event",
    "run_health_decision",
]
