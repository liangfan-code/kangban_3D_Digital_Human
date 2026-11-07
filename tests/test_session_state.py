"""会话状态存储测试：内存模式（force_memory=True，不依赖真实 Redis）。"""

from app.services.context_summary import build_context_message
from app.services.session_state import (
    MedicationSlot,
    SessionState,
    SessionStateStore,
    new_state,
)


async def test_default_state_for_unknown_thread():
    store = SessionStateStore(force_memory=True)
    state = await store.load("thread-1")
    assert state.thread_id == "thread-1"
    assert state.summary == ""
    assert state.summary_upto_seq == 0
    assert state.turn_count == 0
    assert state.slots.intent is None


async def test_save_and_load_roundtrip():
    store = SessionStateStore(force_memory=True)
    state = new_state("thread-2")
    state.summary = "张奶奶喜欢京剧"
    state.summary_upto_seq = 12
    state.turn_count = 3
    state.slots.intent = "medication"
    state.slots.medication = MedicationSlot(drug="降压药", time="八点", confirmed=True)
    await store.save(state)

    loaded = await store.load("thread-2")
    assert loaded.summary == "张奶奶喜欢京剧"
    assert loaded.summary_upto_seq == 12
    assert loaded.turn_count == 3
    assert loaded.slots.intent == "medication"
    assert loaded.slots.medication.drug == "降压药"
    assert loaded.slots.medication.time == "八点"
    assert loaded.slots.medication.confirmed is True


async def test_mutate_updates_and_persists():
    store = SessionStateStore(force_memory=True)
    async with store.mutate("thread-3") as state:
        state.turn_count += 1
        state.slots.intent = "media"
    async with store.mutate("thread-3") as state:
        state.turn_count += 1

    loaded = await store.load("thread-3")
    assert loaded.turn_count == 2
    assert loaded.slots.intent == "media"


def test_state_json_roundtrip_keeps_slots():
    state = new_state("thread-4")
    state.slots.intent = "identity"
    state.slots.identity.candidate = "王奶奶"
    restored = SessionState.model_validate_json(state.model_dump_json())
    assert restored.slots.intent == "identity"
    assert restored.slots.identity.candidate == "王奶奶"


def test_build_context_message_empty_returns_none():
    assert build_context_message(new_state("t")) is None


def test_build_context_message_contains_summary_and_slots():
    state = new_state("t")
    state.summary = "李爷爷对青霉素过敏"
    state.slots.intent = "medication"
    state.slots.medication.drug = "降压药"
    message = build_context_message(state)
    assert message is not None
    assert "【会话摘要】李爷爷对青霉素过敏" in message
    assert "【当前状态】" in message
    assert "降压药" in message
