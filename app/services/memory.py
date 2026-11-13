"""长期记忆服务（Day7 段5）：萃取 → 存储 → 召回过滤 → 冲突检测 → 遗忘归并。

铁律：
- 所有记忆读写都强制 ``WHERE MemoryChunk.elder_id == elder_id``，禁止先全量查再过滤；
- 不同 embedding 模型/维度不混算，召回与维护都按 ``embedding_model == provider.model`` 过滤；
- 日志只记 elder_id、条数、耗时、category 分布与降级标记，**不记记忆正文/对话原文**；
- LLM 未配置/失败全部可见降级：不写库、不推进游标、不中断主链路。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.integrations.embeddings import EmbeddingProvider, get_embedding_provider
from app.integrations.llm import LlmUnavailable, complete_chat
from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    Elder,
    FamilyElderLink,
    Room,
)
from app.models.enums import (
    AccountRole,
    AssignmentStatus,
    MemoryCategory,
    MemoryStatus,
    MemoryVisibility,
)
from app.models.memory import MemoryChunk

logger = get_logger(__name__)

MEMORY_TTL_DAYS = 90
DEDUP_SIMILARITY = 0.95
CONFLICT_SIMILARITY = 0.90
MERGE_SIMILARITY = 0.92
RECALL_TOP_K = 5
RECALL_MAX_KEEP = 3
RECALL_DEGRADED_KEEP = 2
CONTEXT_MAX_ITEMS = 3
CONTEXT_MAX_CHARS = 40
MAX_ITEM_CHARS = 200
COLD_DAYS = 90
COLD_WEIGHT_THRESHOLD = 0.5
TRANSCRIPT_MAX_CHARS = 4000

CATEGORY_WEIGHTS: dict[str, float] = {
    MemoryCategory.HEALTH: 1.0,
    MemoryCategory.ROUTINE: 0.8,
    MemoryCategory.PREFERENCE: 0.6,
    MemoryCategory.RELATIONSHIP: 0.4,
    MemoryCategory.EVENT: 0.3,
    MemoryCategory.OTHER: 0.2,
}
# 健康/慢病/用药相关提升到照护团队可见（医生/护士），其余默认 private
CARE_TEAM_KEYWORDS = ("药", "病", "高血压", "糖尿病", "过敏", "医院", "医生", "疼", "血压")

_EXTRACT_SYSTEM = (
    "你是养老社区长期记忆整理助手。请从对话中提取关于长者本人的稳定事实，"
    "最多 3 条（不足不硬凑），只记事实、不记情绪评价，不把助手的话当成长者事实。"
    "严格输出 JSON："
    '{"memories":[{"content":"...","category":"preference|health|event|relationship|routine|other"}]}'
    "。没有可记内容时输出 {\"memories\":[]}。"
)

_RERANK_SYSTEM = (
    "你是记忆相关性筛选器。给定长者当前问题和候选记忆，只保留能直接帮助回答问题的记忆。"
    '严格输出 JSON：{"keep":["候选id", ...]}；没有相关记忆时输出 {"keep":[]}。'
)

_CONFLICT_SYSTEM = (
    "你是记忆冲突检测器。判断两条关于同一位长者的记忆是否语义矛盾"
    "（例如「不吃甜食」与「特别想吃月饼」视为矛盾；「喜欢京剧」与「喜欢越剧」不算）。"
    '严格输出 JSON：{"contradict": true} 或 {"contradict": false}。'
)


def _normalize_category(value: Any) -> str:
    text = str(value or "").strip().lower()
    allowed = {item.value for item in MemoryCategory}
    return text if text in allowed else MemoryCategory.OTHER.value


def _visibility_for(content: str, category: str) -> str:
    if category == MemoryCategory.HEALTH:
        return MemoryVisibility.CARE_TEAM
    if any(keyword in content for keyword in CARE_TEAM_KEYWORDS):
        return MemoryVisibility.CARE_TEAM
    return MemoryVisibility.PRIVATE


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_json_object(raw: str) -> dict | None:
    """从 LLM 文本中提取第一个 JSON 对象；容忍 ``` 代码围栏与前后噪声。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _llm_json(system: str, user: str, *, max_tokens: int = 400) -> dict | None:
    """LLM 结构化调用；未配置/失败/解析失败统一返回 None（调用方降级）。"""
    try:
        content = await complete_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
    except LlmUnavailable:
        return None
    return _parse_json_object(content)


# ---- 数据访问：所有语句都强制 elder_id ----


def _active_category_stmt(elder_id: UUID, category: str, model: str):
    return select(MemoryChunk).where(
        MemoryChunk.elder_id == elder_id,
        MemoryChunk.category == category,
        MemoryChunk.status == MemoryStatus.ACTIVE,
        MemoryChunk.embedding_model == model,
    )


def _active_recall_stmt(
    elder_id: UUID, model: str, vector: list[float], limit: int
):
    """按余弦距离排序的召回语句；``elder_id`` 是查询参数之一（隔离可测）。"""
    return (
        select(MemoryChunk)
        .where(
            MemoryChunk.elder_id == elder_id,
            MemoryChunk.status == MemoryStatus.ACTIVE,
            MemoryChunk.embedding_model == model,
        )
        .order_by(MemoryChunk.embedding.cosine_distance(vector))
        .limit(limit)
    )


def _active_for_elder_stmt(elder_id: UUID, model: str):
    return select(MemoryChunk).where(
        MemoryChunk.elder_id == elder_id,
        MemoryChunk.status == MemoryStatus.ACTIVE,
        MemoryChunk.embedding_model == model,
    )


def _require_elder(elder_id: UUID | None) -> UUID:
    if elder_id is None:
        raise ValueError("elder_id 不能为空：记忆读写必须绑定长者")
    return elder_id


# ---- 写入 ----


async def _is_contradiction(old_content: str, new_content: str) -> bool | None:
    """LLM 判定两段记忆是否矛盾；不可用/解析失败返回 None（按不矛盾处理）。"""
    result = await _llm_json(
        _CONFLICT_SYSTEM,
        json.dumps(
            {"old": old_content, "new": new_content}, ensure_ascii=False
        ),
        max_tokens=80,
    )
    if result is None or "contradict" not in result:
        return None
    return bool(result.get("contradict"))


async def remember(
    db: AsyncSession,
    elder_id: UUID,
    items: list[dict[str, Any]],
    *,
    provider: EmbeddingProvider | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """写入结构化记忆：去重 → 冲突检测 → active 落库。

    返回 ``{"written", "duplicate", "superseded", "skipped"}``；不提交事务，
    由调用方在同一事务内提交。
    """
    elder_id = _require_elder(elder_id)
    provider = provider or get_embedding_provider()
    started = time.monotonic()
    stats = {"written": 0, "duplicate": 0, "superseded": 0, "skipped": 0}
    reference = now or datetime.now(UTC)
    expires_at = reference + timedelta(days=MEMORY_TTL_DAYS)

    cleaned: list[dict[str, str]] = []
    for item in items or []:
        content = _truncate(str((item or {}).get("content") or ""), MAX_ITEM_CHARS)
        if not content:
            stats["skipped"] += 1
            continue
        cleaned.append(
            {"content": content, "category": _normalize_category((item or {}).get("category"))}
        )
    if not cleaned:
        return stats

    vectors = await provider.embed([item["content"] for item in cleaned])

    for item, vector in zip(cleaned, vectors, strict=True):
        existing = list(
            (
                await db.execute(
                    _active_category_stmt(elder_id, item["category"], provider.model)
                )
            )
            .scalars()
            .all()
        )
        best: MemoryChunk | None = None
        best_similarity = 0.0
        for row in existing:
            similarity = _cosine(list(row.embedding or []), vector)
            if similarity > best_similarity:
                best, best_similarity = row, similarity

        if best is not None and best_similarity > DEDUP_SIMILARITY:
            stats["duplicate"] += 1
            continue

        if best is not None and best_similarity > CONFLICT_SIMILARITY:
            contradiction = await _is_contradiction(best.content, item["content"])
            if contradiction is None:
                logger.info(
                    "记忆冲突检测降级，按不矛盾处理",
                    extra={
                        "event": "memory_conflict_degraded",
                        "elder_id": str(elder_id),
                    },
                )
            elif contradiction:
                best.status = MemoryStatus.SUPERSEDED
                stats["superseded"] += 1

        db.add(
            MemoryChunk(
                elder_id=elder_id,
                content=item["content"],
                embedding=vector,
                embedding_model=provider.model,
                category=item["category"],
                visibility=_visibility_for(item["content"], item["category"]),
                status=MemoryStatus.ACTIVE,
                expires_at=expires_at,
            )
        )
        stats["written"] += 1

    await db.flush()
    logger.info(
        "记忆写入完成",
        extra={
            "event": "memory_remember",
            "elder_id": str(elder_id),
            "model": provider.model,
            "dimension": provider.dimension,
            "count": stats["written"],
            "duplicate": stats["duplicate"],
            "superseded": stats["superseded"],
            "skipped": stats["skipped"],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return stats


# ---- 召回 ----


async def _rerank(question: str, rows: list[MemoryChunk]) -> list[MemoryChunk] | None:
    """二次推理过滤：只保留 LLM 判定相关的候选；失败返回 None 由调用方退化。"""
    payload = {
        "question": question,
        "candidates": [
            {"id": str(row.chunk_id), "content": row.content} for row in rows
        ],
    }
    result = await _llm_json(
        _RERANK_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=300
    )
    if result is None or not isinstance(result.get("keep"), list):
        return None
    keep_ids = {str(item) for item in result["keep"]}
    return [row for row in rows if str(row.chunk_id) in keep_ids]


async def recall(
    db: AsyncSession,
    elder_id: UUID,
    query: str,
    top_k: int = RECALL_TOP_K,
    *,
    provider: EmbeddingProvider | None = None,
) -> list[MemoryChunk]:
    """召回 + 二次过滤；最多返回 3 条，LLM 失败退化为 Top2。"""
    elder_id = _require_elder(elder_id)
    text = str(query or "").strip()
    if not text:
        return []
    provider = provider or get_embedding_provider()
    vectors = await provider.embed([text])
    if not vectors:
        return []
    vector = vectors[0]

    rows = list(
        (
            await db.execute(
                _active_recall_stmt(elder_id, provider.model, vector, max(1, top_k))
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    kept = await _rerank(text, rows)
    if kept is None:
        kept = rows[:RECALL_DEGRADED_KEEP]
        logger.warning(
            "记忆二次过滤降级，退化为 Top2",
            extra={
                "event": "memory_rerank_degraded",
                "elder_id": str(elder_id),
                "count": len(kept),
            },
        )
    kept = kept[:RECALL_MAX_KEEP]

    if kept:
        # 表里没有 accessed_at：用 updated_at 作「最近访问时间」代理，失败不影响返回
        now = datetime.now(UTC)
        try:
            for row in kept:
                row.updated_at = now
            await db.commit()
        except Exception as exc:  # noqa: BLE001 —— 刷新访问时间失败不阻断召回
            await db.rollback()
            logger.warning(
                "记忆访问时间刷新失败",
                extra={
                    "event": "memory_touch_failed",
                    "elder_id": str(elder_id),
                    "error_type": type(exc).__name__,
                },
            )
    return kept


def build_memory_context(memories: list[MemoryChunk]) -> str | None:
    """拼装注入 system prompt 的记忆上下文：最多 3 条、每条不超过 40 字。"""
    lines: list[str] = []
    for row in memories[:CONTEXT_MAX_ITEMS]:
        content = _truncate(getattr(row, "content", ""), CONTEXT_MAX_CHARS)
        if content:
            lines.append(f"- {content}")
    if not lines:
        return None
    return "【关于长者的记忆】\n" + "\n".join(lines)


async def build_recall_context(
    db: AsyncSession,
    *,
    elder_id: UUID,
    query: str,
    provider: EmbeddingProvider | None = None,
) -> str | None:
    """召回并拼装上下文；供 voice 的 ``_prepare_state`` 直接使用。"""
    memories = await recall(db, elder_id, query, provider=provider)
    return build_memory_context(memories)


# ---- 萃取 ----


def _transcript(messages: list[Any]) -> str:
    lines: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            role, content = message.get("role"), message.get("content")
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", None)
        if not content:
            continue
        speaker = "长者" if role == "user" else "康伴"
        lines.append(f"{speaker}：{content}")
    return "\n".join(lines)[:TRANSCRIPT_MAX_CHARS]


async def extract_memories(messages: list[Any]) -> list[dict[str, str]] | None:
    """从对话中萃取最多 3 条结构化记忆；LLM 失败返回 None（不写库、不推进游标）。"""
    transcript = _transcript(messages)
    if not transcript.strip():
        return []
    result = await _llm_json(
        _EXTRACT_SYSTEM, f"【对话】\n{transcript}", max_tokens=600
    )
    if result is None or not isinstance(result.get("memories"), list):
        logger.warning(
            "记忆萃取降级：LLM 不可用或输出不可解析",
            extra={"event": "memory_extract_degraded"},
        )
        return None

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in result["memories"][:CONTEXT_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        content = _truncate(str(raw.get("content") or ""), MAX_ITEM_CHARS)
        if not content or content in seen:
            continue
        seen.add(content)
        items.append(
            {"content": content, "category": _normalize_category(raw.get("category"))}
        )
    return items


async def extract_and_remember(
    db: AsyncSession,
    *,
    elder_id: UUID,
    messages: list[Any],
    thread_id: UUID | None = None,
    provider: EmbeddingProvider | None = None,
) -> dict[str, int] | None:
    """萃取 + 写入；返回 None 表示萃取降级（调用方不得推进游标）。"""
    items = await extract_memories(messages)
    if items is None:
        return None
    if not items:
        stats = {"written": 0, "duplicate": 0, "superseded": 0, "skipped": 0}
    else:
        stats = await remember(db, elder_id, items, provider=provider)
    distribution = Counter(item["category"] for item in items)
    logger.info(
        "记忆萃取完成",
        extra={
            "event": "memory_extracted",
            "elder_id": str(elder_id),
            "thread_id": str(thread_id) if thread_id else None,
            "count": len(items),
            "categories": dict(distribution),
            "count_new": stats["written"],
        },
    )
    return stats


# ---- 遗忘与归并 ----


def _mark_suppressed(
    row: MemoryChunk, stats: dict[str, int], marked: set[int], *, dry_run: bool
) -> None:
    key = id(row)
    if key in marked:
        return
    marked.add(key)
    stats["suppressed"] += 1
    if not dry_run:
        row.status = MemoryStatus.SUPPRESSED


async def maintenance(
    db: AsyncSession,
    elder_id: UUID | None = None,
    dry_run: bool = True,
    *,
    provider: EmbeddingProvider | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """过期抑制 → 重复归并 → 冷热分层；``dry_run=True`` 只统计不写库。

    ``elder_id=None`` 时逐个长者处理（每条查询仍强制 elder_id），绝不跨长者比对。
    """
    provider = provider or get_embedding_provider()
    reference = now or datetime.now(UTC)
    stats: dict[str, Any] = {"scanned": 0, "suppressed": 0, "merged": 0, "dry_run": dry_run}

    if elder_id is not None:
        elder_ids: list[UUID] = [elder_id]
    else:
        elder_ids = list(
            (
                await db.execute(
                    select(MemoryChunk.elder_id)
                    .where(MemoryChunk.status == MemoryStatus.ACTIVE)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

    marked: set[int] = set()
    for current_elder in elder_ids:
        rows = list(
            (
                await db.execute(_active_for_elder_stmt(current_elder, provider.model))
            )
            .scalars()
            .all()
        )
        stats["scanned"] += len(rows)

        # 1) 过期 → suppressed（不物理删除，便于审计）
        for row in rows:
            expires_at = _aware(row.expires_at)
            if expires_at is not None and expires_at < reference:
                _mark_suppressed(row, stats, marked, dry_run=dry_run)

        active_rows = [
            row for row in rows if dry_run or row.status == MemoryStatus.ACTIVE
        ]

        # 2) 同 elder + category 内相似度 >0.92：保留最新、旧记录 superseded
        by_category: dict[str, list[MemoryChunk]] = {}
        for row in active_rows:
            by_category.setdefault(row.category, []).append(row)
        for group in by_category.values():
            ordered = sorted(
                group,
                key=lambda item: _aware(item.updated_at)
                or _aware(item.created_at)
                or reference,
                reverse=True,
            )
            representatives: list[MemoryChunk] = []
            for row in ordered:
                if id(row) in marked:
                    continue  # dry-run 下已判定抑制/合并的行不参与代表集
                duplicate = any(
                    _cosine(list(row.embedding or []), list(rep.embedding or []))
                    > MERGE_SIMILARITY
                    for rep in representatives
                )
                if duplicate:
                    marked.add(id(row))
                    stats["merged"] += 1
                    if not dry_run:
                        row.status = MemoryStatus.SUPERSEDED
                    continue
                representatives.append(row)

        # 3) 90 天未被访问且低权重 → suppressed
        cold_cutoff = reference - timedelta(days=COLD_DAYS)
        for row in active_rows:
            if id(row) in marked:
                continue
            last_seen = _aware(row.updated_at) or _aware(row.created_at)
            weight = CATEGORY_WEIGHTS.get(row.category, CATEGORY_WEIGHTS[MemoryCategory.OTHER])
            if last_seen is not None and last_seen < cold_cutoff and weight < COLD_WEIGHT_THRESHOLD:
                _mark_suppressed(row, stats, marked, dry_run=dry_run)

    if not dry_run:
        await db.flush()
    return stats


async def weekly_maintenance(*, dry_run: bool = False) -> dict[str, Any]:
    """调度器周任务入口：全量维护并提交（dry_run 时回滚）。"""
    factory = get_session_factory()
    async with factory() as db:
        stats = await maintenance(db, elder_id=None, dry_run=dry_run)
        if dry_run:
            await db.rollback()
        else:
            await db.commit()
    logger.info(
        "每周记忆维护完成",
        extra={"event": "memory_maintenance_weekly", **stats},
    )
    return stats


# ---- 只读查询（对话历史可视化 段 3）----


class MemoryElderNotFound(Exception):
    """长者不存在（上层映射 404，不暴露存在性）。"""


class MemoryAccessDenied(Exception):
    """当前账号无权查看该长者的记忆（上层映射 403）。"""


async def _assert_elder_visible(
    db: AsyncSession, *, account_id: UUID, role: str, elder_id: UUID
) -> None:
    """记忆读权限校验：admin 全量；doctor 限绑定；nurse 限楼栋；family 限有效绑定。

    家属未绑定按长者不存在处理（404），不暴露存在性；医生/护士越权映射 403。
    """
    elder = await db.get(Elder, elder_id)
    if elder is None:
        raise MemoryElderNotFound("长者不存在")

    if role == AccountRole.ADMIN:
        return

    if role == AccountRole.DOCTOR:
        stmt = select(DoctorElderAssignment.assignment_id).where(
            DoctorElderAssignment.account_id == account_id,
            DoctorElderAssignment.elder_id == elder_id,
            DoctorElderAssignment.status == AssignmentStatus.ACTIVE,
            or_(
                DoctorElderAssignment.valid_until.is_(None),
                DoctorElderAssignment.valid_until > func.now(),
            ),
        )
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            raise MemoryAccessDenied("elder_not_visible")
        return

    if role == AccountRole.NURSE:
        buildings = select(BuildingAssignment.building_code).where(
            BuildingAssignment.account_id == account_id,
            BuildingAssignment.status == AssignmentStatus.ACTIVE,
        )
        stmt = (
            select(Elder.elder_id)
            .join(Room, Room.room_id == Elder.room_id)
            .where(Elder.elder_id == elder_id, Room.building_code.in_(buildings))
        )
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            raise MemoryAccessDenied("elder_not_visible")
        return

    if role == AccountRole.FAMILY:
        stmt = select(FamilyElderLink.assignment_id).where(
            FamilyElderLink.account_id == account_id,
            FamilyElderLink.elder_id == elder_id,
            FamilyElderLink.status == AssignmentStatus.ACTIVE,
            or_(
                FamilyElderLink.valid_until.is_(None),
                FamilyElderLink.valid_until > func.now(),
            ),
        )
        if (await db.execute(stmt)).scalar_one_or_none() is None:
            raise MemoryElderNotFound("长者不存在")
        return

    raise MemoryAccessDenied("elder_not_visible")


async def list_memory_chunks(
    db: AsyncSession,
    *,
    account_id: UUID,
    role: str,
    elder_id: UUID,
    visibility: str | None = None,
    category: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """只读分页查询长者长期记忆（不含 embedding 向量）。

    默认只返回 status=active；家属可读绑定长者的全部 active 记忆。
    返回 ``{"items", "total"}``，items 按创建时间倒序；
    日志只记元数据，不记记忆正文。
    """
    elder_id = _require_elder(elder_id)
    await _assert_elder_visible(db, account_id=account_id, role=role, elder_id=elder_id)

    conditions = [
        MemoryChunk.elder_id == elder_id,
        MemoryChunk.status == MemoryStatus.ACTIVE,
    ]
    if visibility is not None:
        conditions.append(MemoryChunk.visibility == visibility)
    if category is not None:
        conditions.append(MemoryChunk.category == category)

    total = (
        await db.execute(select(func.count()).select_from(MemoryChunk).where(*conditions))
    ).scalar_one()
    rows = (
        await db.execute(
            select(
                MemoryChunk.chunk_id,
                MemoryChunk.content,
                MemoryChunk.category,
                MemoryChunk.visibility,
                MemoryChunk.created_at,
            )
            .where(*conditions)
            .order_by(MemoryChunk.created_at.desc(), MemoryChunk.chunk_id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    logger.info(
        "记忆列表查询",
        extra={
            "event": "memory_list",
            "elder_id": str(elder_id),
            "role": role,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )
    return {
        "items": [
            {
                "chunk_id": str(row.chunk_id),
                "content": row.content,
                "category": row.category,
                "visibility": row.visibility,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "total": total,
    }


__all__ = [
    "MemoryAccessDenied",
    "MemoryElderNotFound",
    "build_memory_context",
    "build_recall_context",
    "extract_and_remember",
    "extract_memories",
    "list_memory_chunks",
    "maintenance",
    "recall",
    "remember",
    "weekly_maintenance",
]
