"""非流式 complete_chat 测试：MockTransport 校验解析、参数透传与非 200 降级。"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.integrations import llm


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "llm_api_key": "test-key",
        "llm_base_url": "http://llm.local/v1",
        "llm_model": "test-model",
        "llm_reasoning_effort": "low",
        "llm_timeout_seconds": 5,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_complete_chat_parses_content_and_passes_flags(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "摘要正文"}}]})

    result = await llm.complete_chat(
        [{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
        max_tokens=200,
    )
    assert result == "摘要正文"
    assert seen["stream"] is False
    assert seen["reasoning_effort"] == "low"
    assert seen["max_tokens"] == 200


async def test_complete_chat_non_200_raises(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "x"}))
    with pytest.raises(llm.LlmUnavailable):
        await llm.complete_chat([{"role": "user", "content": "hi"}], transport=transport)


async def test_complete_chat_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(llm_api_key=None))
    with pytest.raises(llm.LlmUnavailable):
        await llm.complete_chat([{"role": "user", "content": "hi"}])
