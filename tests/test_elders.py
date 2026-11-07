"""长者档案 CRUD 与范围查询测试（Day2 文档 6.4 test_elders.py 行）。

写操作均为「HTTP 断言 + 回查数据库」双验证。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from app.models import DoctorElderAssignment, Elder
from tests.conftest import login


async def _create_elder(api, seed, *, name="新人", room_key=None, bed_no=None,
                        persona_key="on", token=None):
    room = seed["rooms"][room_key] if room_key else None
    payload: dict = {
        "name": name,
        "persona_id": str(seed["personas"][persona_key].persona_id),
    }
    if room_key:
        payload["room_id"] = str(room.room_id)
        payload["bed_no"] = bed_no
    return await api.post("/api/v1/elders", json=payload, headers=token)


async def _elder_count(db_session) -> int:
    return (
        await db_session.execute(select(func.count()).select_from(Elder))
    ).scalar_one()


async def test_admin_create_elder_201_and_db_check(api, seed, db_session):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    # room_other 的 2 号床空闲
    resp = await _create_elder(api, seed, name="测试入住", room_key="other",
                               bed_no=2, token=token)
    assert resp.status_code == 201, resp.text
    elder_id = resp.json()["elder_id"]
    # 回查数据库
    row = (
        await db_session.execute(select(Elder).where(Elder.elder_id == elder_id))
    ).scalar_one()
    assert row.name == "测试入住"
    assert str(row.room_id) == str(seed["rooms"]["other"].room_id)
    assert row.bed_no == 2


async def test_admin_create_elder_bed_conflict_409(api, seed, db_session):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await _create_elder(api, seed, room_key="double", bed_no=1, token=token)
    assert resp.status_code == 409
    # 回查：数量不变（4 位种子长者）
    assert await _elder_count(db_session) == 4


async def test_admin_create_elder_bed_over_capacity_422(api, seed):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await _create_elder(api, seed, room_key="double", bed_no=3, token=token)
    assert resp.status_code == 422


async def test_admin_create_elder_inactive_persona_422(api, seed):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await _create_elder(api, seed, persona_key="off", token=token)
    assert resp.status_code == 422


async def test_admin_create_elder_room_not_found_422(api, seed):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    import uuid

    resp = await api.post(
        "/api/v1/elders",
        json={
            "name": "幽灵房间",
            "persona_id": str(seed["personas"]["on"].persona_id),
            "room_id": str(uuid.uuid4()),
            "bed_no": 1,
        },
        headers=token,
    )
    assert resp.status_code == 422


async def test_nurse_create_elder_403(api, seed, db_session):
    token = {"Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"}
    resp = await _create_elder(api, seed, token=token)
    assert resp.status_code == 403
    assert await _elder_count(db_session) == 4


async def test_admin_update_elder_200_and_db_check(api, seed, db_session):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    elder = seed["elders"]["single"]
    # 搬到 room_other 的 2 号床（空闲）
    resp = await api.put(
        f"/api/v1/elders/{elder.elder_id}",
        json={"name": "王改名", "room_id": str(seed["rooms"]["other"].room_id),
              "bed_no": 2},
        headers=token,
    )
    assert resp.status_code == 200, resp.text
    # 回查：姓名与床位均已变更
    row = (
        await db_session.execute(select(Elder).where(Elder.elder_id == elder.elder_id))
    ).scalar_one()
    assert row.name == "王改名"
    assert str(row.room_id) == str(seed["rooms"]["other"].room_id)
    assert row.bed_no == 2


async def test_update_elder_bed_conflict_409(api, seed):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    # elder_d2 想搬进 double 床 1（已被 elder_d1 占用）
    resp = await api.put(
        f"/api/v1/elders/{seed['elders']['d2'].elder_id}",
        json={"room_id": str(seed["rooms"]["double"].room_id), "bed_no": 1},
        headers=token,
    )
    assert resp.status_code == 409


async def test_update_elder_room_bed_paired_422(api, seed):
    token = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await api.put(
        f"/api/v1/elders/{seed['elders']['d1'].elder_id}",
        json={"bed_no": 2},  # 只给 bed_no 不给 room_id
        headers=token,
    )
    assert resp.status_code == 422


async def test_family_get_elder_out_of_scope_404(api, seed):
    """阶段一变更：家属放开为关联只读；无关联长者返回 404（隐藏存在性）。"""
    token = {"Authorization": f"Bearer {await login(api, 'family', seed['password'])}"}
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['single'].elder_id}", headers=token
    )
    assert resp.status_code == 404


async def test_nurse_scope_query_in_and_out_of_building(api, seed):
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    nurse = {"Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"}
    # 未授权楼栋前：A 楼长者也 404
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['single'].elder_id}", headers=nurse
    )
    assert resp.status_code == 404

    assign = await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id),
              "building_code": "A"},
        headers=admin,
    )
    assert assign.status_code == 201, assign.text

    # A 楼可见；B 楼 404 且响应不含长者姓名（存在性隐藏）
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['single'].elder_id}", headers=nurse
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "王单间"
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['other'].elder_id}", headers=nurse
    )
    assert resp.status_code == 404
    assert "钱他楼" not in resp.text


async def test_doctor_scope_query_assignment_and_expiry(api, seed, db_session):
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    doctor = {"Authorization": f"Bearer {await login(api, 'doctor', seed['password'])}"}
    # 未关联 → 404
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['d1'].elder_id}", headers=doctor
    )
    assert resp.status_code == 404

    # 授权后 → 200（回查关联行）
    assign = await api.post(
        "/api/v1/admin/assignments/doctors",
        json={"account_id": str(seed["accounts"]["doctor"].account_id),
              "elder_id": str(seed["elders"]["d1"].elder_id)},
        headers=admin,
    )
    assert assign.status_code == 201, assign.text
    row = (
        await db_session.execute(
            select(DoctorElderAssignment).where(
                DoctorElderAssignment.account_id
                == seed["accounts"]["doctor"].account_id
            )
        )
    ).scalar_one()
    assert str(row.elder_id) == str(seed["elders"]["d1"].elder_id)

    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['d1'].elder_id}", headers=doctor
    )
    assert resp.status_code == 200

    # 关联过期 → 404
    await db_session.execute(
        sa_update(DoctorElderAssignment)
        .where(DoctorElderAssignment.assignment_id == row.assignment_id)
        .values(valid_until=datetime.now(UTC) - timedelta(days=1))
    )
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['d1'].elder_id}", headers=doctor
    )
    assert resp.status_code == 404


async def test_expired_valid_until_rejected_422(api, seed):
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    resp = await api.post(
        "/api/v1/admin/assignments/doctors",
        json={
            "account_id": str(seed["accounts"]["doctor"].account_id),
            "elder_id": str(seed["elders"]["d2"].elder_id),
            "valid_until": "2020-01-01T00:00:00Z",
        },
        headers=admin,
    )
    assert resp.status_code == 422


async def test_nurse_revoke_building_scope_hidden(api, seed):
    """吊销楼栋授权（软删）后护工访问回到 404。"""
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    nurse = {"Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"}
    assign = await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id),
              "building_code": "A"},
        headers=admin,
    )
    assert assign.status_code == 201
    delete = await api.delete(
        f"/api/v1/admin/assignments/buildings/{assign.json()['assignment_id']}",
        headers=admin,
    )
    assert delete.status_code == 204
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['single'].elder_id}", headers=nurse
    )
    assert resp.status_code == 404
