"""trace_id 中间件：建立、上下文绑定与响应头回传。

- 合法 X-Trace-ID 请求头透传；非法或缺失则生成 UUID，避免日志注入。
- trace_id 绑定到 contextvars，日志 filter 与异常处理器可读取。
- 响应头回传 X-Trace-ID，并输出请求完成日志（方法、路径、状态码、耗时）。
"""

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger, set_trace_id

logger = get_logger(__name__)

TRACE_ID_HEADER = "X-Trace-ID"


def _normalize_trace_id(raw: str | None) -> str:
    """合法 UUID 透传，否则重新生成。"""
    if raw:
        try:
            return str(uuid.UUID(raw.strip()))
        except (ValueError, AttributeError):
            pass
    return str(uuid.uuid4())


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = _normalize_trace_id(request.headers.get(TRACE_ID_HEADER))
        set_trace_id(trace_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        response.headers[TRACE_ID_HEADER] = trace_id
        logger.info(
            "%s %s",
            request.method,
            request.url.path,
            extra={
                "event": "request_completed",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response
