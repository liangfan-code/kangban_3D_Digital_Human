"""工单 HTTP 接口与四端实时通道（Day8 Task 4 / Task 5；Day7 段1.5 四端化）。

- 员工（doctor/nurse）：列表 / 详情（含对话快照）/ 已读 / 提交方案 / 未处理计数 / WS；
- 管理员：全量列表 / 详情 / 终审关闭（双角色 handled 才允许）；
- 家属：绑定老人列表与详情（只读，不含对话快照，越权 404）；
- WS 四端接入（doctor/nurse/admin/family，与广播共用 ``WS_ROLES``）；
  管理员与家属只收刷新信号，不发 ticket.counts；
- 工单事件推送一律在本接口 commit() 之后发布（服务层不做事务内推送）；
- 身份一律来自服务端 Token，elder_id 只从路径或工单本身取，信任客户端不传身份字段。
"""

from __future__ import annotations

import asyncio
import json
import time
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
from app.services.workorder_events import (
    WS_ROLES,
    publish_workorder_event,
    workorder_event_hub,
)

logger = get_logger(__name__)

router = APIRouter(tags=["workorders"])
staff_router = APIRouter(prefix="/staff/workorders", tags=["workorders"])
admin_router = APIRouter(prefix="/admin/workorders", tags=["workorders"])
family_router = APIRouter(prefix="/family", tags=["workorders"])

_WS_AUTH_TIMEOUT_SECONDS = 10
_WS_INVALID_AUTH = 4401
_WS_ROLE_FORBIDDEN = 4403
_WS_CONN_LIMIT = 4409
# WS 四端角色唯一来源是 workorder_events.WS_ROLES（与广播同源，禁止另写一份）；
# 此处仅保留 HTTP 员工端点的鉴权元组（require_role 用，与 WS 无关）。
_HTTP_STAFF_ROLES = (AccountRole.DOCTOR, AccountRole.NURSE)


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
    workorder: Workorder,
    dispositions: Sequence[WorkorderDisposition],
    role: str,
    receipts: dict[UUID, object] | None = None,
) -> WorkorderListItem:
    mine = next((row for row in dispositions if row.role == role), None)
    # 已读按账号：staff 从 workorder_reads 回执取值；admin/family 不读不显
    receipt_at = (receipts or {}).get(workorder.workorder_id)
    is_staff = role in svc.STAFF_ROLES
    return WorkorderListItem(
        id=workorder.workorder_id,
        elder_id=workorder.elder_id,
        type=workorder.type,
        title=workorder.title,
        category=workorder.sensitive_category,
        status=workorder.status,
        priority=workorder.priority,
        occurrence_count=workorder.occurrence_count,
        order_kind=workorder.order_kind,
        medication_plan_id=workorder.medication_plan_id,
        medication_order_id=workorder.medication_order_id,
        effective_from=workorder.effective_from,
        effective_until=workorder.effective_until,
        is_read=receipt_at is not None if is_staff else None,
        read_at=receipt_at if is_staff else None,
        created_at=workorder.created_at,
        updated_at=workorder.updated_at,
        dispositions=[WorkorderDispositionOut.model_validate(row) for row in dispositions],
        my_status=mine.status if mine is not None else None,
        pending=bool(mine is not None and mine.status != WorkorderDispositionStatus.HANDLED),
    )


def _detail_from(
    workorder: Workorder,
    dispositions: Sequence[WorkorderDisposition],
    role: str,
    receipts: dict[UUID, object] | None = None,
) -> WorkorderDetail:
    return WorkorderDetail(
        **_item_from(workorder, dispositions, role, receipts).model_dump(),
        description=workorder.description,
        context_snapshot=workorder.context_snapshot,
        resolved_at=workorder.resolved_at,
        closed_by=workorder.closed_by,
        closed_note=workorder.closed_note,
    )


