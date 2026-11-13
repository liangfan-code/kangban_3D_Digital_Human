"""管理员会话治理 API（对话历史可视化 段 4）。

- POST /api/v1/admin/conversations/maintenance {dry_run} — 清理/归档报表（仅管理员）；
- GET  /api/v1/admin/conversations/audit-logs — 审计查询（仅管理员）。
"""


from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, require_admin
from app.db.session import get_session
from app.services import conversation_retention as retention
from app.services.audit import list_audit_logs

router = APIRouter(tags=["conversation-admin"])


class MaintenanceRequest(BaseModel):
    dry_run: bool = True


def _serialize_log(log) -> dict:
    return {
        "audit_id": str(log.audit_id),
        "actor_account_id": str(log.actor_account_id) if log.actor_account_id else None,
        "actor_role": log.actor_role,
        "action": log.action,
        "target_type": log.target_type,
        "target_id": log.target_id,
        "detail": log.detail,
        "created_at": log.created_at,
    }


@router.post("/admin/conversations/maintenance")
async def run_conversation_maintenance(
    body: MaintenanceRequest | None = None,
    actor: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    report = await retention.run_maintenance(
        session,
        dry_run=body.dry_run if body else True,
        actor_account_id=actor.account_id,
        actor_role=actor.role,
    )
    await session.commit()
    return report


@router.get("/admin/conversations/audit-logs")
async def list_conversation_audit_logs(
    _admin: UserIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    target_type: str | None = Query(None, max_length=32),
    target_id: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
):
    logs = await list_audit_logs(
        session, target_type=target_type, target_id=target_id, limit=limit
    )
    return {"items": [_serialize_log(log) for log in logs]}
