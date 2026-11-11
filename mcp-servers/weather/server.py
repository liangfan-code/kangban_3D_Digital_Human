"""康伴天气 MCP Server：真实免费天气（Open-Meteo）。

- 传输：stdio，JSON-RPC 由官方 MCP SDK 处理；
- 数据：Open-Meteo 地理编码 + 当日预报（免费、无需 API Key）；
  ``MCP_WEATHER_PROVIDER=fixture`` 时退回本地示例数据，仅用于测试/离线演示；
- 失败：抛 ``WeatherUnavailable``，由 MCP SDK 转 ``isError``，客户端走可见降级，
  绝不编造温度；
- 独立进程运行：

    python mcp-servers/weather/server.py
"""

from __future__ import annotations

import anyio
from mcp.server.mcpserver import MCPServer
from providers import get_weather_report

server = MCPServer("kangban-weather")


@server.tool()
async def get_weather(city: str = "本地") -> str:
    """查询真实天气（Open-Meteo 免费数据），返回一句可直接朗读的中文。"""
    return await get_weather_report(city)


if __name__ == "__main__":
    anyio.run(server.run_stdio_async)
