"""MCP 通用客户端（Day6.4）：配置驱动的多 Server 装载、发现与调用。

设计取舍：
- 每个 Server 都在配置里声明，新增/更换/停用只改 ``config/mcp_servers.json``；
- 发现只在启动期做一次 ``initialize + list_tools``，调用仍是短生命周期会话，
  避免把 MCP 会话绑死在 uvicorn 事件循环上（遵守 AGENTS 实时模块规则）；
- 配置缺失、单 Server 发现失败、调用超时都只降级、不抛异常：应用与语音链路照常工作，
  失败必须可见（日志 event + ``McpCallResult`` / ``server_status``）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client import streamable_http as _streamable_http
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "mcp_servers.json"

_ID_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
_EXPOSED_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")
_TRANSPORTS = frozenset({"stdio", "streamable_http", "sse"})

# 子进程只继承运行 Python 最低限度的变量；绝不透传 os.environ 全量，
# 否则 LLM_API_KEY / JWT_SECRET_KEY / DATABASE_URL 会泄漏给外部 Server。
_BASE_ENV_KEYS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP")

_UNKNOWN_TOOL_TEXT = "这个功能我暂时还不会用。"
_CALL_FAILED_TEXT = "这个功能暂时不可用，我先帮您记着。"


@dataclass(frozen=True)
class McpCallResult:
    """单次 MCP 工具调用结果；前 4 个字段保持原有位置参数顺序。"""

    ok: bool
    content: str
    elapsed_ms: int
    error: str | None = None
    server_id: str | None = None


@dataclass(frozen=True)
class McpServerConfig:
    """一个 MCP Server 的声明式配置；字段缺省即「不启用该能力」。"""

    id: str
    enabled: bool = True
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_from: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 8.0
    discovery_timeout_seconds: float = 10.0
    tool_prefix: str | None = None
    tool_aliases: dict[str, str] = field(default_factory=dict)
    allow_tools: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpToolInfo:
    """发现阶段缓存的工具元数据；``exposed_name`` 是上层实际调用的名字。"""

    server_id: str
    remote_name: str
    exposed_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ServerStatus:
    """单个 Server 的发现状态；只供自检/管理端展示。"""

    id: str
    enabled: bool
    available: bool = False
    tool_count: int = 0
    error: str | None = None


def _as_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} 必须是字符串数组")
    return tuple(value)


def _as_str_map(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{field_name} 必须是字符串键值对象")
    return dict(value)


def _as_float(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} 必须是数字")
    if value <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return float(value)


def _parse_server(raw: Any, default_timeout: float) -> McpServerConfig | None:
    """校验单条配置；任何不合法都只跳过该条并记日志，不影响其余 Server。"""
    if not isinstance(raw, dict):
        logger.warning("MCP 配置条目不是对象，已跳过", extra={"event": "mcp_config_invalid"})
        return None

    raw_id = raw.get("id")
    server_id = raw_id if isinstance(raw_id, str) else "<missing>"
    try:
        if not isinstance(raw_id, str) or not _ID_PATTERN.fullmatch(raw_id):
            raise ValueError("id 必须匹配 ^[a-z0-9_-]{1,32}$")

        transport = raw.get("transport", "stdio")
        if transport not in _TRANSPORTS:
            raise ValueError(f"transport 不支持：{transport}")

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")

        command = raw.get("command")
        if command is not None and not isinstance(command, str):
            raise ValueError("command 必须是字符串")
        url = raw.get("url")
        if url is not None and not isinstance(url, str):
            raise ValueError("url 必须是字符串")

        args = _as_str_tuple(raw.get("args"), "args")
        if transport == "stdio":
            if not args:
                raise ValueError("stdio 必须配置 args")
            if url:
                raise ValueError("stdio 不能同时配置 url")
        elif not url:
            raise ValueError(f"{transport} 必须配置 url")

        return McpServerConfig(
            id=server_id,
            enabled=enabled,
            transport=transport,
            command=command,
            args=args,
            cwd=raw.get("cwd") if isinstance(raw.get("cwd"), str) else None,
            env=_as_str_map(raw.get("env"), "env"),
            env_from=_as_str_tuple(raw.get("env_from"), "env_from"),
            url=url,
            headers=_as_str_map(raw.get("headers"), "headers"),
            timeout_seconds=_as_float(
                raw.get("timeout_seconds"), "timeout_seconds", default_timeout
            ),
            discovery_timeout_seconds=_as_float(
                raw.get("discovery_timeout_seconds"), "discovery_timeout_seconds", 10.0
            ),
            tool_prefix=raw.get("tool_prefix")
            if isinstance(raw.get("tool_prefix"), str)
            else None,
            tool_aliases=_as_str_map(raw.get("tool_aliases"), "tool_aliases"),
            allow_tools=_as_str_tuple(raw.get("allow_tools"), "allow_tools"),
            deny_tools=_as_str_tuple(raw.get("deny_tools"), "deny_tools"),
        )
    except ValueError as exc:
        logger.warning(
            "MCP 配置条目无效已跳过：id=%s 原因=%s",
            server_id,
            exc,
            extra={"event": "mcp_config_invalid", "reason": str(exc)},
        )
        return None


def load_mcp_servers(path: str | Path | None = None) -> list[McpServerConfig]:
    """加载并校验 Server 配置；文件缺失/损坏一律返回空列表且不抛异常。"""
    config_path = Path(path) if path else _config_path_from_env()

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "MCP 配置文件不存在：%s", config_path, extra={"event": "mcp_config_missing"}
        )
        return []
    except OSError as exc:
        logger.warning(
            "MCP 配置文件读取失败：%s",
            type(exc).__name__,
            extra={"event": "mcp_config_invalid", "reason": type(exc).__name__},
        )
        return []

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "MCP 配置文件 JSON 解析失败：%s",
            exc.msg,
            extra={"event": "mcp_config_invalid", "reason": "json_decode_error"},
        )
        return []

    if not isinstance(payload, dict):
        logger.warning(
            "MCP 配置顶层必须是对象", extra={"event": "mcp_config_invalid", "reason": "not_object"}
        )
        return []

    entries = payload.get("servers")
    if not isinstance(entries, list):
        logger.warning(
            "MCP 配置缺少 servers 数组",
            extra={"event": "mcp_config_invalid", "reason": "servers_not_list"},
        )
        return []

    default_timeout = 8.0
    try:
        default_timeout = float(get_settings().mcp_default_call_timeout_seconds)
    except Exception:  # noqa: BLE001 —— 配置层异常不应影响加载流程
        pass

    servers: list[McpServerConfig] = []
    seen_ids: set[str] = set()
    for entry in entries:
        config = _parse_server(entry, default_timeout)
        if config is None:
            continue
        if config.id in seen_ids:
            logger.warning(
                "MCP Server id 重复已跳过：%s",
                config.id,
                extra={"event": "mcp_config_invalid", "reason": "duplicate_id"},
            )
            continue
        seen_ids.add(config.id)
        servers.append(config)
    return servers


def _config_path_from_env() -> Path:
    env_path = os.getenv("MCP_CONFIG_PATH")
    return Path(env_path) if env_path else DEFAULT_CONFIG_PATH


def _extract_text(result: object) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _resolve_cwd(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    return str(path if path.is_absolute() else PROJECT_ROOT / path)


def _child_env(config: McpServerConfig) -> dict[str, str]:
    """构造 stdio 子进程环境：最小白名单 + 显式 env + env_from 透传。"""
    env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUTF8"] = "1"
    env.update(config.env)
    for name in config.env_from:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _unpack_streams(streams: object) -> tuple[Any, Any]:
    """兼容 mcp 1.x 的 (read, write, get_session_id) 与 2.x 的 (read, write)。"""
    if not isinstance(streams, tuple) or len(streams) < 2:
        raise RuntimeError("MCP transport 返回了无法识别的流")
    return streams[0], streams[1]


def _streamable_http_client() -> Callable[..., Any]:
    client = getattr(_streamable_http, "streamablehttp_client", None)
    if client is None:
        client = getattr(_streamable_http, "streamable_http_client")
    return client


@asynccontextmanager
async def _open_transport(
    config: McpServerConfig, headers: dict[str, str]
) -> AsyncIterator[tuple[Any, Any]]:
    """按配置打开一次传输，退出上下文即关闭；绝不复用跨调用的会话。"""
    if config.transport == "stdio":
        params = StdioServerParameters(
            command=config.command or sys.executable,
            args=list(config.args),
            env=_child_env(config),
            cwd=_resolve_cwd(config.cwd),
        )
        async with stdio_client(params) as streams:
            yield _unpack_streams(streams)
        return

    if config.transport == "streamable_http":
        client_fn = _streamable_http_client()
        kwargs: dict[str, Any] = {}
        http_client = None
        if headers:
            if "headers" in inspect.signature(client_fn).parameters:
                kwargs["headers"] = headers
            else:
                # mcp 2.x 只接受预配置的 httpx 客户端，用官方工厂带上 header
                factory = getattr(_streamable_http, "create_mcp_http_client", None)
                if factory is not None:
                    http_client = factory(headers=headers)
                    kwargs["http_client"] = http_client
        try:
            async with client_fn(config.url or "", **kwargs) as streams:
                yield _unpack_streams(streams)
        finally:
            if http_client is not None:
                await http_client.aclose()
        return

    async with sse_client(config.url or "", headers=headers or None) as streams:
        yield _unpack_streams(streams)


class McpHost:
    """配置驱动的 MCP Server 宿主：发现工具、按名分发、失败可见降级。"""

    def __init__(
        self,
        servers: list[McpServerConfig] | None = None,
        *,
        loader: Callable[[], list[McpServerConfig]] | None = None,
    ) -> None:
        self._servers = servers
        self._loader = loader or load_mcp_servers
        self._tools: dict[str, McpToolInfo] = {}
        self._status: dict[str, ServerStatus] = {}
        self._started = False

    async def start(self) -> None:
        """并发发现所有启用 Server；幂等，任何单点失败都不抛出。"""
        if self._started:
            return
        self._started = True

        configs = self._configs()
        self._status = {config.id: ServerStatus(config.id, config.enabled) for config in configs}
        enabled = [config for config in configs if config.enabled]
        if not enabled:
            return

        results = await asyncio.gather(
            *(self._discover(config) for config in enabled), return_exceptions=True
        )
        for config, result in zip(enabled, results, strict=True):
            if isinstance(result, BaseException):
                # _discover 内部已兜底；这里是 gather 的最后一道防线
                self._mark_failed(config, result)
                continue
            if result is None:
                continue
            self._status[config.id].available = True
            for tool in result:
                self._register(config, tool)

        for info in self._tools.values():
            self._status[info.server_id].tool_count += 1

    async def refresh(self) -> None:
        """清空发现快照后重新发现，供 CLI 或后续管理端使用。"""
        self._tools.clear()
        self._status.clear()
        self._started = False
        await self.start()

    async def call(self, exposed_name: str, args: dict[str, Any]) -> McpCallResult:
        """按暴露名调用远端工具；任何异常都转成可朗读的降级结果。"""
        started = perf_counter()
        info = self._tools.get(exposed_name)
        if info is None:
            return McpCallResult(
                False,
                _UNKNOWN_TOOL_TEXT,
                int((perf_counter() - started) * 1000),
                "unknown_tool",
            )

        config = self._config_by_id(info.server_id)
        if config is None:
            return McpCallResult(
                False,
                _CALL_FAILED_TEXT,
                int((perf_counter() - started) * 1000),
                "server_not_configured",
                info.server_id,
            )

        try:
            text = await asyncio.wait_for(
                self._call_remote(config, info.remote_name, args),
                timeout=config.timeout_seconds,
            )
            if not text:
                raise RuntimeError("MCP 返回空内容")
            return McpCallResult(
                True, text, int((perf_counter() - started) * 1000), server_id=info.server_id
            )
        except Exception as exc:  # noqa: BLE001 —— 外部进程/网络异常统一降级
            logger.warning(
                "MCP 工具调用失败：server=%s tool=%s error=%s",
                info.server_id,
                info.remote_name,
                type(exc).__name__,
                extra={
                    "event": "mcp_call_failed",
                    "tool_name": info.remote_name,
                    "error": type(exc).__name__,
                },
            )
            return McpCallResult(
                False,
                _CALL_FAILED_TEXT,
                int((perf_counter() - started) * 1000),
                f"{type(exc).__name__}: {exc}"[:200],
                info.server_id,
            )

    def tools_snapshot(self) -> list[McpToolInfo]:
        """只返回已发现工具；未 start 时为空，可安全在单测/CLI 调用。"""
        return sorted(self._tools.values(), key=lambda info: (info.server_id, info.exposed_name))

    def server_status(self) -> list[dict[str, Any]]:
        """按配置顺序返回每个 Server 的状态，未启动时 available=False。"""
        statuses: list[dict[str, Any]] = []
        for config in self._configs():
            status = self._status.get(config.id) or ServerStatus(config.id, config.enabled)
            statuses.append(
                {
                    "id": config.id,
                    "enabled": config.enabled,
                    "available": status.available,
                    "tool_count": status.tool_count,
                    "error": status.error,
                }
            )
        return statuses

    def has_tool(self, exposed_name: str) -> bool:
        return exposed_name in self._tools

    def _configs(self) -> list[McpServerConfig]:
        if self._servers is None:
            try:
                self._servers = list(self._loader())
            except Exception as exc:  # noqa: BLE001 —— 加载器异常也不能阻断启动
                logger.warning(
                    "MCP 配置加载异常：%s",
                    type(exc).__name__,
                    extra={"event": "mcp_config_invalid", "reason": type(exc).__name__},
                )
                self._servers = []
        return self._servers

    def _config_by_id(self, server_id: str) -> McpServerConfig | None:
        return next((config for config in self._configs() if config.id == server_id), None)

    async def _discover(self, config: McpServerConfig) -> list[Any] | None:
        """返回远端工具列表；失败返回 None 并标记 unavailable，绝不抛出。"""
        try:
            return await asyncio.wait_for(
                self._list_remote_tools(config), timeout=config.discovery_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 —— 单 Server 失败不拖垮启动
            self._mark_failed(config, exc)
            return None

    def _mark_failed(self, config: McpServerConfig, exc: BaseException) -> None:
        status = self._status.get(config.id)
        if status is not None:
            status.available = False
            status.error = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning(
            "MCP Server 工具发现失败：server=%s error=%s",
            config.id,
            type(exc).__name__,
            extra={"event": "mcp_discovery_failed", "error": type(exc).__name__},
        )

    async def _list_remote_tools(self, config: McpServerConfig) -> list[Any]:
        headers = self._resolve_headers(config)
        async with _open_transport(config, headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
        return list(getattr(result, "tools", None) or [])

    async def _call_remote(
        self, config: McpServerConfig, remote_name: str, args: dict[str, Any]
    ) -> str:
        headers = self._resolve_headers(config)
        async with _open_transport(config, headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    remote_name,
                    args or {},
                    read_timeout_seconds=config.timeout_seconds,
                )
        if getattr(result, "isError", False):
            raise RuntimeError("MCP tool returned error")
        return _extract_text(result)

    def _resolve_headers(self, config: McpServerConfig) -> dict[str, str]:
        if config.transport == "stdio":
            return {}
        headers: dict[str, str] = {}
        for key, raw in config.headers.items():
            if not raw.startswith("env:"):
                headers[key] = raw
                continue
            env_name = raw[len("env:") :].strip()
            value = os.getenv(env_name) if env_name else None
            if not value:
                # 只记 header 名与变量名，绝不打印值
                logger.warning(
                    "MCP header 缺少环境变量已跳过：server=%s header=%s env=%s",
                    config.id,
                    key,
                    env_name,
                    extra={"event": "mcp_header_missing"},
                )
                continue
            headers[key] = value
        return headers

    def _allowed(self, config: McpServerConfig, remote_name: str) -> bool:
        if remote_name in config.deny_tools:
            return False
        return not config.allow_tools or remote_name in config.allow_tools

    def _exposed_name(self, config: McpServerConfig, remote_name: str) -> str:
        alias = config.tool_aliases.get(remote_name)
        if alias:
            candidate = alias
        else:
            prefix = config.tool_prefix if config.tool_prefix is not None else f"{config.id}__"
            candidate = f"{prefix}{remote_name}"
        cleaned = _EXPOSED_NAME_PATTERN.sub("_", candidate)[:64]
        return cleaned or f"{config.id}__tool"

    def _register(self, config: McpServerConfig, tool: Any) -> None:
        remote_name = str(getattr(tool, "name", "") or "")
        if not remote_name or not self._allowed(config, remote_name):
            return
        exposed_name = self._exposed_name(config, remote_name)
        if exposed_name in self._tools:
            existing = self._tools[exposed_name]
            logger.warning(
                "MCP 工具暴露名冲突已跳过：%s（%s 与 %s）",
                exposed_name,
                existing.server_id,
                config.id,
                extra={"event": "mcp_tool_name_conflict", "tool_name": exposed_name},
            )
            return
        schema = getattr(tool, "inputSchema", None)
        self._tools[exposed_name] = McpToolInfo(
            server_id=config.id,
            remote_name=remote_name,
            exposed_name=exposed_name,
            description=str(getattr(tool, "description", None) or ""),
            input_schema=dict(schema) if isinstance(schema, dict) else {},
        )


_host: McpHost | None = None


def get_mcp_host() -> McpHost:
    """进程级单例；懒创建，未 start 时工具快照为空。"""
    global _host
    if _host is None:
        config_path = get_settings().mcp_config_path
        _host = McpHost(loader=lambda: load_mcp_servers(config_path))
    return _host


def reset_mcp_host(host: McpHost | None = None) -> None:
    """测试注入/清理入口。"""
    global _host
    _host = host


async def _cli() -> None:
    settings = get_settings()
    if not settings.mcp_enabled:
        print("MCP 已停用（MCP_ENABLED=false），不会发现任何外部工具。")
        return

    host = get_mcp_host()
    await host.start()

    config_path = settings.mcp_config_path or str(DEFAULT_CONFIG_PATH)
    print(f"MCP 配置文件：{config_path}")
    print("Server 状态：")
    for status in host.server_status():
        enabled = "是" if status["enabled"] else "否"
        available = "是" if status["available"] else "否"
        error = status["error"] or "-"
        print(
            f"  - {status['id']}：启用={enabled} 可用={available} "
            f"工具数={status['tool_count']} 错误={error}"
        )

    tools = host.tools_snapshot()
    if not tools:
        print("工具：无（配置为空、全部停用或发现失败）")
        return
    print("工具：")
    for info in tools:
        print(f"  - {info.server_id}：{info.remote_name} -> {info.exposed_name}")


if __name__ == "__main__":
    asyncio.run(_cli())
