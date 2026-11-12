"""Day6.3 Tool 层测试：注册表、MCP Host 合并、HITL 高危动作、上下文注入。"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.agent.tools as tools_module
import app.graphs.medication as graph_module
from app.agent.mcp_client import McpCallResult, McpToolInfo
from app.agent.tools import ToolContext, build_default_registry
from app.core.config import get_settings
from app.models import (
    MediaHistory,
    MediaLibrary,
    MedicationEvent,
    MedicationOrder,
    MedicationPlan,
)
from app.models.enums import (
    MediaCopyrightStatus,
    MediaStatus,
    MediaType,
    MedicationOrderStatus,
)
from app.services.hitl import hitl_queue
from app.services.session_state import SessionStateStore

_WEATHER_TOOL = McpToolInfo(
    server_id="weather",
    remote_name="get_weather",
    exposed_name="weather_get",
    description="查询真实天气",
    input_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": [],
    },
)


class FakeHost:
    """MCP Host 替身：固定发现快照，按暴露名返回预置结果。"""

    def __init__(self, ok: bool = True, tools: list[McpToolInfo] | None = None) -> None:
        self.ok = ok
        self._tools = [_WEATHER_TOOL] if tools is None else tools

    def tools_snapshot(self) -> list[McpToolInfo]:
        return list(self._tools)

    async def call(self, exposed_name: str, args: dict[str, Any]) -> McpCallResult:
        if not self.ok:
            return McpCallResult(False, "天气服务暂时不可用。", 3, "boom", "weather")
        city = str(args.get("city") or "本地")
        return McpCallResult(True, f"{city}今天晴，18 到 25 度。", 3, None, "weather")


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


def _patch_tool_session(monkeypatch, db_session) -> None:
    """Tool 与图内默认会话都指向测试事务，避免触达开发库。"""
    factory = _session_factory(db_session)
    monkeypatch.setattr(tools_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(graph_module, "get_session_factory", lambda: factory)


async def _add_active_plan(db_session, elder_id, *, drug_name="降压药") -> MedicationPlan:
    today = datetime.now(ZoneInfo(get_settings().app_timezone)).date()
    plan = MedicationPlan(
        elder_id=elder_id,
        drug_name=drug_name,
        dose="1 片",
        schedule={"times": ["00:00"], "weekdays": []},
        start_date=today,
        status="active",
    )
    db_session.add(plan)
    await db_session.flush()
    return plan


@pytest.fixture(autouse=True)
def _clear_hitl():
    hitl_queue.clear()
    yield
    hitl_queue.clear()


def test_registry_exposes_openai_schemas():
    registry = build_default_registry(mcp_host=FakeHost())
    names = registry.names()
    assert {
        "weather_get",
        "time_get",
        "medication_query",
        "media_play",
        "notify_family",
    } <= set(names)
    schemas = registry.schemas()
    assert all(item["type"] == "function" for item in schemas)
    assert all("parameters" in item["function"] for item in schemas)
    weather = next(
        item["function"] for item in schemas if item["function"]["name"] == "weather_get"
    )
    assert weather["description"] == "查询真实天气"
    assert "city" in weather["parameters"]["properties"]


async def test_time_tool_returns_clock_text():
    registry = build_default_registry(mcp_host=FakeHost())
    result = await registry.call("time_get", {}, ToolContext())
    assert result.ok is True
    assert result.content.startswith("现在是")


async def test_weather_tool_returns_mcp_text():
    registry = build_default_registry(mcp_host=FakeHost())
    result = await registry.call("weather_get", {"city": "上海"}, ToolContext())
    assert result.ok is True
    assert "上海" in result.content
    assert result.mcp_server == "weather"


async def test_weather_tool_failure_is_visible():
    registry = build_default_registry(mcp_host=FakeHost(ok=False))
    result = await registry.call("weather_get", {}, ToolContext())
    assert result.ok is False
    assert result.degraded == "mcp_unavailable:weather"
    assert result.mcp_server == "weather"


def test_empty_snapshot_falls_back_to_local_weather_tool():
    registry = build_default_registry(mcp_host=FakeHost(tools=[]))
    assert "weather_get" in registry.names()


async def test_empty_snapshot_weather_is_degraded_not_unknown_tool():
    registry = build_default_registry(mcp_host=FakeHost(tools=[]))
    result = await registry.call("weather_get", {}, ToolContext())
    assert result.ok is False
    assert result.degraded == "mcp_unavailable:weather"
    assert result.mcp_server is None


async def test_medication_query_without_context_never_fabricates():
    registry = build_default_registry(mcp_host=FakeHost())
    result = await registry.call("medication_query", {}, ToolContext())
    assert result.ok is False
    assert result.degraded == "missing_elder_context"
    assert "药名" not in result.content


async def test_medication_query_returns_real_plan(db_session, seed, monkeypatch):
    await _add_active_plan(db_session, seed["elders"]["d1"].elder_id)
    _patch_tool_session(monkeypatch, db_session)
    registry = build_default_registry(mcp_host=FakeHost())

    result = await registry.call(
        "medication_query", {}, ToolContext(elder_id=str(seed["elders"]["d1"].elder_id))
    )

    assert result.ok is True
    assert "降压药" in result.content
    assert "1 片" in result.content


async def test_medication_query_without_plan_is_explicit(db_session, seed, monkeypatch):
    _patch_tool_session(monkeypatch, db_session)
    registry = build_default_registry(mcp_host=FakeHost())

    result = await registry.call(
        "medication_query", {}, ToolContext(elder_id=str(seed["elders"]["d2"].elder_id))
    )

    assert result.ok is False
    assert result.degraded == "medication_not_configured"
    assert "还没有登记用药计划" in result.content


async def test_medication_confirm_records_order_and_event(db_session, seed, monkeypatch):
    plan = await _add_active_plan(db_session, seed["elders"]["d1"].elder_id)
    _patch_tool_session(monkeypatch, db_session)
    registry = build_default_registry(mcp_host=FakeHost())

    result = await registry.call(
        "medication_confirm",
        {"reply": "我吃过了", "elder_id": str(seed["elders"]["d2"].elder_id)},
        ToolContext(elder_id=str(seed["elders"]["d1"].elder_id)),
    )

    assert result.ok is True
    order = (
        await db_session.execute(
            select(MedicationOrder).where(MedicationOrder.plan_id == plan.plan_id)
        )
    ).scalars().one()
    assert order.elder_id == seed["elders"]["d1"].elder_id
    assert order.status == MedicationOrderStatus.CONFIRMED
    event_types = (
        await db_session.execute(
            select(MedicationEvent.event_type).where(
                MedicationEvent.order_id == order.order_id
            )
        )
    ).scalars().all()
    assert event_types.count("confirmed") == 1


async def test_medication_confirm_degraded_still_records(db_session, seed, monkeypatch):
    plan = await _add_active_plan(db_session, seed["elders"]["d1"].elder_id)
    _patch_tool_session(monkeypatch, db_session)
    monkeypatch.setattr(graph_module, "LANGGRAPH_AVAILABLE", False)
    registry = build_default_registry(mcp_host=FakeHost())

    result = await registry.call(
        "medication_confirm",
        {"reply": "吃过了"},
        ToolContext(elder_id=str(seed["elders"]["d1"].elder_id)),
    )

    assert result.ok is True
    assert result.degraded == "medication_graph_unavailable"
    order = (
        await db_session.execute(
            select(MedicationOrder).where(MedicationOrder.plan_id == plan.plan_id)
        )
    ).scalars().one()
    assert order.status == MedicationOrderStatus.CONFIRMED


async def _add_active_media(db_session, *, title: str = "越剧测试") -> MediaLibrary:
    media = MediaLibrary(
        title=title,
        type=MediaType.OPERA,
        genre="越剧",
        artist="测试",
        media_uri="/static/media/library/test.wav",
        duration_seconds=8,
        copyright_status=MediaCopyrightStatus.LICENSED,
        status=MediaStatus.ACTIVE,
    )
    db_session.add(media)
    await db_session.flush()
    return media


async def test_media_play_without_elder_degrades():
    registry = build_default_registry(mcp_host=FakeHost())
    result = await registry.call("media_play", {"genre": "越剧"}, ToolContext())
    assert result.ok is False
    assert result.degraded == "missing_elder_context"


async def test_media_play_emits_control_event_and_history(
    db_session, seed, monkeypatch
):
    _patch_tool_session(monkeypatch, db_session)
    store = SessionStateStore(force_memory=True)
    monkeypatch.setattr(tools_module, "get_session_store", lambda: store)
    await _add_active_media(db_session)

    registry = build_default_registry(mcp_host=FakeHost())
    ctx = ToolContext(
        elder_id=str(seed["elders"]["single"].elder_id),
        device_id="device-1",
        thread_id="thread-media-play",
    )
    result = await registry.call("media_play", {"genre": "越剧"}, ctx)

    assert result.ok is True
    event = result.client_events[0]
    assert event["type"] == "control"
    assert event["action"] == "media_play"
    assert event["genre"] == "越剧"
    assert event["url"] == "/static/media/library/test.wav"
    assert event["history_id"]

    row = (
        await db_session.execute(
            select(MediaHistory).where(
                MediaHistory.history_id == uuid.UUID(event["history_id"])
            )
        )
    ).scalars().one()
    assert row.finished_at is None


async def test_media_stop_closes_history(db_session, seed, monkeypatch):
    _patch_tool_session(monkeypatch, db_session)
    store = SessionStateStore(force_memory=True)
    monkeypatch.setattr(tools_module, "get_session_store", lambda: store)
    await _add_active_media(db_session)

    registry = build_default_registry(mcp_host=FakeHost())
    ctx = ToolContext(
        elder_id=str(seed["elders"]["single"].elder_id),
        device_id="device-1",
        thread_id="thread-media-stop",
    )
    played = await registry.call("media_play", {"genre": "越剧"}, ctx)
    history_id = uuid.UUID(played.client_events[0]["history_id"])

    stopped = await registry.call("media_stop", {"elapsed_seconds": 3}, ctx)

    assert stopped.ok is True
    assert stopped.client_events == [{"type": "control", "action": "stop"}]
    row = (
        await db_session.execute(
            select(MediaHistory).where(MediaHistory.history_id == history_id)
        )
    ).scalars().one()
    assert row.finished_at is not None


async def test_media_play_without_library_degrades(db_session, seed, monkeypatch):
    _patch_tool_session(monkeypatch, db_session)
    store = SessionStateStore(force_memory=True)
    monkeypatch.setattr(tools_module, "get_session_store", lambda: store)
    registry = build_default_registry(mcp_host=FakeHost())
    ctx = ToolContext(
        elder_id=str(seed["elders"]["single"].elder_id),
        thread_id="thread-media-empty",
    )

    result = await registry.call("media_play", {"genre": "越剧"}, ctx)

    assert result.ok is False
    assert result.degraded == "media_unavailable"


async def test_notify_family_goes_to_hitl_not_direct_send():
    registry = build_default_registry(mcp_host=FakeHost())
    ctx = ToolContext(elder_id="elder-1", device_id="device-1")
    result = await registry.call("notify_family", {"message": "今天挺好"}, ctx)
    assert result.ok is True
    assert result.client_events[0]["type"] == "hitl"
    pending = hitl_queue.pending()
    assert len(pending) == 1
    assert pending[0].kind == "notify_family"
    assert pending[0].elder_id == "elder-1"


async def test_unknown_tool_degrades():
    registry = build_default_registry(mcp_host=FakeHost())
    result = await registry.call("nope", {}, ToolContext())
    assert result.ok is False
    assert result.degraded == "unknown_tool"


async def test_handler_exception_is_contained():
    registry = build_default_registry(mcp_host=FakeHost())

    async def boom(args, ctx):  # noqa: ARG001
        raise RuntimeError("handler down")

    from app.agent.tools import ToolSpec

    registry.register(ToolSpec("boom", "explode", {"type": "object"}, boom))
    result = await registry.call("boom", {}, ToolContext())
    assert result.ok is False
    assert result.degraded == "tool_error:RuntimeError"
