"""文字通道测试（POST /voice/text）：设备鉴权、身份级联、LLM 与降级。

背景：ASR 容器挂掉时前端降级到文字输入，长者仍应拿到回复（验收项 5）。
"""

import pytest
from sqlalchemy import select

from app.models import ConversationMessage
from app.services.resilience import reset_circuit_breakers
from tests.conftest import login


@pytest.fixture(autouse=True)
def _reset_breakers():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


async def _device_token(api, seed, device_key: str = "single") -> str:
    """管理员签发注册凭证 → 设备注册换取 access token（完整真实链路）。"""
    admin = await login(api, "admin", seed["password"])
    device = seed["devices"][device_key]
    issued = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert issued.status_code == 200, issued.text
    registered = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": issued.json()["registration_token"],
        },
    )
    assert registered.status_code == 200, registered.text
    return registered.json()["access_token"]


async def test_text_chat_requires_device_token(api, seed):
    """未带 Token → 401；人 JWT 不能冒用设备通道 → 403。"""
    anon = await api.post("/api/v1/voice/text", json={"text": "你好"})
    assert anon.status_code == 401

    human = await login(api, "nurse_a", seed["password"])
    resp = await api.post(
        "/api/v1/voice/text",
        json={"text": "你好"},
        headers={"Authorization": f"Bearer {human}"},
    )
    assert resp.status_code == 403


async def test_text_chat_single_room_cascades_elder(api, seed):
    """单人间：elder_id 由设备级联锁定；LLM 未配置时模板兜底并标 degraded。"""
    token = await _device_token(api, seed, "single")
    resp = await api.post(
        "/api/v1/voice/text",
        json={"text": "你陪我聊聊天吧"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_id"] == str(seed["elders"]["single"].elder_id)
    assert body["reply"] == "你刚才说的是：你陪我聊聊天吧"
    assert body["degraded"] is True


async def test_text_chat_double_room_is_anonymous(api, seed):
    """双人间文字无法确认是哪位长者：不猜身份，走匿名线程，仍正常回复。"""
    token = await _device_token(api, seed, "double")
    resp = await api.post(
        "/api/v1/voice/text",
        json={"text": "我有点想家"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_id"] is None
    assert "我有点想家" in body["reply"]


async def test_text_chat_rejects_blank(api, seed):
    token = await _device_token(api, seed, "single")
    resp = await api.post(
        "/api/v1/voice/text",
        json={"text": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_text_chat_uses_llm_and_persists_turn(api, seed, monkeypatch, db_session):
    """LLM 已配置：流式增量拼成整句返回，并把这一轮写入会话线程。"""

    async def fake_stream(messages):
        assert messages[-1]["content"] == "你陪我聊聊天吧"
        yield "今天"
        yield "天气不错。"

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", fake_stream)

    token = await _device_token(api, seed, "single")
    resp = await api.post(
        "/api/v1/voice/text",
        json={"text": "你陪我聊聊天吧"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "今天天气不错。"
    assert body["degraded"] is False

    rows = (
        await db_session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.content.in_(["你陪我聊聊天吧", "今天天气不错。"]))
            .order_by(ConversationMessage.seq)
        )
    ).scalars().all()
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].content == "你陪我聊聊天吧"
    assert rows[1].content == "今天天气不错。"


async def test_multi_intent_preempts_clarification(api, seed, monkeypatch):
    """组合请求走主控 Agent，不能被「低置信→反问」截断（大纲 6.3 验收项）。"""
    from app.agent.companion import AgentResult

    async def fake_agent(text, **kwargs):
        assert "放戏" in text
        return AgentResult(
            text="先记着吃药，再给您放戏，最后告诉您儿子。",
            tool_names=["medication_query", "media_play", "notify_family"],
            steps=2,
        )

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.agent_respond", fake_agent)

    token = await _device_token(api, seed, "single")
    resp = await api.post(
        "/api/v1/voice/text",
        json={"text": "吃完药放戏再告诉我儿子"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "先记着吃药，再给您放戏，最后告诉您儿子。"
    assert body["reply"] != "您是想聊聊天，还是想听段戏或者查查天气？"


async def test_text_chat_llm_failure_falls_back_to_template(api, seed, monkeypatch):
    """LLM 抛错：不向上抛 5xx，改模板回复并标 degraded。"""

    async def broken_stream(messages):
        raise RuntimeError("provider down")
        yield ""  # pragma: no cover —— 保持 async generator 语义

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", broken_stream)

    token = await _device_token(api, seed, "single")
    resp = await api.post(
        "/api/v1/voice/text",
        json={"text": "晚安"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "你刚才说的是：晚安"
    assert body["degraded"] is True
