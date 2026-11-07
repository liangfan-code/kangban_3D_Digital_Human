"""早期对话摘要（Day4.4）：把窗口外的旧消息滚动压缩成短摘要。

上下文窗口只保留最近 ``HISTORY_MESSAGE_LIMIT``（10 条 = 5 轮）原文；更早的
内容用 LLM 压成不超过 ``SESSION_SUMMARY_MAX_CHARS``（默认 150）字的中文摘要，
连同结构化槽位一起注入 system prompt。

降级口径：LLM 未配置/调用失败时保留旧摘要、不推进 ``summary_upto_seq``，
下一轮自动重试；任何异常都不得中断通话。摘要内容不写日志，只记长度与耗时。
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.llm import LlmUnavailable, complete_chat
from app.models.conversation_message import ConversationMessage
from app.services.conversation_store import HISTORY_MESSAGE_LIMIT, recent_messages
from app.services.session_state import SessionState, SessionStateStore, get_session_store

logger = logging.getLogger(__name__)

_SUMMARY_TEMPLATE = (
    "你是养老社区的对话记录整理助手。请把下面的对话压缩成不超过 {max_chars} 字的中文摘要，"
    "只保留：对长者的称呼、喜好、健康主诉、答应过的事（如吃药提醒）、情绪状态。"
    "不要编造，不要评论，不用列表符号，直接输出摘要正文。"
)


def _slots_payload(state: SessionState) -> dict:
    """只输出非空槽位，避免把一堆 null 塞进 prompt。"""
    slots = state.slots
    payload: dict = {}
    if slots.intent:
        payload["intent"] = slots.intent
    medication = {
        key: value
        for key, value in (
            ("drug", slots.medication.drug),
            ("time", slots.medication.time),
            ("confirmed", slots.medication.confirmed),
        )
        if value is not None
    }
    if medication:
        payload["medication"] = medication
    media = {
        key: value
        for key, value in (
            ("action", slots.media.action),
            ("keyword", slots.media.keyword),
        )
        if value is not None
    }
    if media:
        payload["media"] = media
    identity = {
        key: value
        for key, value in (
            ("candidate", slots.identity.candidate),
            ("confirmed", slots.identity.confirmed),
        )
        if value is not None
    }
    if identity:
        payload["identity"] = identity
    return payload


def build_context_message(state: SessionState) -> str | None:
    """拼装注入 system prompt 的动态上下文；摘要与槽位都为空时返回 None。"""

    parts: list[str] = []
    summary = state.summary.strip()
    if summary:
        parts.append(f"【会话摘要】{summary}")
    payload = _slots_payload(state)
    if payload:
        parts.append(f"【当前状态】{json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(parts) if parts else None


async def refresh_summary(
    db: AsyncSession,
    thread_id: uuid.UUID,
    state: SessionState,
    *,
    store: SessionStateStore | None = None,
) -> int:
    """把窗口外的旧消息压进摘要；返回本次推进后的 ``summary_upto_seq``。

    边界取「最近 10 条中最旧一条」的 seq，而不是 ``max_seq - 10``：seq 是全局
    单调序列（跨 thread 共享），减 10 会错把别的会话消息算进来。
    """

    store = store or get_session_store()
    settings = get_settings()

    recent = await recent_messages(db, thread_id, limit=HISTORY_MESSAGE_LIMIT)
    if len(recent) < HISTORY_MESSAGE_LIMIT:
        return state.summary_upto_seq
    boundary = recent[0].seq
    if boundary <= state.summary_upto_seq:
        return state.summary_upto_seq

    stmt = (
        select(ConversationMessage)
        .where(
            ConversationMessage.thread_id == thread_id,
            ConversationMessage.seq < boundary,
            ConversationMessage.seq > state.summary_upto_seq,
        )
        .order_by(ConversationMessage.seq)
        .limit(settings.session_summary_batch)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return state.summary_upto_seq

    transcript = "\n".join(
        f"{'长者' if row.role == 'user' else '康伴'}：{row.content}" for row in rows
    )
    user_content = f"【新增对话】\n{transcript}"
    if state.summary.strip():
        user_content = f"【已有摘要】{state.summary.strip()}\n{user_content}"
    messages = [
        {
            "role": "system",
            "content": _SUMMARY_TEMPLATE.format(max_chars=settings.session_summary_max_chars),
        },
        {"role": "user", "content": user_content},
    ]

    started = time.monotonic()
    try:
        summary = await complete_chat(
            messages, max_tokens=max(settings.session_summary_max_chars * 2, 200)
        )
    except LlmUnavailable as exc:
        logger.warning(
            "summary refresh skipped",
            extra={"event": "summary_refresh_skipped", "error": str(exc)},
        )
        return state.summary_upto_seq

    summary = summary.strip()
    if not summary:
        return state.summary_upto_seq
    if len(summary) > settings.session_summary_max_chars:
        summary = summary[: settings.session_summary_max_chars]
    new_upto = rows[-1].seq

    # 只合并摘要字段，避免覆盖并发写入的槽位/轮次
    async with store.mutate(str(thread_id)) as latest:
        latest.summary = summary
        latest.summary_upto_seq = new_upto

    logger.info(
        "summary refreshed",
        extra={
            "event": "summary_refreshed",
            "thread_id": str(thread_id),
            "messages": len(rows),
            "summary_chars": len(summary),
            "summary_upto_seq": new_upto,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return new_upto
