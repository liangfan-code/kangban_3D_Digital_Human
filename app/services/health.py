"""`/health` 的最小响应逻辑：纯存活检查，不访问数据库。"""

from app.core.config import get_settings
from app.core.logging import get_trace_id
from app.schemas.health import HealthResponse


async def build_health_response() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        app_env=settings.app_env,
        trace_id=get_trace_id(),
    )
