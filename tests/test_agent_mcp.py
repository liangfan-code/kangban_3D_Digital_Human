"""Day6.4 MCP 集成：真实 stdio 子进程；默认不访问外网。"""

from __future__ import annotations

import os

import pytest

from app.agent.mcp_client import McpHost, McpServerConfig

_WEATHER_SCRIPT = "mcp-servers/weather/server.py"
_ALIAS = {"get_weather": "weather_get"}


def _weather_host(server_env: dict[str, str]) -> McpHost:
    return McpHost(
        [
            McpServerConfig(
                id="weather",
                args=(_WEATHER_SCRIPT,),
                cwd=".",
                env=server_env,
                tool_aliases=_ALIAS,
                # 子进程冷启动导入 mcp/httpx 需数秒，测试超时放宽避免误判降级
                timeout_seconds=60,
                discovery_timeout_seconds=60,
            )
        ]
    )


async def test_real_weather_server_over_stdio():
    """默认走 fixture Provider：真实子进程返回示例数据，不访问外网。"""
    host = _weather_host({"MCP_WEATHER_PROVIDER": "fixture"})
    await host.start()

    result = await host.call("weather_get", {"city": "上海"})

    assert result.ok is True, result.error
    assert "示例数据" in result.content
    assert "上海" in result.content


async def test_missing_server_file_degrades_without_raising():
    host = McpHost(
        [
            McpServerConfig(
                id="weather",
                args=("mcp-servers/weather/does-not-exist.py",),
                cwd=".",
                tool_aliases=_ALIAS,
                discovery_timeout_seconds=5,
            )
        ]
    )

    await host.start()

    status = host.server_status()[0]
    assert status["available"] is False
    assert status["error"]

    result = await host.call("weather_get", {"city": "上海"})
    assert result.ok is False
    assert result.content


@pytest.mark.skipif(
    os.getenv("MCP_WEATHER_LIVE_TEST") != "1",
    reason="需要显式开启真实天气网络测试",
)
async def test_open_meteo_weather_live():
    """显式开启后访问真实 Open-Meteo：MCP_WEATHER_LIVE_TEST=1 pytest -k live。"""
    host = _weather_host({"MCP_WEATHER_PROVIDER": "open_meteo"})
    await host.start()

    result = await host.call("weather_get", {"city": "上海"})

    assert result.ok is True, result.error
    assert "上海" in result.content
    assert "Open-Meteo" in result.content
    assert "示例数据" not in result.content
