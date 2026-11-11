"""Day6.3 Tool 层测试：注册表、MCP Host 合并、HITL 高危动作、上下文注入。"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.mcp_client import McpCallResult, McpToolInfo
from app.agent.tools import ToolContext, build_default_registry
from app.services.hitl import hitl_queue

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


async def test_medication_stub_never_fabricates():
    registry = build_default_registry(mcp_host=FakeHost())
    result = await registry.call("medication_query", {}, ToolContext())
    assert result.ok is False
    assert result.degraded == "medication_not_configured"
    assert "药名" not in result.content


async def test_media_play_emits_control_event():
    registry = build_default_registry(mcp_host=FakeHost())
    result = await registry.call("media_play", {"genre": "越剧"}, ToolContext())
    assert result.client_events == [
        {"type": "control", "action": "media_play", "genre": "越剧"}
    ]


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
