"""长期记忆服务测试：萃取解析、去重、冲突、召回过滤、降级、遗忘归并。

统一用确定性 fake embedding + fake LLM，数据库走 kangban_test 的 db_session 事务。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.integrations.llm import LlmUnavailable
from app.models.enums import MemoryStatus
from app.models.memory import MemoryChunk
from app.services import memory


class FakeEmbedding:
    """确定性 embedding：显式映射优先，其余按文本哈希生成 3 维向量。"""

    provider = "fake"
    model = "fake-3"
    dimension = 3
    available = True

    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        self.mapping = mapping or {}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        if text in self.mapping:
            return list(self.mapping[text])
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=6).digest()
        raw = [digest[0] - 127.5, digest[1] - 127.5, digest[2] - 127.5]
        norm = sum(item * item for item in raw) ** 0.5 or 1.0
        return [item / norm for item in raw]


def _fake_llm(monkeypatch, payload: str):
    async def _complete(messages, *, transport=None, max_tokens=None):
        return payload

    monkeypatch.setattr("app.services.memory.complete_chat", _complete)


async def _active_rows(db_session, elder_id):
    rows = (
        await db_session.execute(
            select(MemoryChunk).where(
                MemoryChunk.elder_id == elder_id,
                MemoryChunk.status == MemoryStatus.ACTIVE,
            )
        )
    ).scalars()
    return list(rows.all())


async def test_extract_memories_parses_and_limits(monkeypatch):
    _fake_llm(
        monkeypatch,
        '```json\n{"memories":[{"content":"我有糖尿病","category":"health"},'
        '{"content":"我喜欢京剧","category":"preference"},'
        '{"content":"女儿每周日来看我","category":"relationship"},'
        '{"content":"多余的一条","category":"event"}]}\n```',
    )

    items = await memory.extract_memories([{"role": "user", "content": "我有糖尿病"}])

    assert items is not None
    assert len(items) == 3
    assert items[0] == {"content": "我有糖尿病", "category": "health"}
    assert items[1]["category"] == "preference"
    # 非法 category 归入 other
    assert items[2]["category"] == "relationship"


async def test_extract_memories_degrades_without_llm(monkeypatch):
    async def _boom(*args, **kwargs):
        raise LlmUnavailable("down")

    monkeypatch.setattr("app.services.memory.complete_chat", _boom)

    assert await memory.extract_memories([{"role": "user", "content": "你好"}]) is None


async def test_extract_memories_rejects_bad_json(monkeypatch):
    _fake_llm(monkeypatch, "这不是 JSON")
    assert await memory.extract_memories([{"role": "user", "content": "你好"}]) is None


async def test_remember_dedupes_and_marks_health_care_team(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    provider = FakeEmbedding({"我有糖尿病": [1.0, 0.0, 0.0]})

    first = await memory.remember(
        db_session,
        elder_id,
        [{"content": "我有糖尿病", "category": "health"}],
        provider=provider,
    )
    await db_session.commit()
    assert first["written"] == 1

    second = await memory.remember(
        db_session,
        elder_id,
        [{"content": "我有糖尿病", "category": "health"}],
        provider=provider,
    )
    await db_session.commit()
    assert second["duplicate"] == 1

    rows = await _active_rows(db_session, elder_id)
    assert len(rows) == 1
    assert rows[0].visibility == "care_team"
    assert rows[0].embedding_model == "fake-3"
    assert rows[0].expires_at is not None


async def test_remember_conflict_supersedes_old(db_session, seed, monkeypatch):
    elder_id = seed["elders"]["single"].elder_id
    provider = FakeEmbedding(
        {
            "我不吃甜食": [1.0, 0.0, 0.0],
            "我最近特别想吃月饼": [0.93, 0.368, 0.0],
        }
    )
    _fake_llm(monkeypatch, '{"contradict": true}')

    await memory.remember(
        db_session,
        elder_id,
        [{"content": "我不吃甜食", "category": "preference"}],
        provider=provider,
    )
    await db_session.commit()
    stats = await memory.remember(
        db_session,
        elder_id,
        [{"content": "我最近特别想吃月饼", "category": "preference"}],
        provider=provider,
    )
    await db_session.commit()

    assert stats["superseded"] == 1
    active = await _active_rows(db_session, elder_id)
    assert [row.content for row in active] == ["我最近特别想吃月饼"]

    all_rows = list(
        (
            await db_session.execute(
                select(MemoryChunk).where(MemoryChunk.elder_id == elder_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(all_rows) == 2
    superseded = [row for row in all_rows if row.status == MemoryStatus.SUPERSEDED]
    assert [row.content for row in superseded] == ["我不吃甜食"]


async def _insert_chunk(
    db_session,
    elder_id,
    content: str,
    vector: list[float],
    *,
    category: str = "other",
    updated_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> MemoryChunk:
    now = datetime.now(UTC)
    row = MemoryChunk(
        elder_id=elder_id,
        content=content,
        embedding=vector,
        embedding_model="fake-3",
        category=category,
        visibility="private",
        status=MemoryStatus.ACTIVE,
        expires_at=expires_at or now + timedelta(days=90),
        updated_at=updated_at or now,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def test_recall_rerank_filters_noise(db_session, seed, monkeypatch):
    elder_id = seed["elders"]["single"].elder_id
    provider = FakeEmbedding({"我能吃粽子吗": [1.0, 0.0, 0.0]})

    keep_a = await _insert_chunk(db_session, elder_id, "我有糖尿病", [1.0, 0.0, 0.0])
    keep_b = await _insert_chunk(db_session, elder_id, "医生让我少吃甜食", [0.9, 0.1, 0.0])
    await _insert_chunk(db_session, elder_id, "喜欢看京剧", [0.0, 1.0, 0.0])
    await _insert_chunk(db_session, elder_id, "住在二楼", [0.0, 0.0, 1.0])
    await _insert_chunk(db_session, elder_id, "今天天气不错", [-1.0, 0.0, 0.0])
    await db_session.commit()

    _fake_llm(
        monkeypatch,
        '{"keep":["' + str(keep_a.chunk_id) + '","' + str(keep_b.chunk_id) + '"]}',
    )

    results = await memory.recall(db_session, elder_id, "我能吃粽子吗", provider=provider)

    contents = [row.content for row in results]
    assert contents == ["我有糖尿病", "医生让我少吃甜食"]
    assert "喜欢看京剧" not in contents


async def test_recall_degrades_to_top2(db_session, seed, monkeypatch):
    elder_id = seed["elders"]["single"].elder_id
    provider = FakeEmbedding({"最近睡得好吗": [1.0, 0.0, 0.0]})
    await _insert_chunk(db_session, elder_id, "我有糖尿病", [1.0, 0.0, 0.0])
    await _insert_chunk(db_session, elder_id, "医生让我少吃甜食", [0.9, 0.1, 0.0])
    await _insert_chunk(db_session, elder_id, "喜欢看京剧", [0.0, 1.0, 0.0])
    await _insert_chunk(db_session, elder_id, "住在二楼", [0.0, 0.0, 1.0])
    await db_session.commit()

    async def _boom(*args, **kwargs):
        raise LlmUnavailable("down")

    monkeypatch.setattr("app.services.memory.complete_chat", _boom)

    results = await memory.recall(db_session, elder_id, "最近睡得好吗", provider=provider)

    assert len(results) == memory.RECALL_DEGRADED_KEEP
    assert results[0].content == "我有糖尿病"


async def test_maintenance_dry_run_then_apply_is_idempotent(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    provider = FakeEmbedding()
    now = datetime.now(UTC)

    expired = await _insert_chunk(
        db_session,
        elder_id,
        "过期的健康记忆",
        [1.0, 0.0, 0.0],
        category="health",
        expires_at=now - timedelta(days=1),
    )
    fresh_dup = await _insert_chunk(
        db_session,
        elder_id,
        "我喜欢甜食",
        [0.0, 1.0, 0.0],
        category="preference",
        updated_at=now,
    )
    old_dup = await _insert_chunk(
        db_session,
        elder_id,
        "我喜欢甜食（重复）",
        [0.0, 1.0, 0.0],
        category="preference",
        updated_at=now - timedelta(days=2),
    )
    cold = await _insert_chunk(
        db_session,
        elder_id,
        "很久以前的一件小事",
        [0.0, 0.0, 1.0],
        category="event",
        updated_at=now - timedelta(days=100),
    )
    await db_session.commit()

    dry = await memory.maintenance(db_session, elder_id=elder_id, dry_run=True, provider=provider)
    assert dry["scanned"] == 4
    assert dry["suppressed"] == 2
    assert dry["merged"] == 1
    for row in (expired, fresh_dup, old_dup, cold):
        await db_session.refresh(row)
        assert row.status == MemoryStatus.ACTIVE

    applied = await memory.maintenance(
        db_session, elder_id=elder_id, dry_run=False, provider=provider
    )
    await db_session.commit()
    assert applied == {"scanned": 4, "suppressed": 2, "merged": 1, "dry_run": False}

    await db_session.refresh(expired)
    await db_session.refresh(old_dup)
    await db_session.refresh(cold)
    await db_session.refresh(fresh_dup)
    assert expired.status == MemoryStatus.SUPPRESSED
    assert cold.status == MemoryStatus.SUPPRESSED
    assert old_dup.status == MemoryStatus.SUPERSEDED
    assert fresh_dup.status == MemoryStatus.ACTIVE

    again = await memory.maintenance(
        db_session, elder_id=elder_id, dry_run=False, provider=provider
    )
    await db_session.commit()
    assert again["scanned"] == 1
    assert again["suppressed"] == 0
    assert again["merged"] == 0


def test_build_memory_context_caps_items_and_length():
    memories = [SimpleNamespace(content="很长的记忆" * 20) for _ in range(4)]

    context = memory.build_memory_context(memories)

    assert context is not None
    lines = context.splitlines()
    assert lines[0] == "【关于长者的记忆】"
    assert len(lines) == 4  # 标题 + 最多 3 条
    for line in lines[1:]:
        assert line.startswith("- ")
        assert len(line) - 2 <= memory.CONTEXT_MAX_CHARS


def test_build_memory_context_empty():
    assert memory.build_memory_context([]) is None


def test_visibility_rules():
    assert memory._visibility_for("平时吃的药", "other") == "care_team"
    assert memory._visibility_for("有糖尿病", "health") == "care_team"
    assert memory._visibility_for("喜欢越剧", "preference") == "private"
