"""会话数据治理（对话历史可视化 段 4）：保留期 → 归档 → 物理删除。

规则：
- ACTIVE 会话永不清理；只处理 ENDED / ABANDONED；
- 到期判定：``expires_at <= now``（手动标记，永久生效）或
  ``retention_days > 0 且 started_at <= now - retention_days``；
- 清理三步（归档 → 逻辑下架 → 物理删除）在同一事务内原子完成：
  归档表先写入完整拷贝（含消息正文 JSONB），再删消息行、删会话行
  （删除即下架——归档拷贝已在同事务落定，无中间可见态）；
- 审计只记元数据；dry-run 只出报表零副作用；
- ``conversation_retention_days = 0`` 时仅清理 ``expires_at`` 已到期的会话。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models import Conversation, ConversationArchive, ConversationMessage
from app.services.audit import record_audit

logger = get_logger(__name__)


def _expired_conditions(now: datetime, retention_days: int) -> list[Any]:
    conditions = [Conversation.expires_at <= now]
    if retention_days > 0:
        cutoff = now - timedelta(days=retention_days)
        conditions.append(
            (Conversation.expires_at.is_(None)) & (Conversation.started_at <= cutoff)
        )
    return [
        or_(*conditions),
        Conversation.status != "active",
    ]


async def list_expired_conversations(
    db: AsyncSession, *, now: datetime, retention_days: int
) -> list[Conversation]:
    """到期待清理会话（ENDED/ABANDONED；ACTIVE 永不在列）。"""
    stmt = select(Conversation).where(*_expired_conditions(now, retention_days))
    return list((await db.execute(stmt)).scalars().all())


async def run_maintenance(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
    retention_days: int | None = None,
    actor_account_id: UUID | None = None,
    actor_role: str | None = None,
) -> dict[str, Any]:
    """治理主入口：出报表（dry-run）或执行归档清理；不提交事务，由调用方提交。"""
    now = now or datetime.now(UTC)
    if retention_days is None:
        retention_days = get_settings().conversation_retention_days

    candidates = await list_expired_conversations(db, now=now, retention_days=retention_days)
    counts: dict[UUID, int] = {}
    if candidates:
        rows = await db.execute(
            select(ConversationMessage.thread_id, ConversationMessage.seq).where(
                ConversationMessage.thread_id.in_([c.thread_id for c in candidates])
            )
        )
        for thread_id, _seq in rows.all():
            counts[thread_id] = counts.get(thread_id, 0) + 1
    items = [
        {
            "thread_id": str(conv.thread_id),
            "elder_id": str(conv.elder_id) if conv.elder_id else None,
            "session_seq": conv.session_seq,
            "status": conv.status,
            "started_at": conv.started_at,
            "ended_at": conv.ended_at,
            "expires_at": conv.expires_at,
            "message_count": counts.get(conv.thread_id, 0),
        }
        for conv in candidates
    ]

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "retention_days": retention_days,
        "matched": len(items),
        "archived": 0,
        "items": items,
    }

    await record_audit(
        db,
        action="conversation_maintenance",
        target_type="conversation",
        target_id=None,
        actor_account_id=actor_account_id,
        actor_role=actor_role or "scheduler",
        detail={"dry_run": dry_run, "matched": len(items), "retention_days": retention_days},
    )

    if dry_run or not candidates:
        return report

    for conv in candidates:
        messages = (
            (
                await db.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.thread_id == conv.thread_id)
                    .order_by(ConversationMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        db.add(
            ConversationArchive(
                thread_id=conv.thread_id,
                elder_id=conv.elder_id,
                device_id=conv.device_id,
                session_seq=conv.session_seq,
                identity_source=conv.identity_source,
                started_at=conv.started_at,
                ended_at=conv.ended_at,
                message_count=len(messages),
                messages=[
                    {
                        "seq": m.seq,
                        "role": m.role,
                        "content": m.content,
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                    }
                    for m in messages
                ],
            )
        )
        await db.execute(
            delete(ConversationMessage).where(ConversationMessage.thread_id == conv.thread_id)
        )
        await db.delete(conv)
        report["archived"] += 1

    await db.flush()  # 归档插入与删除立即生效（调用方负责提交）

    logger.info(
        "会话治理完成",
        extra={
            "event": "conversation_maintenance",
            "matched": report["matched"],
            "archived": report["archived"],
            "dry_run": dry_run,
            "retention_days": retention_days,
        },
    )
    return report


async def run_scheduled_maintenance() -> dict[str, Any] | None:
    """APScheduler 日任务入口：独立会话 + 提交；异常不外泄。"""
    factory = get_session_factory()
    async with factory() as db:
        report = await run_maintenance(db, dry_run=False, actor_role="scheduler")
        await db.commit()
    return report


__all__ = [
    "list_expired_conversations",
    "run_maintenance",
    "run_scheduled_maintenance",
]
