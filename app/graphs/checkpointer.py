"""LangGraph SQLite checkpointer 工厂。

- 默认落盘 ``logs/checkpoints/medication.sqlite3``（``logs/`` 已在 .gitignore）；
- 路径可用 ``Settings.medication_checkpoint_path`` 覆盖，测试直接传临时路径；
- 所有异步调用绑定当前事件循环，不新建事件循环、不起独立线程；
- LangGraph 未安装或 Saver 打开失败时抛 :class:`MedicationCheckpointUnavailable`，
  由调用方转成可见降级（单轮确认），不阻塞应用启动。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_CHECKPOINT_PATH = Path("logs") / "checkpoints" / "medication.sqlite3"


class MedicationCheckpointUnavailable(RuntimeError):
    """LangGraph checkpointer 不可用（未安装 / 文件无法打开）。"""


def resolve_checkpoint_path(path: str | Path | None = None) -> Path:
    """返回 checkpoint 数据库路径：显式入参 > Settings > 默认值。"""
    if path is not None:
        return Path(path)
    configured = get_settings().medication_checkpoint_path
    return Path(configured) if configured else DEFAULT_CHECKPOINT_PATH


@asynccontextmanager
async def open_medication_checkpointer(
    path: str | Path | None = None,
) -> AsyncIterator[Any]:
    """打开用药确认图的 AsyncSqliteSaver；失败统一抛领域异常。"""
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError as exc:  # pragma: no cover - 未安装依赖时走降级
        raise MedicationCheckpointUnavailable("langgraph-checkpoint-sqlite 未安装") from exc

    db_path = resolve_checkpoint_path(path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MedicationCheckpointUnavailable(
            f"checkpoint 目录不可创建: {type(exc).__name__}"
        ) from exc

    manager = AsyncSqliteSaver.from_conn_string(str(db_path))
    try:
        saver = await manager.__aenter__()
    except Exception as exc:  # noqa: BLE001 —— 打开失败统一降级
        raise MedicationCheckpointUnavailable(
            f"checkpoint 打开失败: {type(exc).__name__}"
        ) from exc

    try:
        yield saver
    finally:
        await manager.__aexit__(None, None, None)
