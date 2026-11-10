"""工单 HTTP 接口与员工端实时通道（Day8 Task 4 / Task 5）。

- 员工（doctor/nurse）：列表 / 详情（含对话快照）/ 已读 / 提交方案 / 未处理计数 / WS；
- 管理员：全量列表 / 详情 / 终审关闭（双角色 handled 才允许）；
- 家属：绑定老人列表与详情（只读，不含对话快照，越权 404）；
- 身份一律来自服务端 Token，elder_id 只从路径或工单本身取，信任客户端不传身份字段。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from uuid import UUID

import jwt
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_admin, require_role
from app.auth.jwt import USER_TOKEN_TYPE, TokenTypeMismatch, decode_token
from app.core.logging import get_logger
from app.db.session import get_session
from app.models import Account, Workorder, WorkorderDisposition
from app.models.enums import (
    AccountRole,
    AccountStatus,
    WorkorderDispositionStatus,
)
from app.schemas.workorder import (
    CloseRequest,
    DispositionRequest,
    FamilyWorkorderDetail,
    WorkorderCountsResponse,
    WorkorderDetail,
    WorkorderDispositionOut,
    WorkorderListItem,
    WorkorderListResponse,
)
from app.services import workorder as svc
from app.services.workorder_events import workorder_event_hub

logger = get_logger(__name__)

router = APIRouter(tags=["workorders"])
staff_router = APIRouter(prefix="/staff/workorders", tags=["workorders"])
admin_router = APIRouter(prefix="/admin/workorders", tags=["workorders"])
family_router = APIRouter(prefix="/family", tags=["workorders"])

_WS_AUTH_TIMEOUT_SECONDS = 10
_WS_INVALID_AUTH = 4401
_WS_ROLE_FORBIDDEN = 4403
_STAFF_ROLES = (AccountRole.DOCTOR, AccountRole.NURSE)


# ---------------------------------------------------------------------------
# 错误映射与响应组装
# ---------------------------------------------------------------------------


def _http_error(exc: Exception) -> HTTPException:
    """领域异常 → HTTP：404 隐藏存在性，403 越权，409 状态冲突，422 入参不合法。"""
    if isinstance(exc, svc.WorkOrderNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, svc.WorkOrderForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, svc.WorkOrderConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, svc.WorkOrderValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="内部错误")


def _conflict_response(exc: svc.WorkOrderConflict, pending: Sequence[str]) -> JSONResponse:
    """409 响应体显式带 code 与未完成角色，便于前端禁用关闭按钮并提示缺谁。"""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {
                "code": "WORKORDER_CONFLICT",
                "message": str(exc),
                "unfinished_roles": list(pending),
            }
        },
    )


def _pending_roles(dispositions: Sequence[WorkorderDisposition]) -> list[str]:
    handled = {
        row.role
        for row in dispositions
        if row.status == WorkorderDispositionStatus.HANDLED
    }
    return [role for role in svc.STAFF_ROLES if role not in handled]


def _item_from(
    workorder: Workorder, dispositions: Sequence[WorkorderDisposition], role: str
) -> WorkorderListItem:
    mine = next((row for row in dispositions if row.role == role), None)
    return WorkorderListItem(
        id=workorder.workorder_id,
        elder_id=workorder.elder_id,
        category=workorder.sensitive_category,
        status=workorder.status,
        priority=workorder.priority,
        occurrence_count=workorder.occurrence_count,
        created_at=workorder.created_at,
        updated_at=workorder.updated_at,
        dispositions=[WorkorderDispositionOut.model_validate(row) for row in dispositions],
        my_status=mine.status if mine is not None else None,
        pending=bool(mine is not None and mine.status != WorkorderDispositionStatus.HANDLED),
    )


def _detail_from(
    workorder: Workorder, dispositions: Sequence[WorkorderDisposition], role: str
) -> WorkorderDetail:
    return WorkorderDetail(
        **_item_from(workorder, dispositions, role).model_dump(),
        title=workorder.title,
        description=workorder.description,
        context_snapshot=workorder.context_snapshot,
        resolved_at=workorder.resolved_at,
        closed_by=workorder.closed_by,
        closed_note=workorder.closed_note,
    )


def _family_detail_from(
    workorder: Workorder, dispositions: Sequence[WorkorderDisposition], role: str
) -> FamilyWorkorderDetail:
    return FamilyWorkorderDetail(
        **_item_from(workorder, dispositions, role).model_dump(),
        title=workorder.title,
        description=workorder.description,
        resolved_at=workorder.resolved_at,
        closed_by=workorder.closed_by,
        closed_note=workorder.closed_note,
    )


async def _reload_after_write(
    session: AsyncSession, workorder_id: UUID, role: str, account_id: UUID
) -> Workorder:
    """写操作提交后重读工单：``updated_at`` 由数据库 onupdate 生成，需显式刷新避免惰性加载。"""
    workorder = await svc.get_work_order(
        session, workorder_id=workorder_id, role=role, account_id=account_id
    )
    await session.refresh(workorder)
    return workorder


async def _load_list_response(
    session: AsyncSession,
    *,
    role: str,
    account_id: UUID,
    elder_id: UUID | None,
    q: str | None,
    status_filter: str | None,
    sort: str,
    order: str,
    page: int,
    page_size: int,
) -> WorkorderListResponse:
    try:
        rows, total = await svc.list_work_orders(
            session,
            role=role,
            account_id=account_id,
            elder_id=elder_id,
            q=q,
            status=status_filter,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    grouped = await svc.list_dispositions(session, [row.workorder_id for row in rows])
    return WorkorderListResponse(
        role=role,
        items=[
            _item_from(row, grouped.get(row.workorder_id, []), role) for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 员工：doctor / nurse
# ---------------------------------------------------------------------------


@staff_router.get("", response_model=WorkorderListResponse)
async def list_staff_workorders(
    q: str | None = Query(default=None, max_length=100, description="关键词搜索"),
    sort: str = Query(default="created_at", description="created_at | priority"),
    order: str = Query(default="desc", description="asc | desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status", description="工单状态"),
    user: UserIdentity = Depends(require_role(*_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListResponse:
    """员工可见范围内的工单列表；搜索/排序/分页在 SQL 层完成。"""
    return await _load_list_response(
        session,
        role=user.role,
        account_id=user.account_id,
        elder_id=None,
        q=q,
        status_filter=status_filter,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )


@staff_router.get("/counts", response_model=WorkorderCountsResponse)
async def staff_workorder_counts(
    user: UserIdentity = Depends(require_role(*_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderCountsResponse:
    """当前角色未处理（处理项未 handled 且工单未关闭）数量；WS 断线时前端轮询此接口。"""
    unhandled = await svc.count_open_by_role(
        session, role=user.role, account_id=user.account_id
    )
    return WorkorderCountsResponse(role=user.role, unhandled=unhandled)


@staff_router.get("/{workorder_id}", response_model=WorkorderDetail)
async def staff_workorder_detail(
    workorder_id: UUID,
    user: UserIdentity = Depends(require_role(*_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderDetail:
    """员工详情（含对话快照）；不在负责范围一律 404。"""
    try:
        workorder = await svc.get_work_order(
            session,
            workorder_id=workorder_id,
            role=user.role,
            account_id=user.account_id,
        )
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    grouped = await svc.list_dispositions(session, [workorder.workorder_id])
    return _detail_from(
        workorder, grouped.get(workorder.workorder_id, []), user.role
    )


@staff_router.post("/{workorder_id}/read", response_model=WorkorderListItem)
async def staff_mark_read(
    workorder_id: UUID,
    user: UserIdentity = Depends(require_role(*_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListItem:
    """把当前角色的处理项标记为已读（幂等，handled 不回退）。"""
    try:
        await svc.mark_read(
            session,
            workorder_id=workorder_id,
            role=user.role,
            account_id=user.account_id,
        )
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    await session.commit()
    workorder = await _reload_after_write(
        session, workorder_id, user.role, user.account_id
    )
    grouped = await svc.list_dispositions(session, [workorder_id])
    return _item_from(workorder, grouped.get(workorder_id, []), user.role)


@staff_router.post("/{workorder_id}/disposition", response_model=WorkorderListItem)
async def staff_submit_disposition(
    workorder_id: UUID,
    body: DispositionRequest,
    user: UserIdentity = Depends(require_role(*_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListItem:
    """提交本角色处理方案（处理项 → handled）；重复提交或已关闭 409。"""
    try:
        await svc.submit_disposition(
            session,
            workorder_id=workorder_id,
            role=user.role,
            account_id=user.account_id,
            plan_text=body.plan_text,
        )
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    await session.commit()
    workorder = await _reload_after_write(
        session, workorder_id, user.role, user.account_id
    )
    grouped = await svc.list_dispositions(session, [workorder_id])
    return _item_from(workorder, grouped.get(workorder_id, []), user.role)


# ---------------------------------------------------------------------------
# 管理员
# ---------------------------------------------------------------------------


@admin_router.get("", response_model=WorkorderListResponse)
async def list_admin_workorders(
    q: str | None = Query(default=None, max_length=100, description="关键词搜索"),
    sort: str = Query(default="created_at", description="created_at | priority"),
    order: str = Query(default="desc", description="asc | desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status", description="工单状态"),
    user: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListResponse:
    """管理员全量工单列表（含历史），同样的搜索/排序/分页。"""
    return await _load_list_response(
        session,
        role=user.role,
        account_id=user.account_id,
        elder_id=None,
        q=q,
        status_filter=status_filter,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )


@admin_router.get("/{workorder_id}", response_model=WorkorderDetail)
async def admin_workorder_detail(
    workorder_id: UUID,
    user: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> WorkorderDetail:
    """管理员详情：全量可见，含对话快照与双方方案。"""
    try:
        workorder = await svc.get_work_order(
            session,
            workorder_id=workorder_id,
            role=user.role,
            account_id=user.account_id,
        )
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    grouped = await svc.list_dispositions(session, [workorder.workorder_id])
    return _detail_from(
        workorder, grouped.get(workorder.workorder_id, []), user.role
    )


@admin_router.post("/{workorder_id}/close", response_model=WorkorderDetail)
async def admin_close_workorder(
    workorder_id: UUID,
    body: CloseRequest,
    user: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """终审关闭：仅 doctor 与 nurse 都 handled 时成功；否则 409 并列出未完成角色。"""
    try:
        await svc.close_work_order(
            session,
            workorder_id=workorder_id,
            admin_account_id=user.account_id,
            note=body.note,
        )
    except svc.WorkOrderConflict as exc:
        grouped = await svc.list_dispositions(session, [workorder_id])
        return _conflict_response(exc, _pending_roles(grouped.get(workorder_id, [])))
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    await session.commit()
    workorder = await _reload_after_write(
        session, workorder_id, user.role, user.account_id
    )
    grouped = await svc.list_dispositions(session, [workorder_id])
    return _detail_from(workorder, grouped.get(workorder_id, []), user.role)


# ---------------------------------------------------------------------------
# 家属（只读）
# ---------------------------------------------------------------------------


@family_router.get("/elders/{elder_id}/workorders", response_model=WorkorderListResponse)
async def list_family_workorders(
    elder_id: UUID,
    q: str | None = Query(default=None, max_length=100, description="关键词搜索"),
    sort: str = Query(default="created_at", description="created_at | priority"),
    order: str = Query(default="desc", description="asc | desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: UserIdentity = Depends(require_role(AccountRole.FAMILY)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListResponse:
    """家属只读列表：校验 FamilyElderLink，越权按 404 处理（不暴露存在性）。"""
    return await _load_list_response(
        session,
        role=user.role,
        account_id=user.account_id,
        elder_id=elder_id,
        q=q,
        status_filter=None,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )


@family_router.get("/workorders/{workorder_id}", response_model=FamilyWorkorderDetail)
async def family_workorder_detail(
    workorder_id: UUID,
    user: UserIdentity = Depends(require_role(AccountRole.FAMILY)),
    session: AsyncSession = Depends(get_session),
) -> FamilyWorkorderDetail:
    """家属详情：只含状态与方案全文，响应体不含 context_snapshot。"""
    try:
        workorder = await svc.get_work_order(
            session,
            workorder_id=workorder_id,
            role=user.role,
            account_id=user.account_id,
        )
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    grouped = await svc.list_dispositions(session, [workorder.workorder_id])
    return _family_detail_from(
        workorder, grouped.get(workorder.workorder_id, []), user.role
    )


# ---------------------------------------------------------------------------
# 员工端 WS：首帧 auth（人 JWT），仅 doctor / nurse
# ---------------------------------------------------------------------------


async def _authenticate_staff_ws(
    ws: WebSocket, session: AsyncSession
) -> UserIdentity | None:
    """首帧鉴权：``{"type": "auth", "token": "<access_token>"}``。

    失败即关闭：无效/过期 4401，角色不符 4403（不静默失败，前端据此回退轮询）。
    """
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_WS_AUTH_TIMEOUT_SECONDS)
    except TimeoutError:
        await ws.close(code=_WS_INVALID_AUTH, reason="auth timeout")
        return None
    except WebSocketDisconnect:
        return None

    try:
        message = json.loads(raw)
    except ValueError:
        await ws.close(code=_WS_INVALID_AUTH, reason="auth message must be json")
        return None
    token = message.get("token") if message.get("type") == "auth" else None
    if not isinstance(token, str) or not token:
        await ws.close(code=_WS_INVALID_AUTH, reason="first message must be auth with token")
        return None

    try:
        payload = decode_token(token, USER_TOKEN_TYPE)
    except TokenTypeMismatch:
        await ws.close(code=_WS_ROLE_FORBIDDEN, reason="device token not allowed")
        return None
    except jwt.InvalidTokenError:
        await ws.close(code=_WS_INVALID_AUTH, reason="invalid or expired token")
        return None

    try:
        account_id = UUID(payload.get("sub", ""))
    except (ValueError, TypeError):
        await ws.close(code=_WS_INVALID_AUTH, reason="invalid subject")
        return None

    account = (
        await session.execute(select(Account).where(Account.account_id == account_id))
    ).scalar_one_or_none()
    if account is None or account.status != AccountStatus.ACTIVE:
        await ws.close(code=_WS_INVALID_AUTH, reason="account inactive")
        return None
    if account.role not in _STAFF_ROLES:
        await ws.close(code=_WS_ROLE_FORBIDDEN, reason="role not allowed")
        return None
    return UserIdentity(
        account_id=account.account_id,
        role=account.role,
        display_name=account.display_name,
    )


@router.websocket("/staff/ws")
async def staff_workorder_ws(
    ws: WebSocket, session: AsyncSession = Depends(get_session)
) -> None:
    """工单事件通道：首帧鉴权成功后注册订阅；断开时清理，不影响其他账号。"""
    await ws.accept()
    user = await _authenticate_staff_ws(ws, session)
    if user is None:
        return

    workorder_event_hub.register(user.account_id, ws)
    logger.info(
        "员工工单 WS 已连接",
        extra={
            "event": "staff_ws_connected",
            "actor_id": str(user.account_id),
            "actor_role": user.role,
        },
    )
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                continue  # 该通道只收 JSON 心跳，不收音频/二进制
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if event.get("type") == "ping":
                await ws.send_json({"type": "pong", "data": {}})
    except WebSocketDisconnect:
        pass
    finally:
        workorder_event_hub.unregister(user.account_id, ws)
        logger.info(
            "员工工单 WS 已断开",
            extra={
                "event": "staff_ws_disconnected",
                "actor_id": str(user.account_id),
                "actor_role": user.role,
            },
        )
