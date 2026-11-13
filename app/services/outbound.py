"""服务端主动外呼编排（Day7 段4）。

职责：
- 由 elder 解析平板设备（bed_no 精确匹配优先，其次房间级设备）；
- 先经段3 的图发起一次提醒（图内 mark_reminded 计数），再尝试反向 WebRTC；
- WebRTC 不可用时降级为现有 WS TTS 通道，并明确标记 ``degraded``；
- 无设备时把订单落 ``missed`` 并调用段1 唯一漏服建单入口，commit 后再推送。

约束：
- 所有任务都在 uvicorn 事件循环内执行，不新建 loop、不起独立线程；
- 原始音频只在内存中流转，不落盘、不进日志；
- 只调用段1 的 ``create_missed_medication_work_order`` 与段3 的图接口，不修改它们。
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.graphs.medication import start_confirmation
from app.models import Device, Elder, MedicationEvent, MedicationOrder, MedicationPlan, Workorder
from app.models.enums import (
    DeviceStatus,
    MedicationEventSource,
    MedicationEventType,
    MedicationOrderStatus,
    MedicationPlanStatus,
)
from app.realtime.session import OutboundCall, outbound_call_registry
from app.services import workorder as workorder_service
from app.services.viseme import build_viseme_timeline
from app.services.voice import get_tts_provider, voice_session_manager
from app.services.workorder_events import publish_workorder_event

logger = get_logger(__name__)

TERMINAL_STATUSES = (
    MedicationOrderStatus.CONFIRMED,
    MedicationOrderStatus.MISSED,
    MedicationOrderStatus.SKIPPED,
    MedicationOrderStatus.CANCELLED,
)
# 前后端约定的建连等待上限（秒）：超出即降级为 WS TTS
ANSWER_TIMEOUT_SECONDS = 8.0
# 外呼结束清理的额外缓冲（播放时长 + 2 秒）
CALL_CLEANUP_MARGIN_SECONDS = 2.0


class OutboundError(RuntimeError):
    """外呼入参/数据错误（任务不存在、计划缺失）。"""


@dataclass
class OutboundResult:
    """一次外呼的可观测结果；脚本只打印元数据，不打印长者原文。"""

    order_id: str
    elder_id: str
    device_id: str | None = None
    channel: str = "none"  # rtc / ws / none
    degraded: str | None = None  # rtc_unavailable / tts_unavailable / no_device / invalid_plan
    attempt: int = 0
    order_status: str = ""
    graph_status: str | None = None
    duration_ms: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _log_degraded(
    result: OutboundResult, *, degraded: str, reason: str, error: str | None = None
) -> None:
    result.degraded = degraded
    logger.warning(
        "用药外呼降级",
        extra={
            "event": "medication_call_degraded",
            "degraded": degraded,
            "reason": reason,
            "object_type": "medication_order",
            "object_id": result.order_id,
            "elder_id": result.elder_id,
            "device_id": result.device_id,
            "error": error,
        },
    )


async def resolve_elder_device(session: AsyncSession, elder: Elder) -> Device | None:
    """bed_no 精确匹配优先；没有则退回 ``bed_no IS NULL`` 的房间级设备。

    多个候选时取最近更新的一条并记警告（不静默选定）。
    """
    if elder.room_id is None:
        return None
    base = (Device.room_id == elder.room_id, Device.status == DeviceStatus.ACTIVE)

    if elder.bed_no is not None:
        rows = (
            await session.execute(
                select(Device)
                .where(*base, Device.bed_no == elder.bed_no)
                .order_by(Device.updated_at.desc())
            )
        ).scalars().all()
        if rows:
            if len(rows) > 1:
                logger.warning(
                    "同一床位存在多台设备，取最近更新的一条",
                    extra={
                        "event": "medication_call_device_ambiguous",
                        "elder_id": str(elder.elder_id),
                        "count": len(rows),
                        "bed_no": elder.bed_no,
                    },
                )
            return rows[0]

    rows = (
        await session.execute(
            select(Device)
            .where(*base, Device.bed_no.is_(None))
            .order_by(Device.updated_at.desc())
        )
    ).scalars().all()
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "同一房间存在多台房间级设备，取最近更新的一条",
            extra={
                "event": "medication_call_device_ambiguous",
                "elder_id": str(elder.elder_id),
                "count": len(rows),
            },
        )
    return rows[0]


async def synthesize_pcm(text: str, settings: Settings | None = None) -> tuple[bytes, int] | None:
    """TTS 合成到内存；失败返回 None，由调用方标记降级（不重试阻塞）。"""
    settings = settings or get_settings()
    tts = get_tts_provider()
    try:
        pcm = await asyncio.wait_for(tts.synthesize(text), timeout=settings.tts_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 —— TTS 临时失败只标记降级，下一轮扫描重试
        logger.warning(
            "外呼 TTS 合成失败",
            extra={"event": "medication_call_tts_failed", "error": f"{type(exc).__name__}: {exc}"},
        )
        return None
    if not pcm:
        return None
    return pcm, tts.sample_rate()


def _tts_event(text: str, pcm: bytes, sample_rate: int) -> dict[str, Any]:
    try:
        visemes = build_viseme_timeline(pcm, sample_rate)
    except Exception:  # noqa: BLE001 —— 口型失败只丢口型，不阻断出声
        visemes = []
    return {
        "type": "tts",
        "text": text,
        "audio": base64.b64encode(pcm).decode("ascii"),
        "sample_rate": sample_rate,
        "visemes": visemes,
    }


def _loads_order_id(order_id: str | uuid.UUID) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(order_id))
    except (TypeError, ValueError, AttributeError):
        return None


async def _force_missed(
    session: AsyncSession, order: MedicationOrder, *, reason_code: str
) -> tuple[Workorder | None, bool]:
    """无设备等不可重试场景：直接落 missed 并调用段1 唯一建单入口。"""
    order.status = MedicationOrderStatus.MISSED
    session.add(
        MedicationEvent(
            order_id=order.order_id,
            plan_id=order.plan_id,
            elder_id=order.elder_id,
            event_type=MedicationEventType.MISSED,
            source=MedicationEventSource.SCHEDULER,
            payload={"attempt": order.attempt_count or 0, "reason_code": reason_code},
            dedupe_key=f"missed:{order.order_id}",
            created_at=datetime.now(UTC),
        )
    )
    workorder, created = await workorder_service.create_missed_medication_work_order(
        session, order=order, reason_code=reason_code
    )
    await session.flush()
    return workorder, created


async def _handle_no_device(
    factory: Callable[[], Any], order_id: uuid.UUID, *, result: OutboundResult
) -> None:
    """无设备：订单落 missed、建漏服工单；commit 之后再 publish（禁事务内推送）。"""
    async with factory() as session:
        order = await session.get(MedicationOrder, order_id)
        if order is None or order.status in TERMINAL_STATUSES:
            return
        workorder, created = await _force_missed(session, order, reason_code="no_device")
        await session.commit()
        if created and workorder is not None:
            await publish_workorder_event("ticket.created", workorder)
        result.order_status = order.status
        result.attempt = order.attempt_count or 0
    logger.warning(
        "用药外呼找不到设备",
        extra={
            "event": "medication_call_no_device",
            "object_type": "medication_order",
            "object_id": result.order_id,
            "elder_id": result.elder_id,
        },
    )
    _log_degraded(result, degraded="no_device", reason="device_not_found")


def _schedule_cleanup(call: Any, registry: Any, pcm: bytes, sample_rate: int) -> None:
    """播放结束后关闭外呼 PC，避免幽灵连接（仅真实 OutboundCall）。"""
    if not isinstance(call, OutboundCall):
        return
    duration = len(pcm) / max(sample_rate, 1) / 2 + CALL_CLEANUP_MARGIN_SECONDS

    async def _cleanup() -> None:
        try:
            await asyncio.sleep(duration)
        finally:
            await registry.close(call.call_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 无运行循环时无需清理
        return
    loop.create_task(_cleanup())


async def trigger_medication_call(
    order_id: str | uuid.UUID,
    *,
    session_factory: Callable[[], Any] | None = None,
    checkpoint_path: str | None = None,
    settings: Settings | None = None,
    call_registry: Any | None = None,
    session_manager: Any | None = None,
    call_factory: Callable[..., Any] | None = None,
) -> OutboundResult:
    """对一条服药任务单发起一次主动提醒（RTC 优先，WS 降级）。"""
    started = time.perf_counter()
    settings = settings or get_settings()
    registry = call_registry if call_registry is not None else outbound_call_registry
    manager = session_manager if session_manager is not None else voice_session_manager
    factory = session_factory or get_session_factory()

    uid = _loads_order_id(order_id)
    if uid is None:
        raise OutboundError("服药任务不存在")
    async with factory() as session:
        order = await session.get(MedicationOrder, uid)
        if order is None:
            raise OutboundError("服药任务不存在")
        plan = await session.get(MedicationPlan, order.plan_id)
        elder = await session.get(Elder, order.elder_id)
    if plan is None or elder is None:
        raise OutboundError("用药计划或长者不存在")

    result = OutboundResult(
        order_id=str(order.order_id),
        elder_id=str(order.elder_id),
        attempt=order.attempt_count or 0,
        order_status=order.status,
    )
    if order.status in TERMINAL_STATUSES:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "服药任务已终态，跳过外呼",
            extra={
                "event": "medication_call_skipped",
                "object_type": "medication_order",
                "object_id": result.order_id,
                "status": order.status,
            },
        )
        return result
    if plan.status != MedicationPlanStatus.ACTIVE:
        result.degraded = "invalid_plan"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "用药计划不生效，跳过外呼",
            extra={
                "event": "medication_call_invalid_plan",
                "object_type": "medication_order",
                "object_id": result.order_id,
                "plan_id": str(plan.plan_id),
                "plan_status": plan.status,
            },
        )
        return result

    async with factory() as session:
        device = await resolve_elder_device(session, elder)
    if device is None:
        await _handle_no_device(factory, order.order_id, result=result)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
    result.device_id = str(device.device_id)

    if registry.find_by_device(device.device_id) is not None:
        result.degraded = "call_in_progress"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "同一设备已有外呼，跳过本次",
            extra={
                "event": "medication_call_in_progress",
                "object_type": "medication_order",
                "object_id": result.order_id,
                "device_id": result.device_id,
            },
        )
        return result

    # 先建单/计数（图内 mark_reminded → calling），再外呼
    confirmation = await start_confirmation(
        order.order_id,
        session_factory=factory,
        checkpoint_path=checkpoint_path,
        source="voice",
        device_id=str(device.device_id),
    )
    result.attempt = confirmation.attempt
    result.order_status = confirmation.order_status
    result.graph_status = confirmation.status
    logger.info(
        "用药外呼已发起",
        extra={
            "event": "medication_call_started",
            "object_type": "medication_order",
            "object_id": result.order_id,
            "elder_id": result.elder_id,
            "device_id": result.device_id,
            "attempt": result.attempt,
            "graph_status": result.graph_status,
        },
    )
    if confirmation.order_status in TERMINAL_STATUSES:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    text = confirmation.reply or "该吃药了。"
    if manager.get(device.device_id) is None:
        # WS 未连接：既不能 RTC 信令，也无法播 WS TTS；明确标记，等待下一轮
        _log_degraded(result, degraded="rtc_unavailable", reason="ws_offline")
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    synthesized = await synthesize_pcm(text, settings)
    if synthesized is None:
        _log_degraded(result, degraded="tts_unavailable", reason="tts_failed")
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
    pcm, sample_rate = synthesized

    call: Any = None
    try:
        call = (
            call_factory(device.device_id, text, pcm, sample_rate)
            if call_factory is not None
            else OutboundCall(device.device_id)
        )
        offer_sdp = await call.create_offer(pcm, sample_rate)
        registry.add(call)
        sent = await manager.send_event_to(
            device.device_id,
            {
                "type": "rtc_call_offer",
                "call_id": call.call_id,
                "sdp": offer_sdp,
                "sdp_type": "offer",
            },
        )
        if not sent:
            raise RuntimeError("ws send failed")
        await call.wait_answered(ANSWER_TIMEOUT_SECONDS)
        result.channel = "rtc"
        _schedule_cleanup(call, registry, pcm, sample_rate)
    except Exception as exc:  # noqa: BLE001 —— RTC 任一环节失败都走可见降级
        if call is not None:
            await registry.close(getattr(call, "call_id", ""))
        _log_degraded(
            result,
            degraded="rtc_unavailable",
            reason="rtc_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        try:
            spoken = await manager.send_event_to(
                device.device_id, _tts_event(text, pcm, sample_rate)
            )
        except Exception as ws_exc:  # noqa: BLE001 —— WS 也失败时只能留日志
            logger.warning(
                "外呼降级 WS 播放失败",
                extra={
                    "event": "medication_call_ws_failed",
                    "object_id": result.order_id,
                    "error": f"{type(ws_exc).__name__}: {ws_exc}",
                },
            )
            spoken = False
        result.channel = "ws" if spoken else "none"
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "用药外呼结果",
        extra={
            "event": "medication_call_finished",
            "object_type": "medication_order",
            "object_id": result.order_id,
            "channel": result.channel,
            "degraded": result.degraded,
            "duration_ms": result.duration_ms,
        },
    )
    return result


HEALTH_INTAKE_TEXT = "该休息了，我问您几个小问题，答不上来也没关系。"


@dataclass
class HealthIntakeResult:
    """一次健康采集引导外呼的可观测结果（不记录长者音频与原文）。"""

    elder_id: str
    device_id: str | None = None
    channel: str = "none"  # ws / none
    degraded: str | None = None  # no_device / device_offline / call_in_progress / ...
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def trigger_health_intake_call(
    elder_id: str | uuid.UUID,
    *,
    session_factory: Callable[[], Any] | None = None,
    settings: Settings | None = None,
    call_registry: Any | None = None,
    session_manager: Any | None = None,
) -> HealthIntakeResult:
    """夜间健康采集引导语：只播一句开场白，逐题问答交给 Agent ``health_check``。

    不新建 WS 逐题状态机，也不使用 RTC（避免踩 aiortc 时序坑）；
    设备离线/已在外呼/无设备都返回可见降级，由调度器继续处理下一位长者。
    """
    settings = settings or get_settings()
    registry = call_registry if call_registry is not None else outbound_call_registry
    manager = session_manager if session_manager is not None else voice_session_manager
    factory = session_factory or get_session_factory()

    uid = _loads_order_id(elder_id)
    if uid is None:
        raise OutboundError("长者不存在")
    async with factory() as session:
        elder = await session.get(Elder, uid)
        if elder is None:
            raise OutboundError("长者不存在")
        device = await resolve_elder_device(session, elder)

    result = HealthIntakeResult(elder_id=str(uid))
    if device is None:
        result.degraded = "no_device"
        logger.warning(
            "健康采集外呼找不到设备",
            extra={
                "event": "health_intake_call_degraded",
                "degraded": result.degraded,
                "elder_id": result.elder_id,
            },
        )
        return result
    result.device_id = str(device.device_id)

    if registry.find_by_device(device.device_id) is not None:
        result.degraded = "call_in_progress"
        logger.info(
            "同一设备已有外呼，跳过健康采集引导",
            extra={
                "event": "health_intake_call_skipped",
                "elder_id": result.elder_id,
                "device_id": result.device_id,
            },
        )
        return result

    if manager.get(device.device_id) is None:
        result.degraded = "device_offline"
        logger.info(
            "设备未连接，跳过健康采集引导",
            extra={
                "event": "health_intake_call_skipped",
                "elder_id": result.elder_id,
                "device_id": result.device_id,
            },
        )
        return result

    synthesized = await synthesize_pcm(HEALTH_INTAKE_TEXT, settings)
    if synthesized is None:
        result.degraded = "tts_unavailable"
        return result
    pcm, sample_rate = synthesized

    try:
        sent = await manager.send_event_to(
            device.device_id, _tts_event(HEALTH_INTAKE_TEXT, pcm, sample_rate)
        )
    except Exception as exc:  # noqa: BLE001 —— WS 失败只记降级
        result.degraded = "ws_send_failed"
        result.error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "健康采集引导 WS 下发失败",
            extra={
                "event": "health_intake_call_degraded",
                "degraded": result.degraded,
                "elder_id": result.elder_id,
                "device_id": result.device_id,
                "error": result.error,
            },
        )
        return result

    result.channel = "ws" if sent else "none"
    if not sent:
        result.degraded = "ws_send_failed"
    logger.info(
        "健康采集引导已下发",
        extra={
            "event": "health_intake_call_finished",
            "elder_id": result.elder_id,
            "device_id": result.device_id,
            "channel": result.channel,
            "degraded": result.degraded,
        },
    )
    return result


__all__ = [
    "HealthIntakeResult",
    "HEALTH_INTAKE_TEXT",
    "OutboundError",
    "OutboundResult",
    "resolve_elder_device",
    "synthesize_pcm",
    "trigger_health_intake_call",
    "trigger_medication_call",
]
