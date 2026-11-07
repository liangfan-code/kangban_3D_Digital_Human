"""设备路由：绑定、一次性注册凭证签发、设备自查、admin 档案与凭证管理。"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, get_current_device, require_admin
from app.auth.device_token import revoke_device_token
from app.core.logging import get_logger
from app.db.session import get_session
from app.models import Device, DeviceToken
from app.schemas.device import (
    BindingUpdate,
    DeviceCreate,
    DeviceResponse,
    DeviceSelfResponse,
    DeviceTokenItem,
    DeviceUpdate,
    RegistrationTokenResponse,
)
from app.services.device import (
    DeviceConflict,
    DeviceNotFound,
    DeviceValidationError,
    bind_device,
    create_device,
    deactivate_device,
    device_self_view,
    get_device_or_404,
    issue_registration_token,
    list_device_tokens,
    revoke_all_device_tokens,
    update_device,
)

router = APIRouter(prefix="/devices", tags=["devices"])
# 管理员子路由：设备档案/凭证管理挂在 /admin/devices 下（对齐 Day2 文档 6.2 接口清单）
admin_router = APIRouter(prefix="/admin/devices", tags=["admin"])
logger = get_logger(__name__)


@router.put("/{device_id}/binding", response_model=DeviceResponse)
async def bind_device_route(
    device_id: uuid.UUID,
    body: BindingUpdate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DeviceResponse:
    """绑定设备到房间/床位（仅管理员）；床位冲突 409、校验失败 422。"""
    device = (
        await session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    try:
        device = await bind_device(session, device, body, actor)
    except DeviceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except DeviceConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    if idempotency_key:
        logger.info(
            "binding idempotency key",
            extra={
                "event": "idempotency_key",
                "object_type": "device",
                "object_id": str(device_id),
                "idempotency_key": idempotency_key,
            },
        )
    return DeviceResponse.model_validate(device)


@admin_router.post("/{device_id}/registration-tokens", response_model=RegistrationTokenResponse)
async def issue_registration_token_route(
    device_id: uuid.UUID,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RegistrationTokenResponse:
    """签发一次性注册凭证（默认 24 小时有效），明文仅此一次返回。"""
    try:
        issued = await issue_registration_token(session, device_id, actor)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await session.commit()
    return RegistrationTokenResponse(
        device_id=device_id,
        registration_token=issued.plaintext,
        expires_at=issued.token.expires_at,
    )


# ---- admin 设备档案 CRUD ----


@admin_router.post("", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device_route(
    body: DeviceCreate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DeviceResponse:
    """建设备（仅管理员）：编码唯一 409、房间校验 422；初始 pending。"""
    try:
        device = await create_device(session, body, actor)
    except DeviceConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DeviceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return DeviceResponse.model_validate(device)


@admin_router.get("", response_model=list[DeviceResponse])
async def list_devices_route(
    _admin: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[DeviceResponse]:
    rows = (await session.execute(select(Device).order_by(Device.created_at))).scalars().all()
    return [DeviceResponse.model_validate(d) for d in rows]


@admin_router.get("/{device_id}", response_model=DeviceResponse)
async def get_device_route(
    device_id: uuid.UUID,
    _admin: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DeviceResponse:
    try:
        device = await get_device_or_404(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DeviceResponse.model_validate(device)


@admin_router.put("/{device_id}", response_model=DeviceResponse)
async def update_device_route(
    device_id: uuid.UUID,
    body: DeviceUpdate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DeviceResponse:
    """更新设备（仅管理员）：device_code 不可改；retired 仅经 DELETE 设置。"""
    try:
        device = await get_device_or_404(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        device = await update_device(session, device, body, actor)
    except DeviceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except DeviceConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return DeviceResponse.model_validate(device)


@admin_router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device_route(
    device_id: uuid.UUID,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """软删设备（仅管理员）：置 retired 并吊销全部凭证；幂等 204。"""
    try:
        device = await get_device_or_404(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await deactivate_device(session, device, actor)
    await session.commit()


# ---- admin 设备凭证管理 ----


@admin_router.get("/{device_id}/tokens", response_model=list[DeviceTokenItem])
async def list_device_tokens_route(
    device_id: uuid.UUID,
    _admin: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[DeviceTokenItem]:
    """设备凭证列表（不含任何明文/哈希）。"""
    try:
        await get_device_or_404(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    rows = await list_device_tokens(session, device_id)
    return [DeviceTokenItem.model_validate(t) for t in rows]


@admin_router.delete("/{device_id}/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device_token_route(
    device_id: uuid.UUID,
    token_id: uuid.UUID,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """吊销单条设备凭证（幂等 204）；凭证与设备不匹配按 404 处理。"""
    try:
        device = await get_device_or_404(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    target = (
        await session.execute(
            select(DeviceToken).where(
                DeviceToken.token_id == token_id, DeviceToken.device_id == device.device_id
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="凭证不存在")
    await revoke_device_token(session, token_id)
    await session.commit()


@admin_router.post("/{device_id}/tokens/revoke-all", response_model=dict)
async def revoke_all_device_tokens_route(
    device_id: uuid.UUID,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """吊销设备全部有效凭证（换机/丢失场景一键失效）。"""
    try:
        await get_device_or_404(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    count = await revoke_all_device_tokens(session, device_id, actor)
    await session.commit()
    return {"revoked_count": count}


@router.get("/me", response_model=DeviceSelfResponse)
async def device_me(
    device_id: uuid.UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> DeviceSelfResponse:
    """设备自查：单人间锁定在住长者，双人间返回候选列表。"""
    try:
        view = await device_self_view(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DeviceSelfResponse(**view)
