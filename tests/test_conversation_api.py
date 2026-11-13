"""段 2：会话操作与历史读取 API + 声纹门禁。

契约（方案 §四）：
- 所有接口要求设备 token（Bearer）+ 声纹确认 token（X-Voice-Token）；
- 未确认/无效 → 403 detail="identity_locked"；
- 列表按 elder_id 聚合（新旧行并列），ABANDONED 与匿名线程不可见；
- 新建/收起/切回走状态机，WS 每轮自动续上最近 ACTIVE。
"""

import uuid

from app.auth.device_token import issue_access_token
from app.services import conversation_store as store
from app.services.voice import voice_token_store

BASE = "/api/v1/conversations"


async def _device_headers(api, seed, db_session, device_key="single", elder_key="single"):
    """签发设备 token + 声纹确认 token，返回 Bearer 头。"""
    device = seed["devices"][device_key]
    elder = seed["elders"][elder_key]
    issued = await issue_access_token(db_session, device.device_id)
    await db_session.commit()
    voice_token, _ = voice_token_store.issue(device.device_id, elder.elder_id, score=0.99)
    return {"Authorization": f"Bearer {issued.access_token}", "X-Voice-Token": voice_token}


async def _conv_headers(api, seed, db_session, device_key="single", elder_key=None):
    """无声纹 token 的设备头（用于门禁失败用例）。"""
    device = seed["devices"][device_key]
    issued = await issue_access_token(db_session, device.device_id)
    await db_session.commit()
    return {"Authorization": f"Bearer {issued.access_token}"}


async def test_list_requires_voice_token(api, seed, db_session):
    headers = await _conv_headers(api, seed, db_session)
    resp = await api.get(BASE, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "identity_locked"

    resp = await api.get(
        BASE, headers={**headers, "X-Voice-Token": "not-a-valid-token"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "identity_locked"


async def test_list_elder_scope_and_visibility(api, seed, db_session):
    elder = seed["elders"]["single"]
    device = seed["devices"]["single"]
    other = seed["elders"]["other"]

    mine1 = await store.get_or_create_active_conversation(
        db_session, device_id=device.device_id, elder_id=elder.elder_id,
        identity_source="device_room",
    )
    mine2 = await store.start_new_conversation(
        db_session, device_id=device.device_id, elder_id=elder.elder_id,
        identity_source="device_room",
    )
    await store.get_or_create_active_conversation(
        db_session, device_id=seed["devices"]["double"].device_id, elder_id=other.elder_id,
        identity_source="device_room",
    )
    await store.get_or_create_active_conversation(
        db_session, device_id=device.device_id, elder_id=None, identity_source="anonymous"
    )
    mine1.status = "abandoned"
    await db_session.flush()

    headers = await _device_headers(api, seed, db_session)
    resp = await api.get(BASE, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    ids = [it["thread_id"] for it in body["items"]]
    assert body["total"] == 1
    assert ids == [str(mine2.thread_id)]  # abandoned / 他人 / 匿名 均不可见
    assert body["items"][0]["is_current"] is True
    assert body["items"][0]["status"] == "active"


async def test_new_then_activate_flow(db_session, api, seed):
    """核心链路：新建 → 旧会话收起 → 切回旧会话 → WS 机制（最近 ACTIVE）落入旧会话。"""
    elder = seed["elders"]["single"]
    device = seed["devices"]["single"]
    headers = await _device_headers(api, seed, db_session)

    # 无进行中会话 → 404
    resp = await api.get(f"{BASE}/current", headers=headers)
    assert resp.status_code == 404

    # 新建第一段对话，写入两条消息
    resp = await api.post(BASE, headers=headers)
    assert resp.status_code == 200
    first_id = resp.json()["thread_id"]
    await store.record_turn(
        db_session,
        thread_id=uuid.UUID(first_id),
        user_text="旧会话提问",
        assistant_text="旧会话回答",
    )
    await db_session.commit()

    # 新建第二段对话：第一段自动收起为 ENDED
    resp = await api.post(BASE, headers=headers)
    assert resp.status_code == 200
    second_id = resp.json()["thread_id"]
    assert second_id != first_id

    resp = await api.get(BASE, headers=headers)
    statuses = {it["thread_id"]: it["status"] for it in resp.json()["items"]}
    assert statuses[first_id] == "ended"
    assert statuses[second_id] == "active"

    # GET /current 指向第二段
    resp = await api.get(f"{BASE}/current", headers=headers)
    assert resp.json()["thread_id"] == second_id

    # 切回第一段（ENDED → ACTIVE）
    resp = await api.post(f"{BASE}/{first_id}/activate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    # WS 机制验证：get_or_create_active_conversation 现在命中第一段
    conv = await store.get_or_create_active_conversation(
        db_session, device_id=device.device_id, elder_id=elder.elder_id,
        identity_source="device_room",
    )
    assert str(conv.thread_id) == first_id

    # ABANDONED 会话拒绝切回：把第二段置为 ABANDONED
    async with db_session.begin_nested():
        row = await db_session.get(
            store.Conversation, uuid.UUID(second_id)
        )
        row.status = "abandoned"
    resp = await api.post(f"{BASE}/{second_id}/activate", headers=headers)
    assert resp.status_code == 422

    # 收起当前对话 → current 404
    resp = await api.post(f"{BASE}/current/end", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ended"
    resp = await api.get(f"{BASE}/current", headers=headers)
    assert resp.status_code == 404


async def test_messages_pagination_and_cross_elder_404(api, seed, db_session):
    elder = seed["elders"]["single"]
    device = seed["devices"]["single"]
    conv = await store.get_or_create_active_conversation(
        db_session, device_id=device.device_id, elder_id=elder.elder_id,
        identity_source="device_room",
    )
    for i in range(3):
        await store.record_turn(
            db_session, thread_id=conv.thread_id, user_text=f"问{i}", assistant_text=f"答{i}"
        )
    await db_session.flush()

    headers = await _device_headers(api, seed, db_session)
    resp = await api.get(
        f"{BASE}/{conv.thread_id}/messages?limit=3", headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is True
    assert [m["content"] for m in body["items"]] == ["答1", "问2", "答2"]  # 最新 3 条、正序

    # 他人长者的 thread → 404（不泄露存在性）
    other_conv = await store.get_or_create_active_conversation(
        db_session, device_id=seed["devices"]["double"].device_id,
        elder_id=seed["elders"]["other"].elder_id, identity_source="device_room",
    )
    await db_session.flush()
    resp = await api.get(
        f"{BASE}/{other_conv.thread_id}/messages", headers=headers
    )
    assert resp.status_code == 404

    # 不存在的 thread → 404
    resp = await api.get(f"{BASE}/{uuid.uuid4()}/messages", headers=headers)
    assert resp.status_code == 404
