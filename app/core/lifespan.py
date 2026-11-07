"""应用生命周期：启动时初始化日志，关闭时释放数据库引擎。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger = get_logger(__name__)
    logger.info("应用启动", extra={"event": "startup"})
    yield
    # 关闭时清理数据库引擎连接池
    from app.db.session import dispose_engine

    await dispose_engine()
    logger.info("应用关闭，引擎已释放", extra={"event": "shutdown"})
