"""Day8 段1：健康事件决策图 + 事件服务 + HTTP 路由集成测试。

覆盖：绿色归档入库、缺字段追问、黄色建单与护士确认、红色医生确认与转诊、
同日幂等、越权与可见范围。
"""

import pytest
from sqlalchemy import select

from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    HealthRecord,
    Workorder,
    WorkorderDisposition,
)
from app.models.enums import (
    WorkorderDispositionStatus,
    WorkorderPriority,
    WorkorderStatus,
)


@pytest.fixture
async def staff(seed, db_session, auth_headers_factory):
    """护士 A 管 A 栋、护士 B 管 B 栋、医生只关联 single 长者。"""
    db_session.add_all(
        [
            BuildingAssignment(
                account_id=seed["accounts"]["nurse_a"].account_id,
                building_code="A",
                status="active",
            ),
            BuildingAssignment(
                account_id=seed["accounts"]["nurse_b"].account_id,
                building_code="B",
                status="active",
            ),
            DoctorElderAssignment(
                account_id=seed["accounts"]["doctor"].account_id,
                elder_id=seed["elders"]["single"].elder_id,
                status="active",
            ),
        ]
    )
    await db_session.flush()
    return {
        "nurse_a": await auth_headers_factory("nurse_a"),
        "nurse_b": await auth_headers_factory("nurse_b"),
        "doctor": await auth_headers_factory("doctor"),
        "admin": await auth_headers_factory("admin"),
        "family": await auth_headers_factory("family"),
    }


async def _post(api, headers, elder_id, **payload):
    return await api.post(
        f"/api/v1/elders/{elder_id}/health-events", headers=headers, json=payload
    )


async def _workorders(db_session, elder_id):
    return (
        (
            await db_session.execute(
                select(Workorder).where(Workorder.elder_id == elder_id)
            )
        )
        .scalars()
        .all()
    )


