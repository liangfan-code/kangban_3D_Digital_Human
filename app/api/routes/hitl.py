"""HITL 待人工确认队列接口（Day6.3，仅 admin）。

Day6 先走进程内存；Day8 建工单表后这里改为持久化实现，接口契约不变。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.dependencies import UserIdentity, require_admin
from app.services.hitl import hitl_queue

router = APIRouter(prefix="/admin/hitl", tags=["hitl"])


class HitlResolveRequest(BaseModel):
    note: str = Field(default="", max_length=500)


@router.get("/pending")
async def list_pending(_: UserIdentity = Depends(require_admin)) -> list[dict]:
    """待人工确认事件（敏感/家属通知），按入队顺序返回。"""
    return [asdict(item) for item in hitl_queue.pending()]


@router.post("/{item_id}/resolve")
async def resolve(
    item_id: str,
    payload: HitlResolveRequest,
    account: UserIdentity = Depends(require_admin),
) -> dict:
    """标记已处理；不存在或已处理返回 404。"""
    item = hitl_queue.resolve(
        item_id, operator=account.display_name, note=payload.note
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待确认事件不存在")
    return asdict(item)
