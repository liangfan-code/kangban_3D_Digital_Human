"""应用生命周期：启动时初始化日志，关闭时释放数据库引擎。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.mcp_client import get_mcp_host, reset_mcp_host
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger = get_logger(__name__)
    logger.info("应用启动", extra={"event": "startup"})
    # MCP 工具发现不阻断启动：单个 Server 失败只记日志，注册表回落到本地兜底工具
    if get_settings().mcp_enabled:
        try:
            await get_mcp_host().start()
        except Exception as exc:  # noqa: BLE001 —— 外部 Server 异常不能拖垮应用
            logger.warning(
                "MCP 启动降级",
                extra={"event": "mcp_startup_degraded", "error_type": type(exc).__name__},
            )
    # 用药提醒：先补漏（不阻塞启动），再起间隔扫描；APScheduler 缺失/关闭均可见降级
    from app.services.medication_scheduler import (
        run_startup_backfill,
        shutdown_medication_scheduler,
        start_medication_scheduler,
    )

    await run_startup_backfill()
    scheduler = None
    try:
        scheduler = await start_medication_scheduler()
    except Exception as exc:  # noqa: BLE001 —— 调度器异常不得拖垮应用
        logger.error(
            "用药调度器启动失败",
            extra={
                "event": "medication_scheduler_start_failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    # Day8：夜间健康采集复用同一调度器，不新建第二个 APScheduler
    from app.services.health_scheduler import register_health_scheduler

    try:
        register_health_scheduler(scheduler)
    except Exception as exc:  # noqa: BLE001 —— 采集 job 注册失败不得拖垮应用
        logger.error(
            "健康采集 job 注册失败",
            extra={
                "event": "health_scheduler_start_failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    yield
    await shutdown_medication_scheduler()
    # 关闭时清理数据库引擎连接池与会话状态缓存连接
    from app.db.session import dispose_engine
    from app.services.session_state import close_session_store

    await dispose_engine()
    await close_session_store()
    # Host 无常驻连接，只清掉进程级缓存引用，避免跨事件循环复用
    reset_mcp_host()
    logger.info("应用关闭，引擎与会话状态已释放", extra={"event": "shutdown"})
