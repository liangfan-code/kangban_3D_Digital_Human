"""Day6.4 MCP 配置加载：缺失、损坏与非法条目都必须安静降级。"""

from __future__ import annotations

import json

from app.agent.mcp_client import load_mcp_servers


def _write_config(tmp_path, payload: object):
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_file_returns_empty_list(tmp_path):
    assert load_mcp_servers(tmp_path / "not-exists.json") == []


def test_invalid_json_returns_empty_list(tmp_path):
    path = tmp_path / "mcp_servers.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert load_mcp_servers(path) == []


def test_top_level_not_object_returns_empty_list(tmp_path):
    path = _write_config(tmp_path, [{"id": "weather"}])
    assert load_mcp_servers(path) == []


def test_env_var_path_takes_effect(tmp_path, monkeypatch):
    path = _write_config(
        tmp_path,
        {"version": 1, "servers": [{"id": "weather", "args": ["mcp-servers/weather/server.py"]}]},
    )
    monkeypatch.setenv("MCP_CONFIG_PATH", str(path))
    servers = load_mcp_servers()
    assert [server.id for server in servers] == ["weather"]
    monkeypatch.delenv("MCP_CONFIG_PATH", raising=False)


def test_invalid_and_duplicate_ids_are_skipped(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "version": 1,
            "servers": [
                {"id": "Bad ID", "args": ["server.py"]},
                {"id": "weather", "args": ["server.py"]},
                {"id": "weather", "args": ["other.py"]},
                {"id": "bad-transport", "transport": "websocket", "args": ["server.py"]},
                {"id": "ok-server", "args": ["server.py"]},
            ],
        },
    )
    servers = load_mcp_servers(path)
    assert [server.id for server in servers] == ["weather", "ok-server"]
    assert servers[0].args == ("server.py",)


def test_stdio_requires_args_and_http_requires_url(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "version": 1,
            "servers": [
                {"id": "no-args"},
                {"id": "stdio-with-url", "args": ["server.py"], "url": "https://example.com/mcp"},
                {"id": "http-no-url", "transport": "streamable_http"},
                {"id": "sse-no-url", "transport": "sse"},
                {"id": "valid", "args": ["server.py"]},
            ],
        },
    )
    servers = load_mcp_servers(path)
    assert [server.id for server in servers] == ["valid"]


def test_disabled_server_is_still_parsed(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "version": 1,
            "servers": [
                {
                    "id": "remote_example",
                    "enabled": False,
                    "transport": "streamable_http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "env:MCP_REMOTE_TOKEN"},
                    "tool_prefix": "remote_",
                    "allow_tools": ["search"],
                }
            ],
        },
    )
    servers = load_mcp_servers(path)
    assert len(servers) == 1
    config = servers[0]
    assert config.id == "remote_example"
    assert config.enabled is False
    assert config.transport == "streamable_http"
    assert config.url == "https://example.com/mcp"
    assert config.headers == {"Authorization": "env:MCP_REMOTE_TOKEN"}
    assert config.tool_prefix == "remote_"
    assert config.allow_tools == ("search",)


def test_weather_entry_defaults_and_alias(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "version": 1,
            "servers": [
                {
                    "id": "weather",
                    "transport": "stdio",
                    "args": ["mcp-servers/weather/server.py"],
                    "cwd": ".",
                    "env": {"MCP_WEATHER_PROVIDER": "open_meteo"},
                    "tool_aliases": {"get_weather": "weather_get"},
                    "allow_tools": ["get_weather"],
                    "timeout_seconds": 8,
                    "discovery_timeout_seconds": 10,
                }
            ],
        },
    )
    config = load_mcp_servers(path)[0]
    assert config.enabled is True
    assert config.command is None
    assert config.cwd == "."
    assert config.env == {"MCP_WEATHER_PROVIDER": "open_meteo"}
    assert config.tool_aliases == {"get_weather": "weather_get"}
    assert config.allow_tools == ("get_weather",)
    assert config.timeout_seconds == 8.0
    assert config.discovery_timeout_seconds == 10.0
