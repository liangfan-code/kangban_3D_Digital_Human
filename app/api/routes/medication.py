"""用药计划与服药任务 HTTP 路由。

- 医生：草稿、修改、确认、停用/恢复/取消；确认后生成长期工单；
- 护工/管理员：只读计划与服药任务核对视图（护工看不到草稿）；
- 家属：只读计划、修改记录与服药历史（字段白名单）。
所有工单事件推送都在 commit 之后执行。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_role
from app.core.logging import get_logger
from app.db.session import get_session
from app.models.enums import AccountRole, MedicationPlanStatus
from app.schemas.medication import (
    FamilyPlanHistoryItem,
    MedicationPlanCreate,
    MedicationPlanDoctorView,
    MedicationPlanFamilyView,
    MedicationPlanNurseView,
    MedicationPlanUpdate,
)
from app.schemas.medication_order import (
    FamilyMedicationHistoryResponse,
    StaffMedicationOrderResponse,
)
from app.services import medication_order as order_service
from app.services import medication_plan as plan_service
from app.services.workorder_events import publish_workorder_event

logger = get_logger(__name__)

router = APIRouter(tags=["medication"])
family_router = APIRouter(prefix="/family", tags=["medication"])

_STATUS_VALUES = {item.value for item in MedicationPlanStatus}
_WRITE_ROLES = (AccountRole.DOCTOR,)


def _raise_http(exc: Exception) -> None:
    if isinstance(
        exc,
        (plan_service.MedicationPlanNotFound, order_service.MedicationOrderNotFound),
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(
        exc,
        (
            plan_service.MedicationPlanForbidden,
            order_service.MedicationOrderForbidden,
        ),
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(
        exc,
        (
            plan_service.MedicationPlanConflict,
            order_service.MedicationOrderConflict,
        ),
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(
        exc,
        (
            plan_service.MedicationPlanValidationError,
            order_service.MedicationOrderValidationError,
        ),
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    raise exc


def _validate_status_filter(value: str | None) -> None:
    if value is not None and value not in _STATUS_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status 取值不合法",
        )


# ---- 员工端：用药计划 ----


@router.post(
    "/elders/{elder_id}/medication-plans",
    response_model=MedicationPlanDoctorView,
    status_code=status.HTTP_201_CREATED,
)
async def create_medication_plan(
    elder_id: uuid.UUID,
    payload: MedicationPlanCreate,
    actor: UserIdentity = Depends(require_role(*_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    try:
        plan = await plan_service.create_plan(
            session, actor=actor, elder_id=elder_id, payload=payload
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - 统一领域异常映射
        await session.rollback()
        _raise_http(exc)
    return plan


@router.get("/elders/{elder_id}/medication-plans", response_model=None)
async def list_medication_plans(
    elder_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    actor: UserIdentity = Depends(
        require_role(AccountRole.DOCTOR, AccountRole.NURSE, AccountRole.ADMIN)
    ),
    session: AsyncSession = Depends(get_session),
):
    _validate_status_filter(status_filter)
    try:
        plans = await plan_service.list_plans(
            session, actor=actor, elder_id=elder_id, status=status_filter
        )
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    if actor.role == AccountRole.NURSE:
        return [MedicationPlanNurseView.model_validate(plan) for plan in plans]
    return [MedicationPlanDoctorView.model_validate(plan) for plan in plans]


@router.get("/medication-plans/{plan_id}", response_model=None)
async def get_medication_plan(
    plan_id: uuid.UUID,
    actor: UserIdentity = Depends(
        require_role(AccountRole.DOCTOR, AccountRole.NURSE, AccountRole.ADMIN)
    ),
    session: AsyncSession = Depends(get_session),
):
    try:
        plan = await plan_service.get_plan(session, actor=actor, plan_id=plan_id)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    if actor.role == AccountRole.NURSE:
        return MedicationPlanNurseView.model_validate(plan)
    return MedicationPlanDoctorView.model_validate(plan)


@router.put("/medication-plans/{plan_id}", response_model=MedicationPlanDoctorView)
async def update_medication_plan(
    plan_id: uuid.UUID,
    payload: MedicationPlanUpdate,
    actor: UserIdentity = Depends(require_role(*_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    try:
        plan, version, changed_fields = await plan_service.update_plan(
            session, actor=actor, plan_id=plan_id, payload=payload
        )
        await session.commit()
        await session.refresh(plan)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        _raise_http(exc)

    if version is not None and plan.workorder_id is not None:
        from app.models import Workorder

        workorder = await session.get(Workorder, plan.workorder_id)
        if workorder is not None:
            await publish_workorder_event(
                "ticket.updated",
                workorder,
                extra={"plan_version": version, "changes": changed_fields},
                session=session,
            )
    return plan


@router.post(
    "/medication-plans/{plan_id}/confirm", response_model=MedicationPlanDoctorView
)
async def confirm_medication_plan(
    plan_id: uuid.UUID,
    actor: UserIdentity = Depends(require_role(*_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    try:
        plan, workorder = await plan_service.confirm_plan(
            session, actor=actor, plan_id=plan_id
        )
        await session.commit()
        await session.refresh(plan)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        _raise_http(exc)
    await publish_workorder_event("ticket.created", workorder, session=session)
    return plan


@router.post(
    "/medication-plans/{plan_id}/pause", response_model=MedicationPlanDoctorView
)
async def pause_medication_plan(
    plan_id: uuid.UUID,
    actor: UserIdentity = Depends(require_role(*_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    try:
        plan = await plan_service.pause_plan(session, actor=actor, plan_id=plan_id)
        await session.commit()
        await session.refresh(plan)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        _raise_http(exc)
    return plan


@router.post(
    "/medication-plans/{plan_id}/resume", response_model=MedicationPlanDoctorView
)
async def resume_medication_plan(
    plan_id: uuid.UUID,
    actor: UserIdentity = Depends(require_role(*_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    try:
        plan = await plan_service.resume_plan(session, actor=actor, plan_id=plan_id)
        await session.commit()
        await session.refresh(plan)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        _raise_http(exc)
    return plan


@router.post(
    "/medication-plans/{plan_id}/cancel", response_model=MedicationPlanDoctorView
)
async def cancel_medication_plan(
    plan_id: uuid.UUID,
    actor: UserIdentity = Depends(require_role(*_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    try:
        plan, workorder = await plan_service.cancel_plan(
            session, actor=actor, plan_id=plan_id
        )
        await session.commit()
        await session.refresh(plan)
        if workorder is not None:
            await session.refresh(workorder)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        _raise_http(exc)
    if workorder is not None:
        await publish_workorder_event("ticket.resolved", workorder, session=session)
    return plan


@router.get(
    "/elders/{elder_id}/medication-orders",
    response_model=StaffMedicationOrderResponse,
)
async def list_staff_medication_orders(
    elder_id: uuid.UUID,
    plan_id: uuid.UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    actor: UserIdentity = Depends(
        require_role(AccountRole.DOCTOR, AccountRole.NURSE, AccountRole.ADMIN)
    ),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await order_service.staff_orders(
            session,
            actor=actor,
            elder_id=elder_id,
            plan_id=plan_id,
            status=status_filter,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


# ---- 家属端：只读计划 / 修改记录 / 服药历史 ----


@family_router.get(
    "/elders/{elder_id}/medication-plans",
    response_model=list[MedicationPlanFamilyView],
)
async def list_family_medication_plans(
    elder_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    actor: UserIdentity = Depends(require_role(AccountRole.FAMILY)),
    session: AsyncSession = Depends(get_session),
):
    _validate_status_filter(status_filter)
    try:
        plans = await plan_service.list_family_plans(
            session, actor=actor, elder_id=elder_id, status=status_filter
        )
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    return [MedicationPlanFamilyView.model_validate(plan) for plan in plans]


@family_router.get(
    "/elders/{elder_id}/medication-plans/{plan_id}/history",
    response_model=list[FamilyPlanHistoryItem],
)
async def get_family_plan_history(
    elder_id: uuid.UUID,
    plan_id: uuid.UUID,
    actor: UserIdentity = Depends(require_role(AccountRole.FAMILY)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await plan_service.family_plan_history(
            session, actor=actor, elder_id=elder_id, plan_id=plan_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)


@family_router.get(
    "/elders/{elder_id}/medication-history",
    response_model=FamilyMedicationHistoryResponse,
)
async def get_family_medication_history(
    elder_id: uuid.UUID,
    plan_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_datetime: datetime | None = Query(default=None, alias="from"),
    to_datetime: datetime | None = Query(default=None, alias="to"),
    actor: UserIdentity = Depends(require_role(AccountRole.FAMILY)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await order_service.family_history(
            session,
            actor=actor,
            elder_id=elder_id,
            plan_id=plan_id,
            page=page,
            page_size=page_size,
            from_datetime=from_datetime,
            to_datetime=to_datetime,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
