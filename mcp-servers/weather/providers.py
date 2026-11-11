"""天气数据 Provider：Open-Meteo 真实数据 + 离线 fixture。

- 只访问 Open-Meteo 两个域名（地理编码、预报），免费、无需 API Key；
- 失败（网络、HTTP 非 200、响应缺字段）统一抛 ``WeatherUnavailable``，由 MCP 层转
  ``isError``，客户端再走可见降级，绝不编造温度；
- 提供内存 TTL 缓存，只缓存最终文本，不缓存原始响应；
- 日志只记事件、Provider、耗时与错误类型，不记完整 URL 或查询串。

该目录名带连字符，不能按包导入；测试通过 ``importlib`` 按文件路径加载本模块。
"""

from __future__ import annotations

import logging
import os
from time import monotonic, perf_counter

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_USER_AGENT = "kangban-weather/1.0"
_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_CACHE_TTL_SECONDS = 600.0
_CACHE_CAPACITY = 32

_CITY_NOT_FOUND = "没有查到您说的这个城市，可以说大一点的城市名。"
_CITY_MISSING = "您想查哪个城市的天气？告诉我城市名就好。"

logger = logging.getLogger(__name__)


class WeatherUnavailable(RuntimeError):
    """Open-Meteo 不可用：网络错误、HTTP 非 200 或响应缺少必要字段。"""


# WMO weather code → (中文天气词, 适老建议)
_WMO_CONDITIONS: dict[int, tuple[str, str]] = {
    0: ("晴", "适合出门走走"),
    1: ("多云", "天气不错，适合散步"),
    2: ("多云", "天气不错，适合散步"),
    3: ("阴", "不晒，适合散步"),
    45: ("雾", "有雾，出门慢一点"),
    48: ("雾", "有雾，出门慢一点"),
    51: ("毛毛雨", "雨不大，出门带把伞"),
    53: ("毛毛雨", "雨不大，出门带把伞"),
    55: ("毛毛雨", "雨不小，出门记得带伞"),
    56: ("毛毛雨", "下雨又冷，带伞再加件外套"),
    57: ("毛毛雨", "下雨又冷，带伞再加件外套"),
    61: ("雨", "出门记得带伞"),
    63: ("雨", "雨有点大，出门记得带伞"),
    65: ("雨", "雨很大，尽量别出门"),
    66: ("雨", "下雨又冷，带伞再加件外套"),
    67: ("雨", "下雨又冷，带伞再加件外套"),
    71: ("小雪", "下雪了，出门慢一点，注意保暖"),
    73: ("雪", "下雪了，出门慢一点，注意保暖"),
    75: ("雪", "雪比较大，尽量别出门"),
    77: ("雪", "有雪粒，出门慢一点"),
    80: ("阵雨", "出门记得带伞"),
    81: ("阵雨", "雨来得急，出门带把伞"),
    82: ("阵雨", "雨很大，尽量别出门"),
    85: ("阵雪", "有阵雪，出门慢一点，注意保暖"),
    86: ("阵雪", "雪比较大，尽量别出门"),
    95: ("雷雨", "打雷下雨，尽量别出门"),
    96: ("雷雨", "打雷下冰雹，尽量别出门"),
    99: ("雷雨", "打雷下冰雹，尽量别出门"),
}

# 城市关键字 → 固定示例数据；仅 ``MCP_WEATHER_PROVIDER=fixture`` 时使用
_FIXTURES: dict[str, dict[str, object]] = {
    "北京": {"condition": "晴", "low": 15, "high": 24, "advice": "早晚凉，出门加件外套"},
    "上海": {"condition": "多云", "low": 20, "high": 26, "advice": "适合出门走走"},
    "广州": {"condition": "阵雨", "low": 24, "high": 30, "advice": "出门记得带伞"},
    "杭州": {"condition": "阴", "low": 19, "high": 25, "advice": "不晒，适合散步"},
}
_DEFAULT_FIXTURE = {"condition": "晴", "low": 18, "high": 25, "advice": "适合出门走走"}

# (provider, 解析后的城市) → (写入时刻, 最终文本)
_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def _reset_cache() -> None:
    """清空缓存；测试用。"""
    _CACHE.clear()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("weather_env_invalid name=%s", name)
        return default
    return value if value > 0 else default


def _describe_weather(code: int) -> tuple[str, str]:
    return _WMO_CONDITIONS.get(code, ("天气变化", "出门前看看窗外，注意添衣"))


def _format_temp(value: float | int) -> str:
    return str(int(round(float(value))))


def _city_label(user_city: str, resolved_name: str) -> str:
    """优先用接口返回的中文名；与用户输入不同时保留输入，便于听懂/断言。"""
    if resolved_name and user_city and user_city not in resolved_name:
        return f"{user_city}（{resolved_name}）"
    return resolved_name or user_city


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, object]) -> dict:
    try:
        response = await client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherUnavailable(type(exc).__name__) from exc
    if not isinstance(payload, dict):
        raise WeatherUnavailable("response_not_object")
    return payload


