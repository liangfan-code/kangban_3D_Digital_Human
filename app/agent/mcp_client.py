"""MCP stdio 客户端（Day6.3）：把天气 Server 包成一次工具调用。

策略：每次调用起一个短生命周期子进程，用完即退。
- 优点：不把 MCP 会话生命周期绑到 uvicorn 事件循环，避免 aiortc 式的跨循环坑；
- 代价：每次约多几十到一百毫秒，Day6 可接受，Day9 再评估常驻会话；
- 失败：超时/子进程异常统一转成 ``ok=False``，由上层可见降级，不阻塞语音。
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVER = PROJECT_ROOT / "mcp-servers" / "weather" / "server.py"


@dataclass(frozen=True)
class McpCallResult:
    ok: bool
    content: str
    elapsed_ms: int
    error: str | None = None


def _extract_text(result: object) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


class McpWeatherClient:
    """天气 Tool 的 MCP 客户端；只暴露 ``call_weather``，不泄露会话细节。"""

    def __init__(self, server_path: str | Path | None = None, timeout_s: float = 5.0) -> None:
        env_path = os.getenv("MCP_WEATHER_SERVER")
        self._server_path = Path(server_path or env_path or DEFAULT_SERVER)
        self._timeout_s = timeout_s

    async def call_weather(self, city: str = "本地") -> McpCallResult:
        started = perf_counter()
        try:
            text = await asyncio.wait_for(self._call(city), timeout=self._timeout_s)
            if not text:
                raise RuntimeError("MCP 返回空内容")
            return McpCallResult(True, text, int((perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 —— 外部进程异常统一降级
            return McpCallResult(
                False,
                "天气服务暂时不可用，我先帮您记着，等会儿再试试。",
                int((perf_counter() - started) * 1000),
                f"{type(exc).__name__}: {exc}"[:200],
            )

    async def _call(self, city: str) -> str:
        if not self._server_path.exists():
            raise FileNotFoundError(str(self._server_path))
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(self._server_path)],
            cwd=str(self._server_path.parent),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "get_weather",
                    {"city": city},
                    read_timeout_seconds=self._timeout_s,
                )
        if getattr(result, "isError", False):
            raise RuntimeError("MCP tool returned error")
        return _extract_text(result)
