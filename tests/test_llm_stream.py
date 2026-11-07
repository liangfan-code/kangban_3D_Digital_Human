"""LLM 流式客户端与动作协议 prompt 测试（httpx.MockTransport，不出网、无密钥）。"""

import json

import httpx
import pytest

from app.core.config import get_settings
from app.integrations.llm import LlmUnavailable, is_configured, stream_chat
from app.services.actions import (
    ActionCatalog,
    ActionDef,
    build_action_prompt,
    get_catalog,
)


@pytest.fixture(autouse=True)
def _isolate_settings_cache(monkeypatch):
    """测试间隔离 lru_cache 的 Settings；默认保持 conftest 的未配置状态。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    get_settings.cache_clear()


def _sse_transport(chunks: list[str], status: int = 200, captured: list | None = None):
    """构造 OpenAI 兼容 SSE 响应；captured 收集请求体与头供断言。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append({"json": json.loads(request.content), "headers": dict(request.headers)})
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": c}}]}) for c in chunks
        ]
        lines.append("data: [DONE]")
        return httpx.Response(
            status,
            content=("\n\n".join(lines) + "\n\n").encode(),
            headers={"content-type": "text/event-stream"},
        )

    return httpx.MockTransport(handler)


async def _collect(messages, transport):
    return [d async for d in stream_chat(messages, transport=transport)]


async def test_stream_parses_deltas_until_done(llm_env):
    captured: list = []
    deltas = await _collect(
        [{"role": "user", "content": "你好"}],
        _sse_transport(["[act:wave]", "您好", "，", "今天精神不错。"], captured=captured),
    )
    assert "".join(deltas) == "[act:wave]您好，今天精神不错。"
    assert captured[0]["json"]["stream"] is True
    assert captured[0]["json"]["model"] == "test-model"
    assert captured[0]["headers"]["authorization"] == "Bearer test-key"
    assert captured[0]["json"]["messages"][0]["content"] == "你好"


async def test_stream_skips_malformed_sse_lines(llm_env):
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            "data: not-json\n\n"
            "data: {\"broken\": true}\n\n"
            "data: {\"choices\": [{\"delta\": {\"content\": \"你好\"}}]}\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    deltas = await _collect([{"role": "user", "content": "hi"}], httpx.MockTransport(handler))
    assert deltas == ["你好"]


async def test_stream_http_500_raises_unavailable(llm_env):
    with pytest.raises(LlmUnavailable, match="HTTP 500"):
        await _collect([{"role": "user", "content": "hi"}], _sse_transport([], status=500))


async def test_stream_network_error_raises_unavailable(llm_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(LlmUnavailable, match="ConnectError"):
        await _collect([{"role": "user", "content": "hi"}], httpx.MockTransport(handler))


async def test_stream_without_configuration_raises(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    assert is_configured() is False
    with pytest.raises(LlmUnavailable, match="未配置"):
        await _collect([{"role": "user", "content": "hi"}], _sse_transport(["x"]))


async def test_is_configured_true_when_env_present(llm_env):
    assert is_configured() is True


# ---------- build_action_prompt ----------


def _mini_catalog() -> ActionCatalog:
    return ActionCatalog(
        actions=(ActionDef(id="wave", label="挥手", usage="打招呼或告别"),),
        marker={"max_per_reply": 2, "no_repeat_consecutive": True},
    )


def test_build_action_prompt_contains_rules_and_actions():
    prompt = build_action_prompt(_mini_catalog())
    assert "[act:" in prompt
    assert "最多 2 个" in prompt
    assert "wave" in prompt and "挥手" in prompt and "打招呼或告别" in prompt
    assert "禁止" in prompt


def test_build_action_prompt_with_real_catalog():
    prompt = build_action_prompt(get_catalog())
    assert prompt
    for action_id in ("wave", "nod", "comfort"):
        assert action_id in prompt


def test_build_action_prompt_catalog_missing_returns_empty(monkeypatch):
    from app.services import actions as actions_mod

    monkeypatch.setattr(actions_mod, "get_catalog", lambda path=None: None)
    assert build_action_prompt() == ""
