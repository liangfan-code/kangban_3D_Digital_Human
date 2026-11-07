"""长者档案路由：管理员创建/更新，按身份范围查询。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, get_current_user, require_admin
from app.db.session import get_session
from app.schemas.elder import ElderCreate, ElderResponse, ElderUpdate
from app.services.elder import (
    ElderConflict,
    ElderNotFound,
    ElderPermissionDenied,
    ElderValidationError,
    create_elder,
    deactivate_elder,
    get_elder_for_user,
    list_elders_for_user,
    update_elder,
)

router = APIRouter(prefix="/elders", tags=["elders"])


@router.get("", response_model=list[ElderResponse])
async def list_elders_route(
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ElderResponse]:
    """长者列表（同接口按角色收敛）：admin 全量；nurse 按楼栋；doctor/family 按有效关联。

    设备 Token 调此接口因人/设备互斥返回 403。
    """
    elders = await list_elders_for_user(session, user)
    return [ElderResponse.model_validate(e) for e in elders]


@router.post("", response_model=ElderResponse, status_code=status.HTTP_201_CREATED)
async def create_elder_route(
    body: ElderCreate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ElderResponse:
    """创建长者档案（仅管理员）：床位成对、容量跨表校验、占用冲突 409。"""
    try:
        elder = await create_elder(session, body, actor)
    except ElderValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ElderConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return ElderResponse.model_validate(elder)


@router.get("/{elder_id}", response_model=ElderResponse)
async def get_elder_route(
    elder_id: UUID,
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ElderResponse:
    """按身份范围查询长者：范围外 404（隐藏存在性），无权限角色 403。"""
    try:
        elder = await get_elder_for_user(session, user, elder_id)
    except ElderPermissionDenied as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ElderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ElderResponse.model_validate(elder)


@router.put("/{elder_id}", response_model=ElderResponse)
async def update_elder_route(
    elder_id: UUID,
    body: ElderUpdate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ElderResponse:
    """更新长者档案（仅管理员）。"""
    elder = await get_elder_for_user(session, actor, elder_id)  # 管理员恒过；不存在 404
    try:
        elder = await update_elder(session, elder, body, actor)
    except ElderValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ElderConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return ElderResponse.model_validate(elder)


@router.delete("/{elder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_elder_route(
    elder_id: UUID,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """软删长者（仅管理员）：置 discharged 并清空床位释放占用；幂等 204。"""
    try:
        elder = await get_elder_for_user(session, actor, elder_id)
    except ElderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await deactivate_elder(session, elder, actor)
    await session.commit()
