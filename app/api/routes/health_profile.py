"""健康档案路由（Day2 v2）：档案读写、ACL 授权、摘要、AI 分析、导出、待录入清单。

错误语义沿用阶段一：范围外 404（隐藏存在性）、角色/ACL 无权限 403。
AI 红线：仅 basic 级出域；未配置 LLM 503；频控 429；失败不留档 502。
"""

import html
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, get_current_user, require_admin, require_role
from app.db.session import get_session
from app.models import (
    ElderHealthRecord,
    HealthAiAnalysis,
    HealthAuditLog,
    HealthGrant,
    HealthSummary,
)
from app.models.enums import AssignmentStatus
from app.schemas.health_profile import (
    AiAnalysisResponse,
    ConfigFlagsResponse,
    HealthAuditLogResponse,
    HealthGrantCreate,
    HealthGrantResponse,
    HealthGrantUpdate,
    HealthProfileResponse,
    HealthRecordCreate,
    HealthRecordResponse,
    HealthRecordUpdate,
    HealthSummaryResponse,
    PendingMeasureItem,
)
from app.services import ai_analysis as ai_service
from app.services import health_profile as hp_service
from app.services.ai_analysis import (
    AnalysisRateLimited,
    LlmCallFailed,
    LlmNotConfigured,
)
from app.services.elder import (
    ElderNotFound,
    ElderPermissionDenied,
    get_elder_for_user,
)
from app.services.health_profile import (
    GrantValidationError,
    HealthAccessDenied,
)

router = APIRouter(tags=["health-profile"])


async def _elder_or_http(session: AsyncSession, user: UserIdentity, elder_id: UUID):
    """范围判定：范围外 404、无权限角色 403。"""
    try:
        return await get_elder_for_user(session, user, elder_id)
    except ElderPermissionDenied as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ElderNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _health_denied(exc: HealthAccessDenied) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


# ---- 健康档案读写 ----


