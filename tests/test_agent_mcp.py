"""Day6.3 MCP 集成：真实启动天气 Server 子进程并调用一次工具。"""

from __future__ import annotations

from app.agent.mcp_client import McpWeatherClient


async def test_real_weather_server_over_stdio():
    """默认 Server 路径存在且能以本地 fixture 正常返回（不访问外网）。"""
    result = await McpWeatherClient(timeout_s=20).call_weather("上海")
    assert result.ok is True, result.error
    assert "上海" in result.content
    assert "示例数据" in result.content


async def test_missing_server_file_degrades_without_raising():
    result = await McpWeatherClient(server_path="does-not-exist.py", timeout_s=3).call_weather()
    assert result.ok is False
    assert result.error is not None
    assert result.content  # 有可朗读的降级话术
