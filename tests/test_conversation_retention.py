"""段 4：会话数据治理（保留期归档 + 审计）。

契约（方案 §四）：POST /api/v1/admin/conversations/maintenance {dry_run}
- 归档 → 逻辑下架 → 物理删除三步（同事务原子完成）；
- ACTIVE 会话永不清理；retention_days=0 时仅清理 expires_at 已到期的会话；
- dry-run 只出报表、无副作用；审计只记元数据，不记对话正文。
"""

from datetime import UTC, datetime, timedelta

from app.models import ConversationAuditLog, ConversationMessage
from app.services import conversation_retention as retention
from app.services import conversation_store as store

_BASE = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


async def _make_conversation(
    db_session,
    seed,
    *,
    device_key="single",
    elder_key="single",
    status="ended",
    age_days=100,
    expires_at=None,
    turns=2,
):
    """造一条已结束、指定年龄的会话（含 turns 轮消息）。"""
    device = seed["devices"][device_key]
    elder = seed["elders"][elder_key] if elder_key else None
    conv = await store.get_or_create_active_conversation(
        db_session,
        device_id=device.device_id,
        elder_id=elder.elder_id if elder else None,
        identity_source="device_room" if elder else "anonymous",
    )
    for i in range(turns):
        await store.record_turn(
            db_session, thread_id=conv.thread_id, user_text=f"问{i}", assistant_text=f"答{i}"
        )
    conv.status = status
    conv.started_at = _BASE - timedelta(days=age_days)
    conv.ended_at = _BASE - timedelta(days=age_days) + timedelta(hours=1)
    conv.expires_at = expires_at
    await db_session.flush()
    return conv


async def test_expired_ended_conversation_archived(db_session, seed):
    """到期 ENDED 会话：归档表有完整拷贝，原行与消息物理删除，审计留痕。"""
    conv = await _make_conversation(db_session, seed, age_days=100)

    report = await retention.run_maintenance(
        db_session, dry_run=False, now=_BASE, retention_days=90
    )

    assert report["dry_run"] is False
    assert report["matched"] == 1
    assert report["archived"] == 1
    item = report["items"][0]
    assert item["thread_id"] == str(conv.thread_id)
    assert item["message_count"] == 4  # 2 轮 = 4 条消息

    # 原行与消息已删除
    assert await db_session.get(store.Conversation, conv.thread_id) is None
    rows = (
        await db_session.execute(
            ConversationMessage.__table__.select().where(
                ConversationMessage.thread_id == conv.thread_id
            )
        )
    ).all()
    assert rows == []

    # 归档表完整拷贝
    from app.models import ConversationArchive

    archive = (
        await db_session.execute(
            ConversationArchive.__table__.select().where(
                ConversationArchive.thread_id == conv.thread_id
            )
        )
    ).one()
    assert archive.message_count == 4
    assert [m["content"] for m in archive.messages] == ["问0", "答0", "问1", "答1"]
    assert archive.identity_source == "device_room"

    # 审计留痕（只记元数据）
    audits = (
        (await db_session.execute(ConversationAuditLog.__table__.select())).mappings().all()
    )
    assert any(a["action"] == "conversation_maintenance" for a in audits)


async def test_active_conversation_never_archived(db_session, seed):
    """进行中会话即使超过保留期也永不清理。"""
    conv = await _make_conversation(db_session, seed, status="active", age_days=200)

    report = await retention.run_maintenance(
        db_session, dry_run=False, now=_BASE, retention_days=90
    )

    assert report["matched"] == 0
    assert report["archived"] == 0
    assert await db_session.get(store.Conversation, conv.thread_id) is not None


async def test_not_expired_untouched(db_session, seed):
    """未到期 ENDED 会话不动。"""
    conv = await _make_conversation(db_session, seed, age_days=10)

    report = await retention.run_maintenance(
        db_session, dry_run=False, now=_BASE, retention_days=90
    )

    assert report["matched"] == 0
    assert await db_session.get(store.Conversation, conv.thread_id) is not None


async def test_expires_at_independent_of_retention_days(db_session, seed):
    """retention_days=0 时，手动标记 expires_at 已到期的会话仍被清理；未到期不动。"""
    expired = await _make_conversation(
        db_session, seed, expires_at=_BASE - timedelta(days=1)
    )
    future = await _make_conversation(
        db_session, seed, device_key="double", elder_key="d1", expires_at=_BASE + timedelta(days=1)
    )

    report = await retention.run_maintenance(
        db_session, dry_run=False, now=_BASE, retention_days=0
    )

    assert report["matched"] == 1
    assert await db_session.get(store.Conversation, expired.thread_id) is None
    assert await db_session.get(store.Conversation, future.thread_id) is not None


