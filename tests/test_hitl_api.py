"""Day6.3 HITL 接口：仅 admin 可查看/处理待确认事件。"""

from __future__ import annotations

import pytest

from app.services.hitl import hitl_queue
from tests.conftest import login


@pytest.fixture(autouse=True)
def _clear_hitl():
    hitl_queue.clear()
    yield
    hitl_queue.clear()


async def test_hitl_requires_admin(api, seed):
    anon = await api.get("/api/v1/admin/hitl/pending")
    assert anon.status_code == 401

    family = await login(api, "family", seed["password"])
    resp = await api.get(
        "/api/v1/admin/hitl/pending", headers={"Authorization": f"Bearer {family}"}
    )
    assert resp.status_code == 403


async def test_admin_lists_and_resolves_pending(api, seed):
    item = hitl_queue.enqueue(
        kind="sensitive", summary="跌倒", elder_id="elder-1", detail="示例"
    )
    admin = await login(api, "admin", seed["password"])
    headers = {"Authorization": f"Bearer {admin}"}

    listed = await api.get("/api/v1/admin/hitl/pending", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [row["id"] for row in body] == [item.id]
    assert body[0]["kind"] == "sensitive"

    resolved = await api.post(
        f"/api/v1/admin/hitl/{item.id}/resolve",
        json={"note": "已联系家属"},
        headers=headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_by"] == "admin"

    again = await api.post(
        f"/api/v1/admin/hitl/{item.id}/resolve", json={}, headers=headers
    )
    assert again.status_code == 404

    empty = await api.get("/api/v1/admin/hitl/pending", headers=headers)
    assert empty.json() == []
