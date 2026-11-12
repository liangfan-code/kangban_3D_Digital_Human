"""用药确认 LangGraph 子图（Day7 段3）。

结构::

    START ──start──▶ prepare_reminder ──▶ await_confirm ──▶ END（本轮结束）
      └────reply──▶ evaluate_reply ──confirmed──▶ END
                          │         └──deferred──▶ END
                          ├──没确认且 attempt<3──▶ prepare_reminder（真循环边）
                          └──attempt>=3──▶ escalate_missed ──▶ END

约束：
- ``thread_id`` 固定 ``medication:{order_id}``，一条任务单一条流程；
- 数据库是权威：``prepare_reminder``→``mark_reminded``、确认→``mark_confirmed``、
  否认/超时→``mark_denied_or_timeout``（内部幂等建漏服工单），图状态只用于恢复；
- 不使用 ``interrupt`` / ``Command(resume=...)``，一律按轮推进（start / reply）；
- 图状态里的 ``last_reply`` 只存分类结果（confirmed/denied/deferred），不存长者原文；
- LangGraph 未安装或 Saver 打开失败时退化为单轮确认，仍必须落库。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.graphs.checkpointer import (
    MedicationCheckpointUnavailable,
    open_medication_checkpointer,
)
from app.models import MedicationOrder, MedicationPlan, Workorder
from app.models.enums import MedicationOrderStatus
from app.router.signals import detect_medication_reply
from app.services import medication_order as order_service

try:  # LangGraph 是可选运行依赖：未安装时应用照常启动，仅本图退化为单轮确认
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时走降级
    END = START = None  # type: ignore[assignment]
    StateGraph = None  # type: ignore[assignment]
    LANGGRAPH_AVAILABLE = False

logger = get_logger(__name__)

THREAD_PREFIX = "medication"
MAX_ATTEMPTS = 3
TERMINAL_STATUSES = (
    MedicationOrderStatus.CONFIRMED,
    MedicationOrderStatus.MISSED,
    MedicationOrderStatus.SKIPPED,
    MedicationOrderStatus.CANCELLED,
)
VALID_SOURCES = ("voice", "text")

# 默认会话工厂：与 FastAPI 共用全局引擎；测试可注入临时会话工厂
SessionFactory = Callable[[], Any]


class MedicationGraphError(RuntimeError):
    """用药确认图入参/数据异常（工具层会转成可朗读降级）。"""


@dataclass(frozen=True)
class ConfirmationResult:
    """一次图推进的可朗读结果与状态摘要。"""

    reply: str
    status: str
    attempt: int
    order_status: str
    workorder_id: str | None = None
    degraded: str | None = None


class MedicationState(TypedDict, total=False):
    """图状态；``status`` 取 calling/waiting_confirm/retry/confirmed/escalated/deferred/done。"""

    elder_id: str
    plan_id: str
    order_id: str
    drug_name: str
    dose: str
    attempt: int
    status: str
    last_reply: str
    device_id: str
    workorder_id: str
    updated_at: str
    action: str
    prompt: str


def thread_id_for(order_id: str | uuid.UUID) -> str:
    """一条任务单一条确认流程，跨天/跨时段天然隔离。"""
    return f"{THREAD_PREFIX}:{order_id}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_source(source: str) -> str:
    return source if source in VALID_SOURCES else "text"


def _reminder_text(drug_name: str, dose: str) -> str:
    return f"该吃药了。{drug_name}，{dose}。吃完跟我说一声。"


def _confirmed_text(drug_name: str) -> str:
    return f"好，{drug_name}已经记下了。"


_DEFERRED_TEXT = "好，那您慢慢来，我过一会儿再提醒您。"
_ESCALATED_TEXT = "这次吃药还没确认，我已经告诉护工，他们会来看看您。"
_TERMINAL_TEXT = {
    MedicationOrderStatus.CONFIRMED: "这次吃药已经记下了。",
    MedicationOrderStatus.MISSED: "这次吃药已经转给护工了，他们会来看看您。",
    MedicationOrderStatus.SKIPPED: "这次吃药已经记下了。",
    MedicationOrderStatus.CANCELLED: "这次吃药不用提醒了。",
}


def _default_session_factory() -> Any:
    return get_session_factory()()


@asynccontextmanager
async def _scope(factory: SessionFactory | None) -> AsyncIterator[AsyncSession]:
    """一次图推进一个短会话；成功提交，异常回滚。"""
    async with (factory or _default_session_factory)() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def _get_order(
    session: AsyncSession, order_id: str | uuid.UUID
) -> MedicationOrder | None:
    try:
        uid = uuid.UUID(str(order_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return await session.get(MedicationOrder, uid)


async def _get_plan(session: AsyncSession, order: MedicationOrder) -> MedicationPlan:
    plan = await session.get(MedicationPlan, order.plan_id)
    if plan is None:
        raise MedicationGraphError("用药计划不存在")
    return plan


def _terminal_result(order: MedicationOrder) -> ConfirmationResult:
    return ConfirmationResult(
        reply=_TERMINAL_TEXT.get(order.status, "这次吃药不用再提醒了。"),
        status="done",
        attempt=order.attempt_count or 0,
        order_status=order.status,
    )


def _snapshot(
    order: MedicationOrder, plan: MedicationPlan, device_id: str
) -> dict[str, Any]:
    return {
        "elder_id": str(order.elder_id),
        "plan_id": str(order.plan_id),
        "order_id": str(order.order_id),
        "drug_name": plan.drug_name,
        "dose": plan.dose,
        "attempt": order.attempt_count or 0,
        "device_id": device_id,
        "updated_at": _now_iso(),
    }


def _result_from_state(
    state: dict[str, Any],
    order: MedicationOrder,
    *,
    default_reply: str,
    degraded: str | None = None,
    workorder_id: str | None = None,
) -> ConfirmationResult:
    return ConfirmationResult(
        reply=str(state.get("prompt") or default_reply),
        status=str(state.get("status") or "done"),
        attempt=int(state.get("attempt") or 0),
        order_status=order.status,
        workorder_id=workorder_id,
        degraded=degraded,
    )


async def _find_missed_workorder_id(
    session: AsyncSession, order_id: uuid.UUID
) -> str | None:
    workorder_id = (
        await session.execute(
            select(Workorder.workorder_id).where(Workorder.medication_order_id == order_id)
        )
    ).scalar_one_or_none()
    return str(workorder_id) if workorder_id else None


# ---------------- 图节点 ----------------


def _build_graph(session: AsyncSession, saver: Any, source: str) -> Any:
    async def prepare_reminder(state: MedicationState) -> dict[str, Any]:
        order = await _get_order(session, state["order_id"])
        if order is None:
            raise MedicationGraphError("服药任务不存在")
        if order.status in TERMINAL_STATUSES:
            return {
                "status": "done",
                "attempt": order.attempt_count or 0,
                "prompt": _TERMINAL_TEXT.get(order.status, "这次吃药不用再提醒了。"),
                "updated_at": _now_iso(),
            }
        order = await order_service.mark_reminded(session, order=order, source=source)
        return {
            "attempt": order.attempt_count,
            "status": "calling",
            "prompt": _reminder_text(state.get("drug_name", ""), state.get("dose", "")),
            "updated_at": _now_iso(),
        }

    async def await_confirm(state: MedicationState) -> dict[str, Any]:
        return {"status": "waiting_confirm", "updated_at": _now_iso()}

    async def evaluate_reply(state: MedicationState) -> dict[str, Any]:
        order = await _get_order(session, state["order_id"])
        if order is None:
            raise MedicationGraphError("服药任务不存在")
        if order.status in TERMINAL_STATUSES:
            return {
                "status": "done",
                "attempt": order.attempt_count or 0,
                "prompt": _TERMINAL_TEXT.get(order.status, "这次吃药不用再提醒了。"),
                "updated_at": _now_iso(),
            }

        kind = state.get("last_reply")
        if kind == "confirmed":
            order = await order_service.mark_confirmed(
                session, order=order, source=source
            )
            return {
                "status": "confirmed",
                "attempt": order.attempt_count or 0,
                "prompt": _confirmed_text(state.get("drug_name", "这次用药")),
                "updated_at": _now_iso(),
            }
        if kind != "denied":
            # 「等会儿吃」不改订单事实，保持等待；下一轮再按 reply 推进
            return {
                "status": "deferred",
                "attempt": order.attempt_count or 0,
                "prompt": _DEFERRED_TEXT,
                "updated_at": _now_iso(),
            }

        order = await order_service.mark_denied_or_timeout(
            session, order=order, kind="denied", source=source
        )
        if order.status == MedicationOrderStatus.MISSED:
            return {
                "status": "escalated",
                "attempt": order.attempt_count or 0,
                "updated_at": _now_iso(),
            }
        return {
            "status": "retry",
            "attempt": order.attempt_count or 0,
            "updated_at": _now_iso(),
        }

    async def escalate_missed(state: MedicationState) -> dict[str, Any]:
        order = await _get_order(session, state["order_id"])
        if order is None:
            raise MedicationGraphError("服药任务不存在")
        # 幂等：若 evaluate_reply 已置 missed，这里不会重复写事件/建单
        order = await order_service.mark_denied_or_timeout(
            session, order=order, kind="denied", source=source
        )
        workorder_id = await _find_missed_workorder_id(session, order.order_id)
        return {
            "status": "escalated",
            "attempt": order.attempt_count or 0,
            "workorder_id": workorder_id or "",
            "prompt": _ESCALATED_TEXT,
            "updated_at": _now_iso(),
        }

    graph = StateGraph(MedicationState)
    graph.add_node("prepare_reminder", prepare_reminder)
    graph.add_node("await_confirm", await_confirm)
    graph.add_node("evaluate_reply", evaluate_reply)
    graph.add_node("escalate_missed", escalate_missed)

    def entry_router(state: MedicationState) -> str:
        return "evaluate_reply" if state.get("action") == "reply" else "prepare_reminder"

    def reply_router(state: MedicationState) -> str:
        if state.get("status") == "retry":
            return "prepare_reminder"
        if state.get("status") == "escalated":
            return "escalate_missed"
        return END

    graph.add_conditional_edges(
        START,
        entry_router,
        {"prepare_reminder": "prepare_reminder", "evaluate_reply": "evaluate_reply"},
    )
    graph.add_edge("prepare_reminder", "await_confirm")
    graph.add_edge("await_confirm", END)
    graph.add_conditional_edges(
        "evaluate_reply",
        reply_router,
        {
            "prepare_reminder": "prepare_reminder",
            "escalate_missed": "escalate_missed",
            END: END,
        },
    )
    graph.add_edge("escalate_missed", END)
    return graph.compile(checkpointer=saver)


# ---------------- 降级：单轮确认（仍必须落库） ----------------


def _log_degraded(order: MedicationOrder, error_type: str) -> None:
    logger.warning(
        "用药确认图不可用，退化为单轮确认",
        extra={
            "event": "medication_graph_degraded",
            "degraded": "medication_graph_unavailable",
            "error_type": error_type,
            "object_type": "medication_order",
            "object_id": str(order.order_id),
            "elder_id": str(order.elder_id),
        },
    )


async def _degraded_start(
    session: AsyncSession,
    *,
    order: MedicationOrder,
    plan: MedicationPlan,
    source: str,
    error_type: str,
) -> ConfirmationResult:
    _log_degraded(order, error_type)
    order = await order_service.mark_reminded(session, order=order, source=source)
    return ConfirmationResult(
        reply=_reminder_text(plan.drug_name, plan.dose),
        status="waiting_confirm",
        attempt=order.attempt_count,
        order_status=order.status,
        degraded="medication_graph_unavailable",
    )


async def _degraded_reply(
    session: AsyncSession,
    *,
    order: MedicationOrder,
    plan: MedicationPlan,
    reply_kind: str,
    source: str,
    error_type: str,
) -> ConfirmationResult:
    _log_degraded(order, error_type)
    if reply_kind == "confirmed":
        order = await order_service.mark_confirmed(session, order=order, source=source)
        return ConfirmationResult(
            reply=_confirmed_text(plan.drug_name),
            status="confirmed",
            attempt=order.attempt_count or 0,
            order_status=order.status,
            degraded="medication_graph_unavailable",
        )
    if reply_kind == "denied":
        # 单轮：只记否认，不重试、不升级（漏服兜底交给段4调度器），订单留在 pending
        order = await order_service.mark_denied_or_timeout(
            session,
            order=order,
            kind="denied",
            source=source,
            retry_limit=max(order.attempt_count or 0, 1) + 1,
        )
        return ConfirmationResult(
            reply="好，我记下了。您方便的时候再吃，我过一会儿再问您。",
            status="retry",
            attempt=order.attempt_count or 0,
            order_status=order.status,
            degraded="medication_graph_unavailable",
        )
    return ConfirmationResult(
        reply=_DEFERRED_TEXT,
        status="deferred",
        attempt=order.attempt_count or 0,
        order_status=order.status,
        degraded="medication_graph_unavailable",
    )


# ---------------- 对外接口 ----------------


async def start_confirmation(
    order_id: str | uuid.UUID,
    *,
    session_factory: SessionFactory | None = None,
    checkpoint_path: str | Path | None = None,
    source: str = "text",
    device_id: str = "",
) -> ConfirmationResult:
    """发起（或重新发起）一次提醒：图状态推进一轮后停在等待确认。"""
    normalized_source = _normalize_source(source)
    async with _scope(session_factory) as session:
        order = await _get_order(session, order_id)
        if order is None:
            raise MedicationGraphError("服药任务不存在")
        if order.status in TERMINAL_STATUSES:
            return _terminal_result(order)
        plan = await _get_plan(session, order)

        state_in = {
            **_snapshot(order, plan, device_id),
            "action": "start",
            "status": "calling",
        }
        if not LANGGRAPH_AVAILABLE:
            return await _degraded_start(
                session, order=order, plan=plan, source=normalized_source,
                error_type="langgraph_not_installed",
            )
        try:
            async with open_medication_checkpointer(checkpoint_path) as saver:
                graph = _build_graph(session, saver, normalized_source)
                state = await graph.ainvoke(
                    state_in, {"configurable": {"thread_id": thread_id_for(order.order_id)}}
                )
        except MedicationCheckpointUnavailable as exc:
            return await _degraded_start(
                session, order=order, plan=plan, source=normalized_source,
                error_type=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 —— 图异常不得穿透到语音链路
            await session.rollback()
            order = await _get_order(session, order_id)
            if order is None:
                raise MedicationGraphError("服药任务不存在") from exc
            plan = await _get_plan(session, order)
            return await _degraded_start(
                session, order=order, plan=plan, source=normalized_source,
                error_type=type(exc).__name__,
            )
        return _result_from_state(
            state,
            order,
            default_reply=_reminder_text(plan.drug_name, plan.dose),
        )


async def submit_reply(
    order_id: str | uuid.UUID,
    reply: str,
    *,
    session_factory: SessionFactory | None = None,
    checkpoint_path: str | Path | None = None,
    source: str = "text",
    device_id: str = "",
) -> ConfirmationResult:
    """提交一轮回复（吃了/没吃/等会儿），推进图并同步写订单与事件。"""
    normalized_source = _normalize_source(source)
    reply_kind = detect_medication_reply(reply)
    async with _scope(session_factory) as session:
        order = await _get_order(session, order_id)
        if order is None:
            raise MedicationGraphError("服药任务不存在")
        if reply_kind is None:
            return ConfirmationResult(
                reply="您是已经吃过药了，还是还没吃？",
                status="waiting_confirm",
                attempt=order.attempt_count or 0,
                order_status=order.status,
            )
        if order.status in TERMINAL_STATUSES:
            return _terminal_result(order)
        plan = await _get_plan(session, order)

        state_in = {
            **_snapshot(order, plan, device_id),
            "action": "reply",
            "last_reply": reply_kind,
        }
        if not LANGGRAPH_AVAILABLE:
            return await _degraded_reply(
                session, order=order, plan=plan, reply_kind=reply_kind,
                source=normalized_source, error_type="langgraph_not_installed",
            )
        try:
            async with open_medication_checkpointer(checkpoint_path) as saver:
                graph = _build_graph(session, saver, normalized_source)
                state = await graph.ainvoke(
                    state_in, {"configurable": {"thread_id": thread_id_for(order.order_id)}}
                )
        except MedicationCheckpointUnavailable as exc:
            return await _degraded_reply(
                session, order=order, plan=plan, reply_kind=reply_kind,
                source=normalized_source, error_type=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 —— 图异常不得穿透到语音链路
            await session.rollback()
            order = await _get_order(session, order_id)
            if order is None:
                raise MedicationGraphError("服药任务不存在") from exc
            plan = await _get_plan(session, order)
            return await _degraded_reply(
                session, order=order, plan=plan, reply_kind=reply_kind,
                source=normalized_source, error_type=type(exc).__name__,
            )
        return _result_from_state(
            state,
            order,
            default_reply=_DEFERRED_TEXT,
            workorder_id=str(state.get("workorder_id") or "") or None,
        )


__all__ = [
    "ConfirmationResult",
    "LANGGRAPH_AVAILABLE",
    "MedicationGraphError",
    "MedicationState",
    "start_confirmation",
    "submit_reply",
    "thread_id_for",
]
