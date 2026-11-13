"""API 路由聚合。"""

from fastapi import APIRouter

from app.api.routes import (
    admin,
    auth,
    conversation_admin,
    conversations,
    devices,
    elders,
    health,
    health_events,
    health_profile,
    health_reports,
    hitl,
    medication,
    memory,
    voice,
    voiceprint_eval,
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
api_router.include_router(health_events.router, prefix="/api/v1")  # Day8 健康事件决策
api_router.include_router(health_reports.router, prefix="/api/v1")  # Day8 趋势 + 家属日报
api_router.include_router(voice.router, prefix="/api/v1")  # Day3 语音：WS + enroll
api_router.include_router(conversations.router, prefix="/api/v1")  # 对话历史可视化 段2
api_router.include_router(hitl.router, prefix="/api/v1")  # Day6.3 HITL 待确认队列
api_router.include_router(workorders.staff_router, prefix="/api/v1")
api_router.include_router(workorders.admin_router, prefix="/api/v1")
api_router.include_router(workorders.family_router, prefix="/api/v1")
api_router.include_router(workorders.router, prefix="/api/v1")  # 员工端 WS /staff/ws
api_router.include_router(medication.router, prefix="/api/v1")  # Day7 用药计划
api_router.include_router(medication.family_router, prefix="/api/v1")
api_router.include_router(memory.router, prefix="/api/v1")  # 对话历史可视化 段3 记忆只读
api_router.include_router(conversation_admin.router, prefix="/api/v1")  # 段4 会话治理（管理员）
api_router.include_router(voiceprint_eval.router, prefix="/api/v1")  # Day8 声纹评测报告（管理员）
api_router.include_router(realtime_router, prefix="/api/v1")  # Day3 WebRTC 信令
