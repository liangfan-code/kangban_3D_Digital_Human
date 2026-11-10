"""API 路由聚合。"""

from fastapi import APIRouter

from app.api.routes import (
    admin,
    auth,
    devices,
    elders,
    health,
    health_profile,
    hitl,
    voice,
    workorders,
)
from app.realtime.routes import router as realtime_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router, prefix="/api/v1")
api_router.include_router(elders.router, prefix="/api/v1")
api_router.include_router(devices.router, prefix="/api/v1")
api_router.include_router(devices.admin_router, prefix="/api/v1")
api_router.include_router(admin.router, prefix="/api/v1")  # /admin/assignments/*
api_router.include_router(admin.rooms_router, prefix="/api/v1")
api_router.include_router(admin.personas_router, prefix="/api/v1")
api_router.include_router(admin.accounts_router, prefix="/api/v1")
api_router.include_router(health_profile.router, prefix="/api/v1")  # 健康档案主体
api_router.include_router(health_profile.nurse_router, prefix="/api/v1")
api_router.include_router(health_profile.admin_router, prefix="/api/v1")
api_router.include_router(health_profile.config_router, prefix="/api/v1")
api_router.include_router(voice.router, prefix="/api/v1")  # Day3 语音：WS + enroll
api_router.include_router(hitl.router, prefix="/api/v1")  # Day6.3 HITL 待确认队列
api_router.include_router(workorders.staff_router, prefix="/api/v1")
api_router.include_router(workorders.admin_router, prefix="/api/v1")
api_router.include_router(workorders.family_router, prefix="/api/v1")
api_router.include_router(workorders.router, prefix="/api/v1")  # 员工端 WS /staff/ws
api_router.include_router(realtime_router, prefix="/api/v1")  # Day3 WebRTC 信令