async def test_complete_green_archives_and_records_health(
    api, seed, staff, db_session
) -> None:
    elder_id = seed["elders"]["single"].elder_id
    resp = await _post(
        api,
        staff["nurse_a"],
        elder_id,
        bp_systolic=130,
        bp_diastolic=80,
        sleep_minutes=420,
        mood="很好",
        chief_complaint="没有不舒服",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["level"] == "green"
    assert body["status"] == "archived"
    assert body["required_role"] is None
    assert body["missing_fields"] == []
    assert body["reply"]

    records = (
        (
            await db_session.execute(
                select(HealthRecord).where(HealthRecord.elder_id == elder_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(records) == 1
    assert records[0].bp_systolic == 130
    assert records[0].source == "manual"
    assert await _workorders(db_session, elder_id) == []


async def test_missing_fields_returns_next_question_then_complete(
    api, seed, staff
) -> None:
    elder_id = seed["elders"]["d1"].elder_id
    first = await _post(
        api, staff["nurse_a"], elder_id, bp_systolic=130, bp_diastolic=80
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "collecting"
    assert body["missing_fields"] == ["sleep_minutes", "mood", "chief_complaint"]
    assert "睡" in body["reply"]
    event_id = body["event_id"]

    second = await _post(
        api,
        staff["nurse_a"],
        elder_id,
        sleep_minutes=420,
        mood="很好",
        chief_complaint="没有不舒服",
    )
    assert second.status_code == 200, second.text
    assert second.json()["event_id"] == event_id
    assert second.json()["status"] == "archived"


async def test_yellow_creates_alert_and_nurse_confirms(
    api, seed, staff, db_session
) -> None:
    elder_id = seed["elders"]["d1"].elder_id
    resp = await _post(
        api,
        staff["nurse_a"],
        elder_id,
        bp_systolic=165,
        bp_diastolic=105,
        sleep_minutes=360,
        mood="一般",
        chief_complaint="没有不舒服",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["level"] == "yellow"
    assert body["status"] == "pending_confirm"
    assert body["required_role"] == "nurse"

    workorders = await _workorders(db_session, elder_id)
    assert len(workorders) == 1
    assert workorders[0].type == "health_alert"
    assert workorders[0].priority == WorkorderPriority.HIGH

    pending_a = await api.get("/api/v1/health-events/pending", headers=staff["nurse_a"])
    assert pending_a.status_code == 200, pending_a.text
    assert any(item["event_id"] == body["event_id"] for item in pending_a.json()["items"])

    pending_b = await api.get("/api/v1/health-events/pending", headers=staff["nurse_b"])
    assert pending_b.status_code == 200
    assert pending_b.json()["items"] == []

    decided = await api.post(
        f"/api/v1/health-events/{body['event_id']}/decision",
        headers=staff["nurse_a"],
        json={"action": "confirm", "note": "已上门查看"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "confirmed"

    workorder = (await _workorders(db_session, elder_id))[0]
    await db_session.refresh(workorder)
    assert workorder.status == WorkorderStatus.IN_PROGRESS
    disposition = (
        await db_session.execute(
            select(WorkorderDisposition).where(
                WorkorderDisposition.workorder_id == workorder.workorder_id,
                WorkorderDisposition.role == "nurse",
            )
        )
    ).scalar_one()
    assert disposition.status == WorkorderDispositionStatus.HANDLED

    again = await api.post(
        f"/api/v1/health-events/{body['event_id']}/decision",
        headers=staff["nurse_a"],
        json={"action": "confirm"},
    )
    assert again.status_code == 409


async def test_yellow_reject_archives_and_resubmit_builds_new_workorder(
    api, seed, staff, db_session
) -> None:
    """驳回路径：归档 + 取消工单；归档后重提同级黄色应建新单，不复用旧单。"""
    elder_id = seed["elders"]["d1"].elder_id
    resp = await _post(
        api,
        staff["nurse_a"],
        elder_id,
        bp_systolic=165,
        bp_diastolic=105,
        sleep_minutes=360,
        mood="一般",
        chief_complaint="没有不舒服",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending_confirm"

    decided = await api.post(
        f"/api/v1/health-events/{body['event_id']}/decision",
        headers=staff["nurse_a"],
        json={"action": "reject", "note": "设备误差，复测正常"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "archived"

    workorders = await _workorders(db_session, elder_id)
    assert len(workorders) == 1
    assert workorders[0].status == WorkorderStatus.CANCELLED
    assert workorders[0].resolved_at is not None

    pending = await api.get("/api/v1/health-events/pending", headers=staff["nurse_a"])
    assert pending.status_code == 200, pending.text
    assert all(item["event_id"] != body["event_id"] for item in pending.json()["items"])

    # 归档后重新提交同级黄色：dedupe_key 命中已取消旧单，应追加序号建新单
    again = await _post(
        api,
        staff["nurse_a"],
        elder_id,
        bp_systolic=168,
        bp_diastolic=106,
        sleep_minutes=350,
        mood="一般",
        chief_complaint="没有不舒服",
    )
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "pending_confirm"

    workorders = await _workorders(db_session, elder_id)
    assert len(workorders) == 2
    assert workorders[0].status == WorkorderStatus.CANCELLED
    assert workorders[1].dedupe_key != workorders[0].dedupe_key
    assert workorders[1].status == WorkorderStatus.OPEN


async def test_red_requires_doctor_and_creates_referral(
    api, seed, staff, db_session
) -> None:
    elder_id = seed["elders"]["single"].elder_id
    resp = await _post(
        api,
        staff["nurse_a"],
        elder_id,
        bp_systolic=185,
        bp_diastolic=115,
        sleep_minutes=300,
        mood="一般",
        chief_complaint="胸口痛，喘不上气",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["level"] == "red"
    assert body["status"] == "pending_confirm"
    assert body["required_role"] == "doctor"

    workorders = await _workorders(db_session, elder_id)
    assert len(workorders) == 1
    assert workorders[0].priority == WorkorderPriority.URGENT

    detail = await api.get(
        f"/api/v1/health-events/{body['event_id']}", headers=staff["doctor"]
    )
    assert detail.status_code == 200, detail.text
    snapshot = detail.json()
    assert snapshot["referral_id"].startswith("MOCK-")
    assert "systolic_ge_180" in snapshot["reasons"]

    nurse_denied = await api.post(
        f"/api/v1/health-events/{body['event_id']}/decision",
        headers=staff["nurse_a"],
        json={"action": "confirm"},
    )
    assert nurse_denied.status_code == 403

    decided = await api.post(
        f"/api/v1/health-events/{body['event_id']}/decision",
        headers=staff["doctor"],
        json={"action": "escalate"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "escalated"


async def test_scope_and_role_guards(api, seed, staff) -> None:
    elder_single = seed["elders"]["single"].elder_id
    elder_other = seed["elders"]["other"].elder_id

    denied = await _post(api, staff["nurse_b"], elder_single, bp_systolic=130)
    # 范围外统一 404（get_elder_for_user 隐藏存在性，与工单/档案口径一致）
    assert denied.status_code == 404

    created = await _post(
        api,
        staff["nurse_a"],
        elder_single,
        bp_systolic=165,
        bp_diastolic=100,
        sleep_minutes=420,
        mood="一般",
        chief_complaint="没有不舒服",
    )
    assert created.status_code == 200, created.text

    # 医生只关联 single，未关联 other：不可见即 404
    hidden = await api.get(
        f"/api/v1/health-events/{created.json()['event_id']}",
        headers=staff["doctor"],
    )
    assert hidden.status_code == 200  # single 在医生授权内

    other_resp = await _post(
        api, staff["nurse_b"], elder_other, bp_systolic=165, bp_diastolic=100
    )
    assert other_resp.status_code == 200, other_resp.text
    hidden_other = await api.get(
        f"/api/v1/health-events/{other_resp.json()['event_id']}",
        headers=staff["doctor"],
    )
    assert hidden_other.status_code == 404

    family_denied = await api.get(
        "/api/v1/health-events/pending", headers=staff["family"]
    )
    assert family_denied.status_code == 403


async def test_same_day_resubmission_is_idempotent(api, seed, staff, db_session) -> None:
    elder_id = seed["elders"]["d2"].elder_id
    payload = dict(
        bp_systolic=165,
        bp_diastolic=105,
        sleep_minutes=400,
        mood="一般",
        chief_complaint="没有不舒服",
    )
    first = await _post(api, staff["nurse_a"], elder_id, **payload)
    second = await _post(api, staff["nurse_a"], elder_id, **payload)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["event_id"] == second.json()["event_id"]
    assert len(await _workorders(db_session, elder_id)) == 1