async def _geocode(
    client: httpx.AsyncClient, city: str
) -> tuple[str, float, float] | None:
    payload = await _get(
        client,
        GEOCODE_URL,
        {"name": city, "count": 1, "language": "zh", "format": "json"},
    )
    results = payload.get("results") or []
    if not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        raise WeatherUnavailable("geocode_invalid_fields")
    try:
        name = str(first.get("name") or city)
        latitude = float(first["latitude"])
        longitude = float(first["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherUnavailable("geocode_missing_fields") from exc
    return name, latitude, longitude


async def _forecast(
    client: httpx.AsyncClient, latitude: float, longitude: float
) -> tuple[str, str, str, str]:
    payload = await _get(
        client,
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
            ),
            "daily": (
                "temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            ),
            "timezone": "Asia/Shanghai",
            "forecast_days": 1,
        },
    )
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise WeatherUnavailable("forecast_invalid_fields")
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    code = current.get("weather_code")
    if code is None or not highs or not lows:
        raise WeatherUnavailable("forecast_missing_fields")
    try:
        condition, advice = _describe_weather(int(code))
        low = _format_temp(lows[0])
        high = _format_temp(highs[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise WeatherUnavailable("forecast_invalid_fields") from exc
    return condition, advice, low, high


async def fetch_open_meteo(
    city: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """查询 Open-Meteo 真实天气，返回一句可直接朗读的中文。"""
    started = perf_counter()
    try:
        if client is not None:
            text = await _fetch_with_client(client, city)
        else:
            timeout_value = (
                timeout
                if timeout is not None
                else _env_float("MCP_WEATHER_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
            )
            async with httpx.AsyncClient(
                timeout=timeout_value,
                transport=transport,
                headers={"User-Agent": _USER_AGENT},
            ) as owned:
                text = await _fetch_with_client(owned, city)
    except WeatherUnavailable as exc:
        logger.warning(
            "weather_fetch_failed provider=open_meteo error=%s elapsed_ms=%d",
            str(exc) or type(exc).__name__,
            int((perf_counter() - started) * 1000),
        )
        raise
    logger.info(
        "weather_fetch_ok provider=open_meteo elapsed_ms=%d",
        int((perf_counter() - started) * 1000),
    )
    return text


async def _fetch_with_client(client: httpx.AsyncClient, city: str) -> str:
    location = await _geocode(client, city)
    if location is None:
        return _CITY_NOT_FOUND
    name, latitude, longitude = location
    condition, advice, low, high = await _forecast(client, latitude, longitude)
    return (
        f"{_city_label(city, name)}今天{condition}，{low} 到 {high} 度，{advice}。"
        "（数据来源 Open-Meteo，仅供参考）"
    )


def fetch_fixture(city: str) -> str:
    """离线 fixture 数据，仅在显式 ``MCP_WEATHER_PROVIDER=fixture`` 时使用。"""
    fixture = next((value for key, value in _FIXTURES.items() if key in city), _DEFAULT_FIXTURE)
    return (
        f"【示例数据】{city}今天{fixture['condition']}，"
        f"{fixture['low']} 到 {fixture['high']} 度，{fixture['advice']}。"
    )


def _resolve_provider(provider: str | None) -> str:
    raw = (
        provider if provider is not None else os.getenv("MCP_WEATHER_PROVIDER") or "open_meteo"
    )
    name = raw.strip().lower()
    if name in {"fixture", "open_meteo"}:
        return name
    logger.warning("weather_provider_unknown provider=%s fallback=open_meteo", name)
    return "open_meteo"


def _cache_get(key: tuple[str, str]) -> str | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    stored_at, text = entry
    if monotonic() - stored_at > _env_float(
        "MCP_WEATHER_CACHE_TTL_SECONDS", _DEFAULT_CACHE_TTL_SECONDS
    ):
        _CACHE.pop(key, None)
        return None
    return text


def _cache_put(key: tuple[str, str], text: str) -> None:
    _CACHE.pop(key, None)
    while len(_CACHE) >= _CACHE_CAPACITY:
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = (monotonic(), text)


async def get_weather_report(
    city: str,
    *,
    provider: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """按 Provider 配置返回天气话术；失败抛 ``WeatherUnavailable``，不编造数据。"""
    started = perf_counter()
    provider_name = _resolve_provider(provider)

    query_city = (city or "").strip()
    if not query_city or query_city == "本地":
        default_city = (os.getenv("MCP_WEATHER_DEFAULT_CITY") or "").strip()
        if not default_city:
            return _CITY_MISSING
        query_city = default_city

    cache_key = (provider_name, query_city)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(
            "weather_cache_hit provider=%s elapsed_ms=%d",
            provider_name,
            int((perf_counter() - started) * 1000),
        )
        return cached

    try:
        if provider_name == "fixture":
            text = fetch_fixture(query_city)
        else:
            text = await fetch_open_meteo(query_city, transport=transport)
    except WeatherUnavailable as exc:
        logger.warning(
            "weather_unavailable provider=%s error=%s elapsed_ms=%d",
            provider_name,
            type(exc).__name__,
            int((perf_counter() - started) * 1000),
        )
        raise

    _cache_put(cache_key, text)
    logger.info(
        "weather_report_ready provider=%s elapsed_ms=%d",
        provider_name,
        int((perf_counter() - started) * 1000),
    )
    return text
