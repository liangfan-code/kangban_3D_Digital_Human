"""HTTP 与未处理异常边界。

保留 FastAPI 参数校验 422 与 HTTPException 语义；未处理异常记录带 trace_id
的服务端日志，对外返回不含堆栈或敏感信息的 500。
"""


from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_trace_id

logger = get_logger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常处理器。"""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "HTTP_ERROR",
                    "message": str(exc.detail),
                    "trace_id": get_trace_id(),
                }
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # errors() 可能含不可 JSON 序列化的对象（如 model_validator 的 ValueError 上下文）
        details = [
            {
                "loc": list(e.get("loc", [])),
                "msg": str(e.get("msg", "")),
                "type": str(e.get("type", "")),
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数校验失败",
                    "trace_id": get_trace_id(),
                    "details": details,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "未处理异常: %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
            extra={"event": "unhandled_exception"},
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务内部错误",
                    "trace_id": get_trace_id(),
                }
            },
        )
