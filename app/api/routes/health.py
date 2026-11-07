"""`/health` 存活检查路由：不访问数据库。"""

from fastapi import APIRouter

from app.schemas.health import HealthResponse
from app.services.health import build_health_response

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return await build_health_response()
