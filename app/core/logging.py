"""结构化 JSON 日志：标准库 logging 输出 JSON，通过 contextvars 注入 trace_id。

不记录请求正文与健康隐私内容。
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def get_trace_id() -> str | None:
    """获取当前请求的 trace_id；请求上下文外返回 None。"""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """绑定当前请求的 trace_id。"""
    trace_id_var.set(trace_id)


class JsonFormatter(logging.Formatter):
    """将日志记录渲染为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = get_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        # 附加结构化字段（来自 extra）
        for key in (
            "method",
            "path",
            "status_code",
            "duration_ms",
            "event",
            # Day2 审计留痕字段（audit_logs 表前的结构化日志替代方案）
            "action",
            "actor_id",
            "actor_role",
            "object_type",
            "object_id",
            "result",
            "idempotency_key",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TraceIdFilter(logging.Filter):
    """确保 trace_id 存在于记录属性中，便于过滤器/格式器访问。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = get_trace_id()  # type: ignore[attr-defined]
        return True


def setup_logging() -> None:
    """初始化根日志：JSON 格式输出到 stdout。幂等，可重复调用。"""
    settings = get_settings()
    root = logging.getLogger()
    # 幂等：已配置过则只同步级别
    if getattr(root, "_kangban_json_configured", False):
        root.setLevel(settings.log_level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceIdFilter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    root._kangban_json_configured = True  # type: ignore[attr-defined]

    # 将 uvicorn 自带日志器统一接入 JSON 根日志，避免混入非结构化输出
    for uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(uv_name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """获取带 trace_id 注入的日志器。"""
    return logging.getLogger(name)
