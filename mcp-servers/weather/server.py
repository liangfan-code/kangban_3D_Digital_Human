"""康伴天气 MCP Server（Day6.3）。

- 传输：stdio，JSON-RPC 由官方 MCP SDK 处理；
- 数据：本地 fixture 模拟，**不调用任何外部天气 API**（AGENTS.md 安全禁区第 4 条）；
- 返回：一句可直接朗读的中文，并明确标注"示例数据"，避免长者误信为真实预报；
- 独立进程运行，主应用不可用时互不影响：

    python mcp-servers/weather/server.py
"""

from __future__ import annotations

import anyio
from mcp.server.mcpserver import MCPServer

server = MCPServer("kangban-weather")

# 城市关键字 → 固定示例数据；真实天气服务在 Day9 评估后再授权接入
_FIXTURES: dict[str, dict[str, object]] = {
    "北京": {"condition": "晴", "low": 15, "high": 24, "advice": "早晚凉，出门加件外套"},
    "上海": {"condition": "多云", "low": 20, "high": 26, "advice": "适合出门走走"},
    "广州": {"condition": "阵雨", "low": 24, "high": 30, "advice": "出门记得带伞"},
    "杭州": {"condition": "阴", "low": 19, "high": 25, "advice": "不晒，适合散步"},
}
_DEFAULT = {"condition": "晴", "low": 18, "high": 25, "advice": "适合出门走走"}


@server.tool()
def get_weather(city: str = "本地") -> str:
    """查询指定城市的天气（本地示例数据，非实时预报）。"""
    fixture = next((value for key, value in _FIXTURES.items() if key in city), _DEFAULT)
    return (
        f"【示例数据】{city}今天{fixture['condition']}，"
        f"{fixture['low']} 到 {fixture['high']} 度，{fixture['advice']}。"
    )


if __name__ == "__main__":
    anyio.run(server.run_stdio_async)
