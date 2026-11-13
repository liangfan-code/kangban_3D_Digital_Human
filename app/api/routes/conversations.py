"""长者端会话操作与历史读取 API（对话历史可视化 段 2）。

鉴权模型（复用现有机制，不新建鉴权体系）：
- 设备 token：Bearer JWT → get_current_device 注入 device_id；
- 声纹门禁：请求头 X-Voice-Token 必须是本设备的有效声纹确认 token
  （voice_token_store，进程内 300s）， elder 身份来自 token 记录本身，
  不信任请求参数。未确认/无效 → 403 identity_locked。

契约见 docs/plan（§四）：列表/当前/新建/切回/收起/消息分页。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_device
from app.db.session import get_session
from app.models.enums import IdentitySource
from app.services import conversation_store as store
from app.services.audit import record_audit
from app.services.conversation_store import (
    ConversationNotFoundError,
    ConversationStateError,
)
from app.services.voice import voice_token_store

router = APIRouter(tags=["conversations"])


async def _confirmed_elder(request: Request, device_id: UUID) -> UUID:
    """声纹门禁：无效/缺失确认 token 一律 403，长者身份取自 token 记录。"""

    token = request.headers.get("X-Voice-Token", "")
    record = voice_token_store.validate(token, device_id) if token else None
    if record is None:
        raise HTTPException(status_code=403, detail="identity_locked")
    return record.elder_id


def _serialize(conversation, *, message_count: int = 0, is_current: bool = False) -> dict:
    return {
        "thread_id": str(conversation.thread_id),
        "session_seq": conversation.session_seq,
        "status": conversation.status,
        "started_at": conversation.started_at,
        "ended_at": conversation.ended_at,
        "message_count": message_count,
        "is_current": is_current,
    }


async def _get_current_item(
    session: AsyncSession, *, device_id: UUID, elder_id: UUID
) -> dict | None:
    """当前进行中会话（最新 ACTIVE 必然排列表首，取第 1 行即可）。"""

    items, _ = await store.list_conversations(
        session, elder_id=elder_id, device_id=device_id, page=1, page_size=1
    )
    if items and items[0]["is_current"]:
        return items[0]
    return None


@router.get("/conversations")
async def list_conversations(
    request: Request,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    elder_id = await _confirmed_elder(request, device_id)
    items, total = await store.list_conversations(
        session, elder_id=elder_id, device_id=device_id, page=page, page_size=page_size
    )
    return {"items": items, "total": total}


@router.get("/conversations/current")
async def get_current_conversation(
    request: Request,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
):
    elder_id = await _confirmed_elder(request, device_id)
    item = await _get_current_item(session, device_id=device_id, elder_id=elder_id)
    if item is None:
        raise HTTPException(status_code=404, detail="no_active_conversation")
    return item


@router.post("/conversations")
async def start_new_conversation(
    request: Request,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
):
    elder_id = await _confirmed_elder(request, device_id)
    conversation = await store.start_new_conversation(
        session,
        device_id=device_id,
        elder_id=elder_id,
        identity_source=IdentitySource.DEVICE_ROOM,
    )
    await session.commit()
    return _serialize(conversation, message_count=0, is_current=True)


@router.post("/conversations/{thread_id}/activate")
async def activate_conversation(
    thread_id: UUID,
    request: Request,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
):
    elder_id = await _confirmed_elder(request, device_id)
    try:
        conversation = await store.activate_conversation(
            session, thread_id=thread_id, device_id=device_id, elder_id=elder_id
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation_not_found") from exc
    except ConversationStateError as exc:
        raise HTTPException(status_code=422, detail="conversation_closed") from exc
    await session.commit()
    return _serialize(conversation, is_current=True)


@router.post("/conversations/current/end")
async def end_current_conversation(
    request: Request,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
):
    elder_id = await _confirmed_elder(request, device_id)
    item = await _get_current_item(session, device_id=device_id, elder_id=elder_id)
    if item is None:
        raise HTTPException(status_code=404, detail="no_active_conversation")
    try:
        conversation = await store.end_active_conversation(
            session, thread_id=item["thread_id"]
        )
    except ConversationStateError as exc:
        raise HTTPException(status_code=422, detail="conversation_closed") from exc
    await session.commit()
    return _serialize(conversation, message_count=item["message_count"], is_current=False)


@router.get("/conversations/{thread_id}/messages")
async def list_conversation_messages(
    thread_id: UUID,
    request: Request,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
    before_seq: int | None = Query(None, ge=0),
    limit: int = Query(30, ge=1, le=100),
):
    elder_id = await _confirmed_elder(request, device_id)
    conversation = await store.get_elder_conversation(
        session, thread_id=thread_id, elder_id=elder_id
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    messages, has_more = await store.list_messages(
        session, thread_id, before_seq=before_seq, limit=limit
    )
    # 审计：查看会话消息（只记元数据，不记正文）
    await record_audit(
        session,
        action="conversation_messages_read",
        target_type="conversation",
        target_id=str(thread_id),
        actor_role="device",
        detail={"elder_id": str(elder_id), "count": len(messages)},
    )
    await session.commit()
    return {
        "items": [
            {
                "seq": m.seq,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at,
            }
            for m in messages
        ],
        "has_more": has_more,
    }
