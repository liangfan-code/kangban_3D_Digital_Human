"""健康事件路由（Day8 段1）：采集提交、待确认看板、人工决策、事件快照。

权限：
- 仅护士/医生/管理员；长者范围沿用 ``get_elder_for_user``（范围外 404）；
- 看板按角色过滤（护士楼栋 / 医生关联长者 / 管理员全量）；
- 决策越权 403、重复决策 409、事件不可见 404。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.db.session import get_session
from app.models import Workorder
from app.schemas.health_event import (
    HealthEventCreate,
    HealthEventDecisionRequest,
    HealthEventDecisionResponse,
    HealthEventDetailResponse,
    HealthEventPendingResponse,
    HealthEventResponse,
)
from app.services import health_events as health_service
from app.services.elder import ElderNotFound, ElderPermissionDenied, get_elder_for_user
from app.services.health_events import (
    HealthEventConflict,
    HealthEventForbidden,
    HealthEventNotFound,
)
from app.services.workorder_events import publish_workorder_event

router = APIRouter(tags=["health-events"])

_STAFF = ("nurse", "doctor", "admin")


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


@router.post(
    "/elders/{elder_id}/health-events",
    response_model=HealthEventResponse,
    status_code=status.HTTP_200_OK,
)
async def create_health_event_route(
    elder_id: UUID,
    body: HealthEventCreate,
    user: UserIdentity = Depends(require_role(*_STAFF)),
    session: AsyncSession = Depends(get_session),
) -> HealthEventResponse:
    """提交一次健康采集；缺字段返回下一问（status=collecting）。"""
    elder = await _elder_or_http(session, user, elder_id)
    result = await health_service.submit_intake(
        session,
        elder_id=elder.elder_id,
        source=body.source,
        bp_systolic=body.bp_systolic,
        bp_diastolic=body.bp_diastolic,
        sleep_minutes=body.sleep_minutes,
        mood=body.mood,
        chief_complaint=body.chief_complaint,
    )
    await session.commit()
    if result.workorder_created and result.workorder_id:
        workorder = await session.get(Workorder, UUID(result.workorder_id))
        if workorder is not None:
            await publish_workorder_event("ticket.created", workorder)
    return HealthEventResponse(
        event_id=result.event.event_id,
        thread_id=result.event.thread_id,
        elder_id=result.event.elder_id,
        level=str(result.event.level),
        status=str(result.event.status),
        reply=result.reply,
        required_role=result.required_role,
        missing_fields=list(result.missing_fields),
    )


@router.get("/health-events/pending", response_model=HealthEventPendingResponse)
async def list_pending_health_events_route(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: UserIdentity = Depends(require_role(*_STAFF)),
    session: AsyncSession = Depends(get_session),
) -> HealthEventPendingResponse:
    """待人工确认的健康事件（护士/医生/管理员按各自范围过滤）。"""
    items, total = await health_service.list_pending(
        session,
        role=user.role,
        account_id=user.account_id,
        page=page,
        page_size=page_size,
    )
    return HealthEventPendingResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.post(
    "/health-events/{event_id}/decision",
    response_model=HealthEventDecisionResponse,
)
async def decide_health_event_route(
    event_id: UUID,
    body: HealthEventDecisionRequest,
    user: UserIdentity = Depends(require_role(*_STAFF)),
    session: AsyncSession = Depends(get_session),
) -> HealthEventDecisionResponse:
    """确认 / 升级 / 驳回；状态流转以数据库为准。"""
    try:
        event = await health_service.decide(
            session,
            event_id=event_id,
            action=body.action,
            user=user,
            note=body.note,
        )
    except HealthEventNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except HealthEventForbidden as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except HealthEventConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    await session.commit()
    return HealthEventDecisionResponse(
        event_id=event.event_id,
        status=str(event.status),
        level=str(event.level),
        workorder_id=event.workorder_id,
        referral_id=event.referral_id,
    )


@router.get("/health-events/{event_id}", response_model=HealthEventDetailResponse)
async def get_health_event_route(
    event_id: UUID,
    user: UserIdentity = Depends(require_role(*_STAFF)),
    session: AsyncSession = Depends(get_session),
) -> HealthEventDetailResponse:
    """事件快照（分级理由、确认人、时间）。"""
    try:
        detail = await health_service.get_event_detail(
            session, event_id=event_id, user=user
        )
    except HealthEventNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return HealthEventDetailResponse(**detail)
