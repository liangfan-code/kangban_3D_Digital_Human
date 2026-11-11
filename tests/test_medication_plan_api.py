"""用药计划 API：创建/确认/修改/取消、长期工单、家属字段白名单。"""

import uuid

from sqlalchemy import select

from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    FamilyElderLink,
    Workorder,
    WorkorderEvent,
)
from app.models.enums import WorkorderOrderKind, WorkorderStatus

PLAN_PAYLOAD = {
    "drug_name": "二甲双胍",
    "dose": "0.5g",
    "schedule": {"times": ["08:00", "20:00"], "weekdays": []},
    "instructions": "随餐服用",
    "internal_note": "内部：注意肾功能",
    "start_date": "2026-11-11",
    "end_date": "2026-12-31",
}


async def _bind(db_session, seed, *, family=True):
    db_session.add_all(
        [
            DoctorElderAssignment(
                account_id=seed["accounts"]["doctor"].account_id,
                elder_id=seed["elders"]["d1"].elder_id,
            ),
            BuildingAssignment(
                account_id=seed["accounts"]["nurse_a"].account_id,
                building_code="A",
            ),
        ]
    )
    if family:
        db_session.add(
            FamilyElderLink(
                account_id=seed["accounts"]["family"].account_id,
                elder_id=seed["elders"]["d1"].elder_id,
            )
        )
    await db_session.flush()


async def _create(api, headers, elder_id, **overrides):
    payload = {**PLAN_PAYLOAD, **overrides}
    return await api.post(
        f"/api/v1/elders/{elder_id}/medication-plans", json=payload, headers=headers
    )


async def test_doctor_creates_draft_nurse_cannot_see(
    api, db_session, seed, auth_headers_factory
):
    await _bind(db_session, seed)
    doctor = await auth_headers_factory("doctor")
    nurse = await auth_headers_factory("nurse_a")
    elder_id = seed["elders"]["d1"].elder_id

    resp = await _create(api, doctor, elder_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "planned"
    assert body["created_by_account_id"] == str(seed["accounts"]["doctor"].account_id)
    assert body["workorder_id"] is None

    nurse_resp = await api.get(
        f"/api/v1/elders/{elder_id}/medication-plans", headers=nurse
    )
    assert nurse_resp.status_code == 200
    assert nurse_resp.json() == []


async def test_confirm_creates_standing_workorder_and_nurse_view_hides_uuids(
    api, db_session, seed, auth_headers_factory
):
    await _bind(db_session, seed)
    doctor = await auth_headers_factory("doctor")
    nurse = await auth_headers_factory("nurse_a")
    elder_id = seed["elders"]["d1"].elder_id
    plan_id = (await _create(api, doctor, elder_id)).json()["plan_id"]

    resp = await api.post(
        f"/api/v1/medication-plans/{plan_id}/confirm", headers=doctor
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["workorder_id"] is not None
    assert body["confirmed_by_account_id"] == str(
        seed["accounts"]["doctor"].account_id
    )

    workorder = (
        await db_session.execute(
            select(Workorder).where(Workorder.medication_plan_id == uuid.UUID(plan_id))
        )
    ).scalar_one()
    assert workorder.order_kind == WorkorderOrderKind.STANDING
    assert workorder.effective_from.isoformat() == "2026-11-11"

    nurse_resp = await api.get(
        f"/api/v1/elders/{elder_id}/medication-plans", headers=nurse
    )
    items = nurse_resp.json()
    assert len(items) == 1
    assert "created_by_account_id" not in items[0]
    assert "updated_by_account_id" not in items[0]
    assert items[0]["internal_note"] == "内部：注意肾功能"


async def test_update_active_plan_writes_revised_event_and_family_history(
    api, db_session, seed, auth_headers_factory
):
    await _bind(db_session, seed)
    doctor = await auth_headers_factory("doctor")
    family = await auth_headers_factory("family")
    elder_id = seed["elders"]["d1"].elder_id
    plan_id = (await _create(api, doctor, elder_id)).json()["plan_id"]
    await api.post(f"/api/v1/medication-plans/{plan_id}/confirm", headers=doctor)

    update = await api.put(
        f"/api/v1/medication-plans/{plan_id}",
        json={"drug_name": "二甲双胍缓释片", "internal_note": "内部：已复查"},
        headers=doctor,
    )
    assert update.status_code == 200, update.text
    assert update.json()["drug_name"] == "二甲双胍缓释片"

    workorder_id = uuid.UUID(update.json()["workorder_id"])
    events = (
        await db_session.execute(
            select(WorkorderEvent)
            .where(WorkorderEvent.workorder_id == workorder_id)
            .order_by(WorkorderEvent.created_at)
        )
    ).scalars().all()
    revised = [event for event in events if event.event_type == "revised"]
    assert len(revised) == 1
    payload = revised[0].payload
    assert payload["plan_version"] == 2
    fields = {item["field"]: item for item in payload["changes"]}
    assert fields["drug_name"]["before"] == "二甲双胍"
    assert fields["drug_name"]["after"] == "二甲双胍缓释片"
    assert fields["internal_note"] == {"field": "internal_note", "changed": True}

    family_plans = await api.get(
        f"/api/v1/family/elders/{elder_id}/medication-plans", headers=family
    )
    assert family_plans.status_code == 200, family_plans.text
    item = family_plans.json()[0]
    for forbidden in (
        "internal_note",
        "workorder_id",
        "created_by_account_id",
        "updated_by_account_id",
        "confirmed_by_account_id",
    ):
        assert forbidden not in item

    history = await api.get(
        f"/api/v1/family/elders/{elder_id}/medication-plans/{plan_id}/history",
        headers=family,
    )
    assert history.status_code == 200, history.text
    items = history.json()
    assert items[0]["changes"] == []
    fields = {change["field"] for change in items[1]["changes"]}
    assert fields == {"drug_name"}


async def test_cancel_plan_closes_standing_workorder(
    api, db_session, seed, auth_headers_factory
):
    await _bind(db_session, seed)
    doctor = await auth_headers_factory("doctor")
    elder_id = seed["elders"]["d1"].elder_id
    plan_id = (await _create(api, doctor, elder_id)).json()["plan_id"]
    await api.post(f"/api/v1/medication-plans/{plan_id}/confirm", headers=doctor)

    resp = await api.post(f"/api/v1/medication-plans/{plan_id}/cancel", headers=doctor)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    workorder = (
        await db_session.execute(
            select(Workorder).where(Workorder.medication_plan_id == uuid.UUID(plan_id))
        )
    ).scalar_one()
    assert workorder.status == WorkorderStatus.CANCELLED
    assert "自动关闭" in workorder.closed_note


async def test_create_rejects_extra_fields_and_invalid_schedule(
    api, db_session, seed, auth_headers_factory
):
    await _bind(db_session, seed)
    doctor = await auth_headers_factory("doctor")
    elder_id = seed["elders"]["d1"].elder_id

    extra = await _create(api, doctor, elder_id, plan_id=str(uuid.uuid4()))
    assert extra.status_code == 422

    bad_time = await _create(
        api, doctor, elder_id, schedule={"times": ["25:00"], "weekdays": []}
    )
    assert bad_time.status_code == 422

    bad_range = await _create(
        api, doctor, elder_id, start_date="2026-12-31", end_date="2026-11-11"
    )
    assert bad_range.status_code == 422
