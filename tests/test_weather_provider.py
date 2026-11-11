"""天气 Provider 单测：全部走 ``httpx.MockTransport``，不发起真实网络。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "mcp-servers" / "weather" / "providers.py"
_ENV_KEYS = (
    "MCP_WEATHER_PROVIDER",
    "MCP_WEATHER_DEFAULT_CITY",
    "MCP_WEATHER_TIMEOUT_SECONDS",
    "MCP_WEATHER_CACHE_TTL_SECONDS",
)


def _load_providers():
    spec = importlib.util.spec_from_file_location("kangban_weather_providers", PROVIDERS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


providers = _load_providers()


@pytest.fixture(autouse=True)
def _clean_weather_state(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    providers._reset_cache()
    yield
    providers._reset_cache()


def _geocode_response(name: str = "上海") -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": [{"name": name, "latitude": 31.22, "longitude": 121.46}]},
    )


def _forecast_response(code: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "current": {
                "temperature_2m": 21.4,
                "relative_humidity_2m": 58,
                "weather_code": code,
                "wind_speed_10m": 3.2,
            },
            "daily": {
                "temperature_2m_max": [26.2],
                "temperature_2m_min": [19.6],
                "precipitation_probability_max": [10],
            },
        },
    )


def _open_meteo_handler(code: int, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "geocoding-api.open-meteo.com":
            return _geocode_response()
        assert request.url.host == "api.open-meteo.com"
        return _forecast_response(code)

    return handler


async def test_fetch_open_meteo_returns_readable_report():
    calls: list[str] = []
    transport = httpx.MockTransport(_open_meteo_handler(1, calls))

    text = await providers.fetch_open_meteo("上海", transport=transport)

    assert "上海" in text
    assert "多云" in text
    assert "20 到 26 度" in text
    assert "Open-Meteo" in text
    assert "示例数据" not in text
    assert calls == ["geocoding-api.open-meteo.com", "api.open-meteo.com"]


async def test_geocode_empty_result_asks_bigger_city():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, json={"results": []})

    text = await providers.fetch_open_meteo(
        "不存在的城市", transport=httpx.MockTransport(handler)
    )

    assert "没有查到" in text
    assert "城市" in text
    assert calls == ["geocoding-api.open-meteo.com"]


async def test_local_city_without_default_asks_city_name():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("不应发起网络请求")

    text = await providers.get_weather_report("本地", transport=httpx.MockTransport(handler))

    assert text == "您想查哪个城市的天气？告诉我城市名就好。"


async def test_local_city_uses_configured_default(monkeypatch):
    monkeypatch.setenv("MCP_WEATHER_DEFAULT_CITY", "杭州")

    text = await providers.get_weather_report("本地", provider="fixture")

    assert "杭州" in text


async def test_http_error_raises_weather_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"reason": "boom"})

    with pytest.raises(providers.WeatherUnavailable):
        await providers.fetch_open_meteo("上海", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(("code", "word"), [(0, "晴"), (61, "雨"), (95, "雷雨")])
async def test_wmo_code_mapping(code: int, word: str):
    calls: list[str] = []
    transport = httpx.MockTransport(_open_meteo_handler(code, calls))

    text = await providers.fetch_open_meteo("上海", transport=transport)

    assert word in text
    assert "Open-Meteo" in text


async def test_same_city_second_call_hits_cache():
    calls: list[str] = []
    transport = httpx.MockTransport(_open_meteo_handler(1, calls))

    first = await providers.get_weather_report("上海", provider="open_meteo", transport=transport)
    second = await providers.get_weather_report("上海", provider="open_meteo", transport=transport)

    assert first == second
    assert len(calls) == 2  # 地理编码 + 预报各一次，第二次命中缓存


async def test_fixture_provider_keeps_sample_marker():
    text = await providers.get_weather_report("北京", provider="fixture")

    assert "示例数据" in text
    assert "北京" in text
