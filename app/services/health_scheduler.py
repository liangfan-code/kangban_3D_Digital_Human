"""夜间健康采集调度（Day8 段1）。

遵循既有约定：不新建第二个 APScheduler（见 ``medication_scheduler`` 注释），
而是把「每晚整点采集」job 注册到已经启动的用药调度器上；调度器不可用时
（测试/关闭/未安装）只记录可见原因，不阻塞应用启动。

任务本体只做两件事：选出「在住 + 有在线设备条件」的长者，逐个下发一句
引导语（``trigger_health_intake_call``）；逐题问答交给 Agent ``health_check``。
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models import Device, Elder
from app.models.enums import DeviceStatus, ElderStatus
from app.services.outbound import trigger_health_intake_call

logger = get_logger(__name__)

JOB_ID = "health_collection_nightly"


def _is_test_environment(settings: Settings) -> bool:
    return settings.app_env.strip().lower() in {"test", "testing", "pytest"} or (
        "pytest" in sys.modules
    )


async def collect_candidate_ids(session: Any) -> list[uuid.UUID]:
    """在住长者中，房间内至少有一台启用设备的长者 id（去重、稳定排序）。"""
    active_room_devices = select(Device.room_id).where(
        Device.status == DeviceStatus.ACTIVE,
        Device.room_id.is_not(None),
    )
    rows = await session.execute(
        select(Elder.elder_id)
        .where(
            Elder.status == ElderStatus.ACTIVE,
            Elder.room_id.in_(active_room_devices),
        )
        .order_by(Elder.elder_id)
    )
    return [row[0] for row in rows.all()]


async def run_nightly_collection(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Any] | None = None,
    trigger: Callable[..., Awaitable[Any]] | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """跑一轮夜间采集；单长者失败不终止整轮，返回计数用于验收。"""
    settings = settings or get_settings()
    summary = {"candidates": 0, "triggered": 0, "failed": 0}
    if not settings.health_collect_enabled:
        logger.info(
            "健康采集已关闭，跳过本轮",
            extra={"event": "health_collection_skipped", "reason": "disabled"},
        )
        return summary

    factory = session_factory or get_session_factory()
    trigger_fn = trigger or trigger_health_intake_call
    async with factory() as session:
        elder_ids = await collect_candidate_ids(session)
    summary["candidates"] = len(elder_ids)

    reference = now or datetime.now(UTC)
    for elder_id in elder_ids:
        try:
            await trigger_fn(
                elder_id,
                session_factory=factory,
                settings=settings,
            )
            summary["triggered"] += 1
        except Exception as exc:  # noqa: BLE001 —— 单长者失败不终止整轮
            summary["failed"] += 1
            logger.error(
                "健康采集触发失败",
                extra={
                    "event": "health_collection_trigger_failed",
                    "elder_id": str(elder_id),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    logger.info(
        "健康采集完成",
        extra={
            "event": "health_collection_summary",
            "reference": reference.isoformat(),
            **summary,
        },
    )
    return summary


async def _collection_job() -> None:
    try:
        await run_nightly_collection()
    except Exception as exc:  # noqa: BLE001 —— job 级兜底
        logger.error(
            "健康采集 job 异常",
            extra={
                "event": "health_collection_job_failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


def register_health_scheduler(
    scheduler: Any | None, settings: Settings | None = None
) -> bool:
    """把每晚采集 job 挂到既有调度器；返回是否注册成功。"""
    settings = settings or get_settings()
    if not settings.health_collect_enabled:
        logger.info(
            "健康采集已关闭，不注册夜间 job",
            extra={"event": "health_scheduler_unavailable", "reason": "disabled"},
        )
        return False
    if _is_test_environment(settings):
        logger.info(
            "测试环境不注册健康采集 job",
            extra={"event": "health_scheduler_unavailable", "reason": "test"},
        )
        return False
    if scheduler is None:
        logger.info(
            "用药调度器未启动，健康采集 job 跳过",
            extra={"event": "health_scheduler_unavailable", "reason": "no_scheduler"},
        )
        return False

    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning(
            "APScheduler 未安装，健康采集 job 跳过",
            extra={"event": "health_scheduler_unavailable", "reason": "not_installed"},
        )
        return False

    hour = min(23, max(0, settings.health_collect_hour))
    scheduler.add_job(
        _collection_job,
        trigger=CronTrigger(hour=hour, minute=0, timezone=settings.app_timezone),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info(
        "健康采集 job 已注册",
        extra={
            "event": "health_scheduler_registered",
            "hour": hour,
            "timezone": settings.app_timezone,
        },
    )
    return True


__all__ = [
    "JOB_ID",
    "collect_candidate_ids",
    "register_health_scheduler",
    "run_nightly_collection",
]
