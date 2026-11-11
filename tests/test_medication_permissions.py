"""用药计划权限边界：仅绑定医生可写、护工按楼栋、家属按有效关联、管理员只读。"""

from sqlalchemy import select

from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    FamilyElderLink,
    MedicationPlan,
)
from app.models.enums import MedicationPlanStatus

PLAN_PAYLOAD = {
    "drug_name": "阿托伐他汀",
    "dose": "20mg",
    "schedule": {"times": ["21:00"], "weekdays": []},
    "instructions": "睡前服用",
    "internal_note": "内部备注",
    "start_date": "2026-11-11",
}


async def _bind_doctor(db_session, seed, *, elder="d1", nurse_building="A"):
    db_session.add(
        DoctorElderAssignment(
            account_id=seed["accounts"]["doctor"].account_id,
            elder_id=seed["elders"][elder].elder_id,
        )
    )
    if nurse_building:
        db_session.add(
            BuildingAssignment(
                account_id=seed["accounts"]["nurse_a"].account_id,
                building_code=nurse_building,
            )
        )
    await db_session.flush()


async def _create(api, headers, elder_id):
    return await api.post(
        f"/api/v1/elders/{elder_id}/medication-plans",
        json=PLAN_PAYLOAD,
        headers=headers,
    )


async def test_write_roles_admin_and_nurse_are_forbidden(
    api, db_session, seed, auth_headers_factory
):
    await _bind_doctor(db_session, seed)
    elder_id = seed["elders"]["d1"].elder_id

    nurse = await auth_headers_factory("nurse_a")
    admin = await auth_headers_factory("admin")
    assert (await _create(api, nurse, elder_id)).status_code == 403
    assert (await _create(api, admin, elder_id)).status_code == 403


async def test_unbound_doctor_gets_hidden_404(
    api, db_session, seed, auth_headers_factory
):
    await _bind_doctor(db_session, seed, elder="d2")
    doctor = await auth_headers_factory("doctor")
    resp = await _create(api, doctor, seed["elders"]["d1"].elder_id)
    assert resp.status_code == 404


async def test_nurse_building_scope_and_family_link_scope(
    api, db_session, seed, auth_headers_factory
):
    await _bind_doctor(db_session, seed)
    doctor = await auth_headers_factory("doctor")
    nurse_a = await auth_headers_factory("nurse_a")
    nurse_b = await auth_headers_factory("nurse_b")
    family = await auth_headers_factory("family")
    elder_id = seed["elders"]["d1"].elder_id

    plan_id = (await _create(api, doctor, elder_id)).json()["plan_id"]

    # 未绑定楼栋的护工 / 未关联的家属：404 隐藏存在性
    assert (
        await api.get(f"/api/v1/elders/{elder_id}/medication-plans", headers=nurse_b)
    ).status_code == 404
    assert (
        await api.get(
            f"/api/v1/family/elders/{elder_id}/medication-plans", headers=family
        )
    ).status_code == 404
    assert (
        await api.get(
            f"/api/v1/family/elders/{elder_id}/medication-history", headers=family
        )
    ).status_code == 404

    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=elder_id,
        )
    )
    await db_session.flush()

    linked = await api.get(
        f"/api/v1/family/elders/{elder_id}/medication-plans", headers=family
    )
    assert linked.status_code == 200
    assert linked.json() == []  # 草稿对家属不可见

    # 护工草稿不可见（详情也 404）
    assert (
        await api.get(f"/api/v1/medication-plans/{plan_id}", headers=nurse_a)
    ).status_code == 404

    # 家属不可访问员工端服药任务核对视图
    assert (
        await api.get(
            f"/api/v1/elders/{elder_id}/medication-orders", headers=family
        )
    ).status_code == 403


async def test_admin_reads_drafts_and_nurse_sees_after_confirm(
    api, db_session, seed, auth_headers_factory
):
    await _bind_doctor(db_session, seed)
    doctor = await auth_headers_factory("doctor")
    admin = await auth_headers_factory("admin")
    nurse = await auth_headers_factory("nurse_a")
    elder_id = seed["elders"]["d1"].elder_id

    plan_id = (await _create(api, doctor, elder_id)).json()["plan_id"]

    admin_list = await api.get(
        f"/api/v1/elders/{elder_id}/medication-plans", headers=admin
    )
    assert admin_list.status_code == 200
    assert admin_list.json()[0]["status"] == MedicationPlanStatus.PLANNED.value

    await api.post(f"/api/v1/medication-plans/{plan_id}/confirm", headers=doctor)
    nurse_list = await api.get(
        f"/api/v1/elders/{elder_id}/medication-plans", headers=nurse
    )
    assert len(nurse_list.json()) == 1

    plan = (
        await db_session.execute(
            select(MedicationPlan).where(MedicationPlan.elder_id == elder_id)
        )
    ).scalar_one()
    assert plan.status == MedicationPlanStatus.ACTIVE