@router.get("/elders/{elder_id}/health", response_model=HealthProfileResponse)
async def get_health_profile_route(
    elder_id: UUID,
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HealthProfileResponse:
    """按 ACL + data_level 过滤后的分组档案；无任何授权 403。"""
    elder = await _elder_or_http(session, user, elder_id)
    try:
        level, records, context = await hp_service.list_profile(session, user, elder)
    except HealthAccessDenied as exc:
        raise _health_denied(exc) from exc
    groups: dict[str, list[HealthRecordResponse]] = {}
    for r in records:
        groups.setdefault(r.category, []).append(HealthRecordResponse.model_validate(r))
    return HealthProfileResponse(
        elder_id=elder.elder_id,
        viewer_level=level,
        groups=groups,
        elder_name=context["elder_name"],
        room_label=context["room_label"],
        building_code=context["building_code"],
        device_codes=context["device_codes"],
    )


@router.post(
    "/elders/{elder_id}/health",
    response_model=HealthRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_health_record_route(
    elder_id: UUID,
    body: HealthRecordCreate,
    user: UserIdentity = Depends(require_role("nurse", "doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> HealthRecordResponse:
    """写入健康记录（依 can_edit_*）；recorded_role 服务端注入；同步写审计流水。"""
    elder = await _elder_or_http(session, user, elder_id)
    try:
        record = await hp_service.create_record(session, user, elder, body)
    except HealthAccessDenied as exc:
        raise _health_denied(exc) from exc
    await session.commit()
    return HealthRecordResponse.model_validate(record)


@router.put("/health/{record_id}", response_model=HealthRecordResponse)
async def update_health_record_route(
    record_id: UUID,
    body: HealthRecordUpdate,
    user: UserIdentity = Depends(require_role("nurse", "doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> HealthRecordResponse:
    """修改健康记录（依 can_edit_*）；旧值→新值写入审计流水。"""
    try:
        record = await hp_service.get_record_or_404(session, record_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await _elder_or_http(session, user, record.elder_id)  # 范围判定（404/403）
    try:
        record = await hp_service.update_record(session, user, record, body)
    except HealthAccessDenied as exc:
        raise _health_denied(exc) from exc
    await session.commit()
    await session.refresh(record)
    return HealthRecordResponse.model_validate(record)


# ---- 健康摘要（医生留档 + 家属已阅） ----


@router.get("/elders/{elder_id}/health/summaries", response_model=list[HealthSummaryResponse])
async def list_summaries_route(
    elder_id: UUID,
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[HealthSummaryResponse]:
    """摘要留档列表：需 confidential 级查看授权（摘要含重大疾病分区）。"""
    elder = await _elder_or_http(session, user, elder_id)
    level = await hp_service.viewer_level(session, user, elder.elder_id)
    if level != "confidential":
        raise _health_denied(HealthAccessDenied("无查看健康摘要权限"))
    rows = (
        await session.execute(
            select(HealthSummary)
            .where(HealthSummary.elder_id == elder.elder_id)
            .order_by(HealthSummary.version.desc())
        )
    ).scalars().all()
    return [HealthSummaryResponse.model_validate(s) for s in rows]


@router.post(
    "/elders/{elder_id}/health/summaries",
    response_model=HealthSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_summary_route(
    elder_id: UUID,
    user: UserIdentity = Depends(require_role("doctor", "admin")),
    session: AsyncSession = Depends(get_session),
) -> HealthSummaryResponse:
    """医生「生成健康摘要」：规则式聚合快照，版本自增留档。"""
    elder = await _elder_or_http(session, user, elder_id)
    try:
        summary = await hp_service.create_summary(session, user, elder)
    except HealthAccessDenied as exc:
        raise _health_denied(exc) from exc
    await session.commit()
    return HealthSummaryResponse.model_validate(summary)


@router.post(
    "/elders/{elder_id}/health/summaries/{summary_id}/ack",
    response_model=HealthSummaryResponse,
)
async def acknowledge_summary_route(
    elder_id: UUID,
    summary_id: UUID,
    user: UserIdentity = Depends(require_role("family")),
    session: AsyncSession = Depends(get_session),
) -> HealthSummaryResponse:
    """家属「已阅」回执（幂等，只记首个回执）。"""
    elder = await _elder_or_http(session, user, elder_id)
    level = await hp_service.viewer_level(session, user, elder.elder_id)
    if level != "confidential":
        raise _health_denied(HealthAccessDenied("无该长者健康摘要回执权限"))
    summary = (
        await session.execute(
            select(HealthSummary).where(
                HealthSummary.summary_id == summary_id,
                HealthSummary.elder_id == elder.elder_id,
            )
        )
    ).scalar_one_or_none()
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="摘要不存在")
    summary = await hp_service.acknowledge_summary(session, user, summary)
    await session.commit()
    return HealthSummaryResponse.model_validate(summary)


# ---- AI 一键健康分析（家属） ----


async def _ai_access(session: AsyncSession, user: UserIdentity, elder_id: UUID):
    if user.role not in ("family", "admin"):
        raise _health_denied(HealthAccessDenied("AI 健康分析仅限家属使用"))
    elder = await _elder_or_http(session, user, elder_id)
    level = await hp_service.viewer_level(session, user, elder.elder_id)
    if level is None:
        raise _health_denied(HealthAccessDenied("无健康档案访问授权"))
    return elder


@router.post(
    "/elders/{elder_id}/health/ai-analysis",
    response_model=AiAnalysisResponse,
    status_code=status.HTTP_201_CREATED,
)
async def run_ai_analysis_route(
    elder_id: UUID,
    user: UserIdentity = Depends(require_role("family", "admin")),
    session: AsyncSession = Depends(get_session),
) -> AiAnalysisResponse:
    """AI 分析：仅 basic 级出域；未配置 503、频控 429、调用失败 502（不留档）。"""
    elder = await _ai_access(session, user, elder_id)
    records = (
        await session.execute(
            select(ElderHealthRecord).where(ElderHealthRecord.elder_id == elder.elder_id)
        )
    ).scalars().all()
    try:
        row = await ai_service.run_analysis(session, elder, list(records), user.account_id)
    except LlmNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except AnalysisRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except LlmCallFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    await session.commit()
    return AiAnalysisResponse.model_validate(row)


@router.get(
    "/elders/{elder_id}/health/ai-analyses", response_model=list[AiAnalysisResponse]
)
async def list_ai_analyses_route(
    elder_id: UUID,
    user: UserIdentity = Depends(require_role("family", "admin")),
    session: AsyncSession = Depends(get_session),
) -> list[AiAnalysisResponse]:
    """历史 AI 分析回看（家属需 basic 级查看授权）。"""
    elder = await _ai_access(session, user, elder_id)
    rows = (
        await session.execute(
            select(HealthAiAnalysis)
            .where(HealthAiAnalysis.elder_id == elder.elder_id)
            .order_by(HealthAiAnalysis.created_at.desc())
        )
    ).scalars().all()
    return [AiAnalysisResponse.model_validate(r) for r in rows]


# ---- 导出（打印版 HTML，增强项 D） ----


@router.get("/elders/{elder_id}/health/export", response_class=HTMLResponse)
async def export_health_route(
    elder_id: UUID,
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """打印版 HTML（浏览器另存 PDF）；仅含授权级别内数据。"""
    elder = await _elder_or_http(session, user, elder_id)
    try:
        level, records, context = await hp_service.list_profile(session, user, elder)
    except HealthAccessDenied as exc:
        raise _health_denied(exc) from exc

    titles = {
        "vital": "体征",
        "body_part": "身体部位",
        "medication": "当前用药",
        "chronic": "慢性病",
        "major": "重大疾病",
    }
    esc = html.escape
    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>{esc(elder.name)} 健康档案</title>",
        "<style>body{font-family:system-ui;max-width:800px;margin:24px auto;color:#222}",
        "h1{font-size:22px}h2{font-size:16px;border-bottom:1px solid #ccc;padding-bottom:4px}",
        "li{margin:4px 0}.meta{color:#666;font-size:13px}",
        "@media print{.no-print{display:none}}</style></head><body>",
        f"<h1>{esc(elder.name)} 健康档案</h1>",
        f"<p class='meta'>房间：{esc(context['room_label'] or '未分配')}｜"
        f"在用设备：{esc('、'.join(context['device_codes']) or '无')}</p>",
    ]
    by_cat: dict[str, list] = {}
    for r in records:
        by_cat.setdefault(r.category, []).append(r)
    for cat, title in titles.items():
        items = by_cat.get(cat, [])
        if not items:
            continue
        parts.append(f"<h2>{title}</h2><ul>")
        for r in items:
            mark = {"warning": "【预警】", "attention": "【需关注】"}.get(r.severity, "")
            parts.append(
                f"<li>{mark}{esc(r.item_key)}：{esc(r.value_text)}</li>"
            )
        parts.append("</ul>")
    parts.append(
        "<p class='no-print'>提示：使用浏览器「打印 → 另存为 PDF」导出。</p>"
        "</body></html>"
    )
    return HTMLResponse("".join(parts))


# ---- 护工：今日待录入清单 ----

nurse_router = APIRouter(prefix="/nurse", tags=["health-profile"])


@nurse_router.get("/tasks/pending-measures", response_model=list[PendingMeasureItem])
async def pending_measures_route(
    user: UserIdentity = Depends(require_role("nurse")),
    session: AsyncSession = Depends(get_session),
) -> list[PendingMeasureItem]:
    """今日待录入清单（所辖楼栋）；另有 warning 长者集合经 /nurse/health-overview 提供。"""
    try:
        result = await hp_service.pending_measures(session, user)
    except HealthAccessDenied as exc:
        raise _health_denied(exc) from exc
    return [PendingMeasureItem(**item) for item in result["items"]]


@nurse_router.get("/health-overview")
async def nurse_health_overview_route(
    user: UserIdentity = Depends(require_role("nurse")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """护工首页概览：体征预警长者集合（黄点）。"""
    result = await hp_service.pending_measures(session, user)
    return {"warning_elder_ids": result["warning_elder_ids"]}


# ---- 管理员：授权矩阵 + 审计流水 ----

admin_router = APIRouter(prefix="/admin", tags=["health-profile"])


@admin_router.get("/health-grants", response_model=list[HealthGrantResponse])
async def list_grants_route(
    _admin=Depends(require_admin),
    account_id: UUID | None = None,
    elder_id: UUID | None = None,
    grant_status: AssignmentStatus | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[HealthGrantResponse]:
    stmt = select(HealthGrant)
    if account_id is not None:
        stmt = stmt.where(HealthGrant.account_id == account_id)
    if elder_id is not None:
        stmt = stmt.where(HealthGrant.elder_id == elder_id)
    if grant_status is not None:
        stmt = stmt.where(HealthGrant.status == grant_status)
    rows = (
        await session.execute(stmt.order_by(HealthGrant.created_at))
    ).scalars().all()
    return [HealthGrantResponse.model_validate(g) for g in rows]


@admin_router.post(
    "/health-grants", response_model=HealthGrantResponse, status_code=status.HTTP_201_CREATED
)
async def upsert_grant_route(
    body: HealthGrantCreate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HealthGrantResponse:
    """创建或覆盖授权（同账号同长者 upsert，吊销态复活）。"""
    try:
        grant = await hp_service.upsert_grant(session, actor, body)
    except GrantValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(grant)
    return HealthGrantResponse.model_validate(grant)


@admin_router.put("/health-grants/{grant_id}", response_model=HealthGrantResponse)
async def update_grant_route(
    grant_id: UUID,
    body: HealthGrantUpdate,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HealthGrantResponse:
    grant = (
        await session.execute(select(HealthGrant).where(HealthGrant.grant_id == grant_id))
    ).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="授权不存在")
    try:
        grant = await hp_service.update_grant(session, actor, grant, body)
    except GrantValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(grant)
    return HealthGrantResponse.model_validate(grant)


@admin_router.delete("/health-grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_grant_route(
    grant_id: UUID,
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """软吊销授权（幂等 204）。"""
    found = await hp_service.suspend_grant(session, grant_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="授权不存在")
    await session.commit()


@admin_router.get("/health-audit", response_model=list[HealthAuditLogResponse])
async def list_health_audit_route(
    _admin=Depends(require_admin),
    elder_id: UUID | None = None,
    operator_id: UUID | None = None,
    limit: int = Query(default=100, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[HealthAuditLogResponse]:
    """健康数据审计流水（按长者/操作人过滤）。"""
    stmt = select(HealthAuditLog).order_by(HealthAuditLog.created_at.desc()).limit(limit)
    if elder_id is not None:
        stmt = stmt.where(HealthAuditLog.elder_id == elder_id)
    if operator_id is not None:
        stmt = stmt.where(HealthAuditLog.operator_id == operator_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [HealthAuditLogResponse.model_validate(r) for r in rows]


# ---- 前端功能开关 ----

config_router = APIRouter(prefix="/config", tags=["health-profile"])


@config_router.get("/flags", response_model=ConfigFlagsResponse)
async def config_flags_route() -> ConfigFlagsResponse:
    """公开开关：AI 服务是否已配置（前端据此置灰按钮）。"""
    return ConfigFlagsResponse(ai_analysis_enabled=ai_service.is_configured())
