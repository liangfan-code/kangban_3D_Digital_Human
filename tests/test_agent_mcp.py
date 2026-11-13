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


# ---------------- 未指名城市 → 出口 IP 自动定位（2026-11-13 用户拍板） ----------------


def _load_providers():
    """providers 目录名带连字符不能按包导入，按文件路径加载。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "mcp-servers/weather/providers.py"
    spec = importlib.util.spec_from_file_location("kangban_weather_providers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iploc_handler(city: str, lat: float, lon: float, *, loc_ok: bool = True):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://ip-api.com/json/"):
            if not loc_ok:
                return httpx.Response(200, json={"status": "fail"})
            return httpx.Response(
                200, json={"status": "success", "city": city, "lat": lat, "lon": lon}
            )
        return httpx.Response(
            200,
            json={
                "current": {"weather_code": 1, "temperature_2m": 22.0},
                "daily": {"temperature_2m_max": [26.0], "temperature_2m_min": [19.0]},
            },
        )

    return handler


async def test_weather_auto_locates_by_ip_when_no_city(monkeypatch):
    import httpx

    monkeypatch.delenv("MCP_WEATHER_DEFAULT_CITY", raising=False)
    monkeypatch.delenv("MCP_WEATHER_PROVIDER", raising=False)
    mod = _load_providers()
    mod._reset_cache()

    text = await mod.get_weather_report(
        "", transport=httpx.MockTransport(_iploc_handler("杭州", 30.25, 120.17))
    )

    assert "杭州" in text
    assert "示例数据" not in text


async def test_weather_iploc_failure_asks_city_instead_of_guessing(monkeypatch):
    """定位不可用 → 可见降级反问城市，绝不编造。"""
    import httpx

    monkeypatch.delenv("MCP_WEATHER_DEFAULT_CITY", raising=False)
    monkeypatch.delenv("MCP_WEATHER_PROVIDER", raising=False)
    mod = _load_providers()
    mod._reset_cache()

    text = await mod.get_weather_report(
        "", transport=httpx.MockTransport(_iploc_handler("杭州", 30.25, 120.17, loc_ok=False))
    )

    assert text == mod._CITY_MISSING