def _family_detail_from(
    workorder: Workorder,
    dispositions: Sequence[WorkorderDisposition],
    role: str,
    receipts: dict[UUID, object] | None = None,
) -> FamilyWorkorderDetail:
    return FamilyWorkorderDetail(
        **_item_from(workorder, dispositions, role, receipts).model_dump(),
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
    mine: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> WorkorderListResponse:
    try:
        rows, total = await svc.list_work_orders(
            session,
            role=role,
            account_id=account_id,
            elder_id=elder_id,
            q=q,
            status=status_filter,
            mine=mine,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
        )
    except svc.WorkOrderError as exc:
        raise _http_error(exc) from exc
    workorder_ids = [row.workorder_id for row in rows]
    grouped = await svc.list_dispositions(session, workorder_ids)
    receipts = await svc.list_receipts(
        session, account_id=account_id, workorder_ids=workorder_ids
    )
    return WorkorderListResponse(
        role=role,
        items=[
            _item_from(row, grouped.get(row.workorder_id, []), role, receipts)
            for row in rows
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
    mine: str | None = Query(default=None, description="unread | read | handled"),
    user: UserIdentity = Depends(require_role(*_HTTP_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListResponse:
    """员工可见范围内的工单列表；搜索/筛选/排序/分页在 SQL 层完成。"""
    return await _load_list_response(
        session,
        role=user.role,
        account_id=user.account_id,
        elder_id=None,
        q=q,
        status_filter=status_filter,
        mine=mine,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )


@staff_router.get("/counts", response_model=WorkorderCountsResponse)
async def staff_workorder_counts(
    user: UserIdentity = Depends(require_role(*_HTTP_STAFF_ROLES)),
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
    user: UserIdentity = Depends(require_role(*_HTTP_STAFF_ROLES)),
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
    receipts = await svc.list_receipts(
        session, account_id=user.account_id, workorder_ids=[workorder.workorder_id]
    )
    return _detail_from(
        workorder,
        grouped.get(workorder.workorder_id, []),
        user.role,
        receipts,
    )


@staff_router.post("/{workorder_id}/read", response_model=WorkorderListItem)
async def staff_mark_read(
    workorder_id: UUID,
    user: UserIdentity = Depends(require_role(*_HTTP_STAFF_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListItem:
    """按账号写已读回执并把当前角色处理项标记为已读（幂等，handled 不回退）。"""
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
    receipts = await svc.list_receipts(
        session, account_id=user.account_id, workorder_ids=[workorder_id]
    )
    await publish_workorder_event("ticket.updated", workorder)
    return _item_from(workorder, grouped.get(workorder_id, []), user.role, receipts)


@staff_router.post("/{workorder_id}/disposition", response_model=WorkorderListItem)
async def staff_submit_disposition(
    workorder_id: UUID,
    body: DispositionRequest,
    user: UserIdentity = Depends(require_role(*_HTTP_STAFF_ROLES)),
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
    receipts = await svc.list_receipts(
        session, account_id=user.account_id, workorder_ids=[workorder_id]
    )
    await publish_workorder_event("ticket.updated", workorder)
    return _item_from(workorder, grouped.get(workorder_id, []), user.role, receipts)


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
    mine: str | None = Query(default=None, description="admin 不支持，传值返回 422"),
    user: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListResponse:
    """管理员全量工单列表（含历史），同样的搜索/排序/分页。"""
    if mine is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="admin 不支持 mine 筛选",
        )
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
    receipts = await svc.list_receipts(
        session, account_id=user.account_id, workorder_ids=[workorder.workorder_id]
    )
    return _detail_from(
        workorder,
        grouped.get(workorder.workorder_id, []),
        user.role,
        receipts,
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
    await publish_workorder_event("ticket.resolved", workorder)
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
    status_filter: str | None = Query(default=None, alias="status", description="工单状态"),
    mine: str | None = Query(default=None, description="family 不支持，传值返回 422"),
    user: UserIdentity = Depends(require_role(AccountRole.FAMILY)),
    session: AsyncSession = Depends(get_session),
) -> WorkorderListResponse:
    """家属只读列表：校验 FamilyElderLink，越权按 404 处理（不暴露存在性）。"""
    if mine is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="family 不支持 mine 筛选",
        )
    return await _load_list_response(
        session,
        role=user.role,
        account_id=user.account_id,
        elder_id=elder_id,
        q=q,
        status_filter=status_filter,
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
# 四端 WS：首帧 auth（人 JWT），doctor / nurse / admin / family（WS_ROLES）
# ---------------------------------------------------------------------------


async def _authenticate_ws(
    ws: WebSocket, session: AsyncSession
) -> tuple[UserIdentity, float] | None:
    """首帧鉴权：``{"type": "auth", "token": "<access_token>"}``。

    失败即关闭：无效/过期/账号停用 4401，角色不在 WS_ROLES 4403
    （不静默失败，前端据此回退轮询）。成功返回身份与 token 过期时间戳。
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
    if account.role not in WS_ROLES:
        await ws.close(code=_WS_ROLE_FORBIDDEN, reason="role not allowed")
        return None
    try:
        exp = float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    return (
        UserIdentity(
            account_id=account.account_id,
            role=account.role,
            display_name=account.display_name,
        ),
        exp,
    )


@router.websocket("/staff/ws")
async def staff_workorder_ws(
    ws: WebSocket, session: AsyncSession = Depends(get_session)
) -> None:
    """工单事件通道（四端）：首帧鉴权成功后注册订阅；断开时清理，不影响其他账号。

    - 按 JWT exp 到期主动 close(4401)；账号停用不实时踢，下次重连拒绝；
    - 单账号连接上限 3，超出时关闭最旧连接（close 4409）；
    - 客户端心跳 ping/pong；本通道只收 JSON 心跳，不收音频/二进制。
    """
    await ws.accept()
    auth = await _authenticate_ws(ws, session)
    if auth is None:
        return
    user, exp = auth

    evicted = workorder_event_hub.register(user.account_id, ws)
    for old in evicted:
        logger.info(
            "工单 WS 连接数超限，关闭最旧连接",
            extra={
                "event": "workorder_ws_limit_exceeded",
                "actor_id": str(user.account_id),
                "actor_role": user.role,
            },
        )
        try:
            await old.close(code=_WS_CONN_LIMIT, reason="connection limit")
        except Exception:  # noqa: BLE001 —— 旧连接可能已断开
            pass

    logger.info(
        "工单 WS 已连接",
        extra={
            "event": "workorder_ws_connected",
            "actor_id": str(user.account_id),
            "actor_role": user.role,
        },
    )
    expires_in = max(0.0, exp - time.time())
    try:
        while True:
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=expires_in)
            except TimeoutError:
                logger.info(
                    "工单 WS token 过期，服务端主动关闭",
                    extra={
                        "event": "workorder_ws_expired",
                        "actor_id": str(user.account_id),
                        "actor_role": user.role,
                    },
                )
                await ws.close(code=_WS_INVALID_AUTH, reason="token expired")
                break
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
            "工单 WS 已断开",
            extra={
                "event": "workorder_ws_disconnected",
                "actor_id": str(user.account_id),
                "actor_role": user.role,
            },
        )
