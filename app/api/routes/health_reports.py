"""健康趋势与家属日报路由（Day8 段3，只读）。

权限：沿用长者范围（范围外 404）+ 健康数据 ACL（无授权 403）；
家属必须持有 ``basic``/``confidential`` 授权才能看到体征趋势与日报。
"""

import base64
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, get_current_user
from app.db.session import get_session
from app.schemas.health_report import (
    HealthDailyReportResponse,
    HealthTrendResponse,
)
from app.services import health_reports as report_service
from app.services.elder import ElderNotFound, ElderPermissionDenied, get_elder_for_user
from app.services.health_profile import viewer_level

router = APIRouter(tags=["health-reports"])


async def _elder_or_http(session: AsyncSession, user: UserIdentity, elder_id: UUID):
    try:
        return await get_elder_for_user(session, user, elder_id)
    except ElderPermissionDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except ElderNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


async def _require_health_view(session: AsyncSession, user: UserIdentity, elder) -> None:
    level = await viewer_level(session, user, elder.elder_id)
    if level is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无该长者的健康数据查看授权，请联系管理员开通",
        )


@router.get(
    "/elders/{elder_id}/health-trend",
    response_model=HealthTrendResponse,
)
async def get_health_trend_route(
    elder_id: UUID,
    days: int = Query(default=14, ge=1, le=30),
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HealthTrendResponse:
    """近 N 天血压/睡眠趋势；缺测日为 null。"""
    elder = await _elder_or_http(session, user, elder_id)
    await _require_health_view(session, user, elder)
    data = await report_service.build_trend(session, elder.elder_id, days=days)
    return HealthTrendResponse(elder_id=elder.elder_id, **data)


@router.get(
    "/elders/{elder_id}/health-daily-report",
    response_model=HealthDailyReportResponse,
)
async def get_health_daily_report_route(
    elder_id: UUID,
    day: date | None = Query(default=None, alias="date"),
    tts: bool = Query(default=False),
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HealthDailyReportResponse:
    """单日日报文本；``tts=1`` 时附带 16-bit PCM 朗读音频。"""
    elder = await _elder_or_http(session, user, elder_id)
    await _require_health_view(session, user, elder)
    data = await report_service.build_daily_report(session, elder.elder_id, day=day)

    audio_pcm: str | None = None
    sample_rate: int | None = None
    degraded: str | None = None
    if tts:
        synth = await report_service.synthesize_report_audio(data["text"])
        if synth is None:
            degraded = "tts_unavailable"
        else:
            pcm, rate = synth
            audio_pcm = base64.b64encode(pcm).decode("ascii")
            sample_rate = rate

    return HealthDailyReportResponse(
        **data,
        audio_pcm_base64=audio_pcm,
        audio_sample_rate=sample_rate,
        tts_degraded=degraded,
    )
