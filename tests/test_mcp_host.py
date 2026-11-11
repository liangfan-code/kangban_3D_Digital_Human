"""Day6.4 McpHost 单测：真实 stdio 子进程，统一走 fixture Provider，不访问外网。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.agent.mcp_client import McpHost, McpServerConfig

_WEATHER_SCRIPT = "mcp-servers/weather/server.py"
_BROKEN_SCRIPT = "mcp-servers/weather/does-not-exist.py"
_FIXTURE_ENV = {"MCP_WEATHER_PROVIDER": "fixture"}
_ALIAS = {"get_weather": "weather_get"}


def _config(server_id: str, **overrides: Any) -> McpServerConfig:
    """按测试意图构造 Server 配置；不读 config/mcp_servers.json。"""
    fields: dict[str, Any] = {
        "id": server_id,
        "args": (_WEATHER_SCRIPT,),
        "cwd": ".",
        "env": dict(_FIXTURE_ENV),
        # 每次调用都新建子进程，冷启动需导入 mcp/httpx（本机 3~5s），
        # 因此测试超时放宽，避免把环境慢误判成降级。
        "timeout_seconds": 60,
        "discovery_timeout_seconds": 60,
    }
    fields.update(overrides)
    return McpServerConfig(**fields)


async def test_two_servers_expose_distinct_tool_names():
    """同一脚本挂两个 id：别名与默认前缀不冲突，且都可用。"""
    host = McpHost(
        [
            _config("weather", tool_aliases=_ALIAS),
            _config("weather2"),
        ]
    )

    await host.start()

    names = [info.exposed_name for info in host.tools_snapshot()]
    assert "weather_get" in names
    assert "weather2__get_weather" in names
    assert len(names) == len(set(names)) == 2

    status = {item["id"]: item for item in host.server_status()}
    assert status["weather"]["available"] is True
    assert status["weather2"]["available"] is True
    assert status["weather"]["tool_count"] == 1
    assert status["weather2"]["tool_count"] == 1


async def test_call_returns_fixture_weather_text():
    host = McpHost([_config("weather", tool_aliases=_ALIAS)])
    await host.start()

    result = await host.call("weather_get", {"city": "上海"})

    assert result.ok is True, result.error
    assert "示例数据" in result.content
    assert "上海" in result.content
    assert result.server_id == "weather"


async def test_allow_and_deny_filters_control_discovery():
    """allow_tools 白名单与 deny_tools 黑名单都在发现期生效，Server 仍标记 available。"""
    allowed = McpHost(
        [_config("weather", tool_aliases=_ALIAS, allow_tools=("get_weather",))]
    )
    allowed_empty = McpHost(
        [_config("weather", tool_aliases=_ALIAS, allow_tools=("not_exists",))]
    )
    denied = McpHost(
        [_config("weather", tool_aliases=_ALIAS, deny_tools=("get_weather",))]
    )

    await asyncio.gather(allowed.start(), allowed_empty.start(), denied.start())

    assert [info.exposed_name for info in allowed.tools_snapshot()] == ["weather_get"]
    assert allowed_empty.tools_snapshot() == []
    assert denied.tools_snapshot() == []
    for host in (allowed_empty, denied):
        status = host.server_status()[0]
        assert status["available"] is True
        assert status["tool_count"] == 0
        assert status["error"] is None


async def test_broken_server_degrades_without_raising():
    host = McpHost(
        [
            _config("broken", args=(_BROKEN_SCRIPT,)),
            _config("weather", tool_aliases=_ALIAS),
        ]
    )

    await host.start()

    status = {item["id"]: item for item in host.server_status()}
    assert status["broken"]["available"] is False
    assert status["broken"]["error"]
    assert status["broken"]["tool_count"] == 0
    assert status["weather"]["available"] is True
    assert [info.exposed_name for info in host.tools_snapshot()] == ["weather_get"]


async def test_disabled_server_is_skipped():
    host = McpHost([_config("weather", enabled=False, tool_aliases=_ALIAS)])

    await host.start()

    assert host.server_status() == [
        {
            "id": "weather",
            "enabled": False,
            "available": False,
            "tool_count": 0,
            "error": None,
        }
    ]
    assert host.tools_snapshot() == []


async def test_refresh_is_idempotent_and_keeps_tools():
    host = McpHost([_config("weather", tool_aliases=_ALIAS)])
    await host.start()

    await host.refresh()
    await host.refresh()

    assert [info.exposed_name for info in host.tools_snapshot()] == ["weather_get"]
    result = await host.call("weather_get", {"city": "上海"})
    assert result.ok is True, result.error
    assert "上海" in result.content
