"""实时会话路由：POST /api/v1/rtc/offer（TDD §7.2，设备 Token 专属）。

权限口径（验收项 11）：人 JWT 到此 403（token_type 互斥由依赖层保证）；
offer 前无需 WS 已连接——音频推送到 voice 会话管理器时按 device_id 查找，
未建会话时音频被丢弃（不报错，等 WS 建连后自然接上）。
"""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_device
from app.realtime.session import RealtimeRegistry, RtcAnswerOut, RtcOfferIn, VoiceRealtimeSession

router = APIRouter(tags=["realtime"])

_registry = RealtimeRegistry()


@router.post("/rtc/offer", response_model=RtcAnswerOut)
async def rtc_offer(
    payload: RtcOfferIn,
    device_id: UUID = Depends(get_current_device),
) -> RtcAnswerOut:
    """SDP offer → answer；设备身份建立实时会话上下文。"""
    session = VoiceRealtimeSession(device_id)
    answer = await session.accept_offer(payload.sdp, payload.type)
    await _registry.replace(device_id, session)
    return answer