async def test_dry_run_no_side_effects(db_session, seed):
    """dry-run 只出报表：DB 无归档、无删除，但审计留痕。"""
    conv = await _make_conversation(db_session, seed, age_days=100)

    report = await retention.run_maintenance(
        db_session, dry_run=True, now=_BASE, retention_days=90
    )

    assert report["dry_run"] is True
    assert report["matched"] == 1
    assert report["archived"] == 0
    assert await db_session.get(store.Conversation, conv.thread_id) is not None

    audits = (
        (await db_session.execute(ConversationAuditLog.__table__.select())).mappings().all()
    )
    dry_audits = [a for a in audits if a["action"] == "conversation_maintenance"]
    assert len(dry_audits) == 1
    assert dry_audits[0]["detail"]["dry_run"] is True


async def test_abandoned_also_archived(db_session, seed):
    """ABANDONED 会话同样到期清理。"""
    conv = await _make_conversation(db_session, seed, status="abandoned", age_days=100)

    report = await retention.run_maintenance(
        db_session, dry_run=False, now=_BASE, retention_days=90
    )
    assert report["archived"] == 1
    assert await db_session.get(store.Conversation, conv.thread_id) is None


# ---- API 层 ----

ADMIN_URL = "/api/v1/admin/conversations/maintenance"
AUDIT_URL = "/api/v1/admin/conversations/audit-logs"


async def test_admin_maintenance_api(db_session, seed, api, auth_headers_factory):
    # API 路径走 settings（conversation_retention_days 默认 0），
    # 造一条 expires_at 已到期的会话验证 expires_at 清理链路
    conv = await _make_conversation(
        db_session, seed, age_days=100, expires_at=_BASE - timedelta(days=1)
    )
    headers = await auth_headers_factory("admin")

    # 默认 dry_run=True：只出报表
    resp = await api.post(ADMIN_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["matched"] == 1
    assert await db_session.get(store.Conversation, conv.thread_id) is not None

    # 非 dry-run：执行归档
    resp = await api.post(ADMIN_URL, json={"dry_run": False}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["archived"] == 1
    assert await db_session.get(store.Conversation, conv.thread_id) is None


async def test_maintenance_requires_admin(db_session, seed, api, auth_headers_factory):
    headers = await auth_headers_factory("nurse_a")
    resp = await api.post(ADMIN_URL, headers=headers)
    assert resp.status_code == 403

    resp = await api.get(AUDIT_URL, headers=headers)
    assert resp.status_code == 403


async def test_audit_logs_query(db_session, seed, api, auth_headers_factory):
    await _make_conversation(db_session, seed, age_days=100)
    await retention.run_maintenance(
        db_session, dry_run=False, now=_BASE, retention_days=90
    )
    headers = await auth_headers_factory("admin")

    resp = await api.get(AUDIT_URL, headers=headers)
    assert resp.status_code == 200
    actions = {it["action"] for it in resp.json()["items"]}
    assert "conversation_maintenance" in actions

    resp = await api.get(
        AUDIT_URL, params={"target_type": "no-such"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_read_endpoints_write_audit(db_session, seed, api, auth_headers_factory):
    """查看会话消息 / 记忆查询写审计（只记元数据）。"""
    conv = await _make_conversation(db_session, seed, status="active", age_days=0, turns=1)
    elder = seed["elders"]["single"]
    await db_session.flush()

    admin_headers = await auth_headers_factory("admin")
    await api.get(f"/api/v1/elders/{elder.elder_id}/memories", headers=admin_headers)

    # 设备端查看消息（复用段 2 的 token 辅助逻辑）
    from app.auth.device_token import issue_access_token
    from app.services.voice import voice_token_store

    device = seed["devices"]["single"]
    issued = await issue_access_token(db_session, device.device_id)
    await db_session.commit()
    voice_token, _ = voice_token_store.issue(device.device_id, elder.elder_id, score=0.99)
    device_headers = {
        "Authorization": f"Bearer {issued.access_token}",
        "X-Voice-Token": voice_token,
    }
    resp = await api.get(
        f"/api/v1/conversations/{conv.thread_id}/messages", headers=device_headers
    )
    assert resp.status_code == 200

    resp = await api.get(AUDIT_URL, headers=admin_headers)
    assert resp.status_code == 200
    actions = {it["action"] for it in resp.json()["items"]}
    assert "memory_list_read" in actions
    assert "conversation_messages_read" in actions
