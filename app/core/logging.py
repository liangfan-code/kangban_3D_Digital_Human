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
            # Day5.2 延迟打点（turn_latency）：只输出时长/数量/降级原因
            "turn_id",
            "turn_total_ms",
            "asr_connect_ms",
            "asr_e2e_ms",
            "asr_ms",
            "llm_first_token_ms",
            "llm_total_ms",
            "a2a_ms",
            "viseme_ms",
            "tts_first_byte_ms",
            "tts_total_ms",
            "tts_decode_ms",
            "tts_sentences",
            "degraded",
            "device_id",
            "elder_id",
            "thread_id",
            # Day5.2 熔断状态迁移与 ASR/TTS Provider 元数据
            "provider",
            "from",
            "to",
            "failure_threshold",
            "open_seconds",
            "asr_confidence",
            "asr_confidence_source",
            "asr_low_conf",
            "mode",
            "pcm_bytes",
            "voice",
            "error",
            # Day5.4 死循环防护（tool_loop_guard）：只记 Tool 名称/次数/原因
            "tool_name",
            "call_count",
            "reason",
            # Day6 三级路由：只记层级/意图/置信度/降级类型，不记长者原文
            "route_layer",
            "intent",
            "route_confidence",
            "is_sensitive",
            "is_private",
            "sensitive_category",
            "rule_id",
            "escalated_to_agent",
            "llm_called",
            "hitl_enqueued",
            "classifier_ms",
            "agent_steps",
            "tool_calls",
            # Day7 段7 任务级 trace：本轮工具调用顺序与回复长度（只记元数据）
            "tools",
            "reply_chars",
            "mcp_calls",
            "server_id",
            "ok",
            # Day7 段4 定时用药外呼：只记任务/设备/通道/计数等元数据
            "attempt",
            "attempt_before",
            "status",
            "channel",
            "call_id",
            "target_device_id",
            "plan_id",
            "plan_status",
            "scheduled_at",
            "interval_seconds",
            "timezone",
            "due",
            "scanned",
            "created_order",
            "triggered",
            "timed_out",
            "missed",
            "skipped",
            "failed",
            "count",
            "bed_no",
            # Day7 段5 长期记忆：只记模型/维度/条数/统计，不记记忆正文
            "model",
            "dimension",
            "elapsed_ms",
            "duplicate",
            "superseded",
            "suppressed",
            "merged",
            "categories",
            "count_new",
            "dry_run",
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
