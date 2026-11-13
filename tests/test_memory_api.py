"""段 3：长期记忆只读 API（对话历史可视化）。

契约（方案 §四）：GET /api/v1/elders/{elder_id}/memories?page=&visibility=&category=
- 管理员全量；医生限绑定长者；护士限楼栋长者（403 elder_not_visible）；
- 家属限有效绑定长者，未绑定按不存在处理（404，不暴露存在性）；
- 默认只返回 status=active；响应不含 embedding 向量。
"""

from datetime import UTC, datetime, timedelta

from app.models.enums import MemoryStatus, MemoryVisibility

MEM_URL = "/api/v1/elders/{elder_id}/memories"

# created_at 是 server_default 事务时间戳（同事务全相同），seed 用显式时间戳保证排序确定
_BASE_TIME = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)


def _chunk(
    elder_id,
    content,
    *,
    visibility="private",
    status="active",
    category="other",
    created_at=None,
):
    from app.models import MemoryChunk

    return MemoryChunk(
        elder_id=elder_id,
        content=content,
        embedding=[0.0, 0.0],
        category=category,
        visibility=visibility,
        status=status,
        created_at=created_at,
    )


async def _seed_chunks(db_session, seed):
    elder = seed["elders"]["single"]
    other = seed["elders"]["other"]
    db_session.add_all(
        [
            _chunk(elder.elder_id, "偏好清淡饮食", created_at=_BASE_TIME),
            _chunk(
                elder.elder_id,
                "摔倒史",
                visibility=MemoryVisibility.CARE_TEAM,
                created_at=_BASE_TIME + timedelta(minutes=1),
            ),
            _chunk(elder.elder_id, "旧偏好", status=MemoryStatus.SUPERSEDED, created_at=_BASE_TIME),
            _chunk(elder.elder_id, "已删除", status=MemoryStatus.DELETED, created_at=_BASE_TIME),
            _chunk(other.elder_id, "他楼记忆", created_at=_BASE_TIME),
        ]
    )
    await db_session.flush()
    return elder, other


async def test_admin_reads_any_elder(db_session, seed, api, auth_headers_factory):
    elder, other = await _seed_chunks(db_session, seed)
    headers = await auth_headers_factory("admin")

    resp = await api.get(MEM_URL.format(elder_id=elder.elder_id), headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2  # superseded/deleted 默认不可见
    contents = [it["content"] for it in body["items"]]
    assert contents == ["摔倒史", "偏好清淡饮食"]  # created_at 倒序
    assert all("embedding" not in it for it in body["items"])

    resp = await api.get(MEM_URL.format(elder_id=other.elder_id), headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


async def test_doctor_scope_by_assignment(db_session, seed, api, auth_headers_factory):
    from app.models import DoctorElderAssignment

    elder, other = await _seed_chunks(db_session, seed)
    db_session.add(
        DoctorElderAssignment(
            account_id=seed["accounts"]["doctor"].account_id,
            elder_id=elder.elder_id,
        )
    )
    await db_session.flush()
    headers = await auth_headers_factory("doctor")

    resp = await api.get(MEM_URL.format(elder_id=elder.elder_id), headers=headers)
    assert resp.status_code == 200

    resp = await api.get(MEM_URL.format(elder_id=other.elder_id), headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "elder_not_visible"


async def test_nurse_scope_by_building(db_session, seed, api, auth_headers_factory):
    from app.models import BuildingAssignment

    elder, other = await _seed_chunks(db_session, seed)
    db_session.add(
        BuildingAssignment(
            account_id=seed["accounts"]["nurse_a"].account_id,
            building_code="A",
        )
    )
    await db_session.flush()
    headers = await auth_headers_factory("nurse_a")

    resp = await api.get(MEM_URL.format(elder_id=elder.elder_id), headers=headers)
    assert resp.status_code == 200  # A 楼长者

    resp = await api.get(MEM_URL.format(elder_id=other.elder_id), headers=headers)
    assert resp.status_code == 403  # B 楼


async def test_family_bound_only(db_session, seed, api, auth_headers_factory):
    from app.models import FamilyElderLink

    elder, other = await _seed_chunks(db_session, seed)
    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=elder.elder_id,
        )
    )
    await db_session.flush()
    headers = await auth_headers_factory("family")

    resp = await api.get(MEM_URL.format(elder_id=elder.elder_id), headers=headers)
    assert resp.status_code == 200
    # 家属可读绑定长者全部 active 记忆（含 care_team）
    body = resp.json()
    assert body["total"] == 2
    assert {it["visibility"] for it in body["items"]} == {"private", "care_team"}

    # 未绑定长者按不存在处理，不暴露存在性
    resp = await api.get(MEM_URL.format(elder_id=other.elder_id), headers=headers)
    assert resp.status_code == 404


async def test_visibility_filter(db_session, seed, api, auth_headers_factory):
    elder, _ = await _seed_chunks(db_session, seed)
    headers = await auth_headers_factory("admin")

    resp = await api.get(
        MEM_URL.format(elder_id=elder.elder_id),
        params={"visibility": MemoryVisibility.CARE_TEAM},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["content"] == "摔倒史"
    assert body["items"][0]["visibility"] == "care_team"

    resp = await api.get(
        MEM_URL.format(elder_id=elder.elder_id),
        params={"visibility": "no-such"},
        headers=headers,
    )
    assert resp.status_code == 422  # 枚举校验拒绝


async def test_pagination(db_session, seed, api, auth_headers_factory):
    elder = seed["elders"]["single"]
    for i in range(5):
        db_session.add(
            _chunk(elder.elder_id, f"记忆{i}", created_at=_BASE_TIME + timedelta(minutes=i))
        )
    await db_session.flush()
    headers = await auth_headers_factory("admin")

    resp = await api.get(
        MEM_URL.format(elder_id=elder.elder_id), params={"page": 1, "page_size": 3},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 3

    resp = await api.get(
        MEM_URL.format(elder_id=elder.elder_id), params={"page": 2, "page_size": 3},
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2


async def test_elder_not_found(db_session, seed, api, auth_headers_factory):
    import uuid as _uuid

    headers = await auth_headers_factory("admin")
    resp = await api.get(MEM_URL.format(elder_id=_uuid.uuid4()), headers=headers)
    assert resp.status_code == 404
