"""应用工厂：注册生命周期、中间件、异常处理器、API 路由与静态页面托管。"""

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.exceptions import register_exception_handlers
from app.core.lifespan import lifespan
from app.core.logging import setup_logging
from app.middleware.trace_id import TraceIdMiddleware

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def no_cache_static(request: Request, call_next) -> Response:
    """HTML/JS/CSS 禁用启发式缓存：静态资源更新后浏览器必须回源校验。

    避免"HTML 已更新而 JS/CSS 仍是旧缓存"的版本错位（会引发前端脚本报错、
    页面交互失效）。no-cache 为标准 HTTP 头，主流浏览器均支持；接口响应不受影响。
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith(("text/html", "application/javascript", "text/css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="康伴 3D 数字人康养陪伴系统",
        lifespan=lifespan,
    )
    app.add_middleware(TraceIdMiddleware)
    app.middleware("http")(no_cache_static)
    register_exception_handlers(app)
    app.include_router(api_router)
    # 静态页面最后挂载到 /（html=True）：API 路由注册在先，优先匹配不冲突
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


# 保留模块级 app 对象，支持 `uvicorn app.main:app`
app = create_app()
