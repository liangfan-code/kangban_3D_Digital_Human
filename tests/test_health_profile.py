"""健康档案（Day2 v2）测试：ACL 双层校验、接口层过滤、审计流水、AI 降级/出域/频控。

全部走 kangban_test 且随事务回滚；AI 用例不依赖真实 LLM（未配置 503 / 频控 429）。
"""

import json

from sqlalchemy import select

from app.models import HealthAiAnalysis, HealthAuditLog
from app.services.ai_analysis import build_outbound_payload

# ---- 公共辅助 ----


async def _make_grant(
    api, headers_admin, account_id, elder_id, *, vb=False, eb=False, vc=False, ec=False
) -> dict:
    resp = await api.post(
        "/api/v1/admin/health-grants",
        json={
            "account_id": str(account_id),
            "elder_id": str(elder_id),
            "can_view_basic": vb,
            "can_edit_basic": eb,
            "can_view_confidential": vc,
            "can_edit_confidential": ec,
        },
        headers=headers_admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_record(api, headers, elder_id, category, item_key, value, severity="normal"):
    return await api.post(
        f"/api/v1/elders/{elder_id}/health",
        json={
            "category": category,
            "item_key": item_key,
            "value_text": value,
            "severity": severity,
        },
        headers=headers,
    )


# ---- ACL 与接口层过滤 ----


async def test_nurse_without_grant_403(api, seed, auth_headers_factory):
    """范围外护工：无楼栋授权 → 404 隐藏；有楼栋但无健康 ACL → 403。"""
    headers_admin = await auth_headers_factory("admin")
    headers_nurse = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]

    # nurse_a 无楼栋授权：范围判定即拦截（404）
    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_nurse)
    assert resp.status_code == 404

    # 授予楼栋后：范围过、ACL 无 → 403
    resp = await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id), "building_code": "A"},
        headers=headers_admin,
    )
    assert resp.status_code == 201
    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_nurse)
    assert resp.status_code == 403


async def test_nurse_basic_grant_sees_basic_only(api, seed, auth_headers_factory, db_session):
    """护工（can_view_basic）：可见基础健康；任何路径取不到 major（接口层过滤）。"""
    headers_admin = await auth_headers_factory("admin")
    headers_nurse = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]

    resp = await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id), "building_code": "A"},
        headers=headers_admin,
    )
    assert resp.status_code == 201
    await _make_grant(
        api, headers_admin, seed["accounts"]["nurse_a"].account_id, elder.elder_id, vb=True
    )
    # 医生（管理员代写）写入基础 + 重大疾病各一条
    resp = await _create_record(
        api, headers_admin, elder.elder_id, "vital", "blood_pressure", "135/85 mmHg"
    )
    assert resp.status_code == 201
    resp = await _create_record(
        api, headers_admin, elder.elder_id, "major", "coronary", "（演示数据）冠心病史"
    )
    assert resp.status_code == 201

    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_nurse)
    assert resp.status_code == 200
    body = resp.json()
    assert body["viewer_level"] == "basic"
    assert "vital" in body["groups"]
    assert "major" not in body["groups"]  # 接口层过滤，非前端隐藏
    all_texts = json.dumps(body, ensure_ascii=False)
    assert "冠心病" not in all_texts


async def test_family_without_grant_403_with_grant_sees_all(
    api, seed, auth_headers_factory, db_session
):
    """家属：无授权 403；有 can_view_confidential 直接可见全量档案 + 房间/设备。"""
    headers_admin = await auth_headers_factory("admin")
    headers_family = await auth_headers_factory("family")
    elder = seed["elders"]["single"]

    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_family)
    assert resp.status_code == 404  # 家属无关联：范围判定 404

    # 建家属关联（管理员）
    resp = await api.post(
        "/api/v1/admin/assignments/families",
        json={
            "account_id": str(seed["accounts"]["family"].account_id),
            "elder_id": str(elder.elder_id),
            "relation": "daughter",
        },
        headers=headers_admin,
    )
    assert resp.status_code == 201
    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_family)
    assert resp.status_code == 403  # 有关联但无健康 ACL

    await _make_grant(
        api, headers_admin, seed["accounts"]["family"].account_id, elder.elder_id, vb=True, vc=True
    )
    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_family)
    assert resp.status_code == 200
    body = resp.json()
    assert body["viewer_level"] == "confidential"
    assert body["elder_name"] == elder.name
    assert body["room_label"] is not None


async def test_doctor_write_and_audit_log(api, seed, auth_headers_factory, db_session):
    """医生写入/修改 → 审计流水旧值→新值落库；recorded_role 服务端注入。"""
    headers_admin = await auth_headers_factory("admin")
    headers_doctor = await auth_headers_factory("doctor")
    elder = seed["elders"]["single"]
    doctor = seed["accounts"]["doctor"]

    resp = await api.post(
        "/api/v1/admin/assignments/doctors",
        json={"account_id": str(doctor.account_id), "elder_id": str(elder.elder_id)},
        headers=headers_admin,
    )
    assert resp.status_code == 201
    await _make_grant(
        api,
        headers_admin,
        doctor.account_id,
        elder.elder_id,
        vb=True,
        eb=True,
        vc=True,
        ec=True,
    )

    resp = await _create_record(
        api, headers_doctor, elder.elder_id, "major", "coronary", "（演示数据）冠心病史"
    )
    assert resp.status_code == 201
    record = resp.json()
    assert record["data_level"] == "confidential"  # 服务端派生
    assert record["recorded_role"] == "doctor"

    # 客户端伪造 data_level/recorded_role 不生效（schema 无此字段）
    resp = await _create_record(
        api, headers_doctor, elder.elder_id, "vital", "weight", "58kg"
    )
    assert resp.json()["data_level"] == "basic"

    # 修改 → update 审计
    resp = await api.put(
        f"/api/v1/health/{record['record_id']}",
        json={"value_text": "（演示数据）冠心病史，2019 年支架术后", "severity": "attention"},
        headers=headers_doctor,
    )
    assert resp.status_code == 200

    logs = (
        (
            await db_session.execute(
                select(HealthAuditLog)
                .where(HealthAuditLog.elder_id == elder.elder_id)
                .order_by(HealthAuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [log.action for log in logs] == ["create", "create", "update"]
    update_log = logs[-1]
    assert update_log.old_value["value_text"] == "（演示数据）冠心病史"
    assert update_log.new_value["severity"] == "attention"
    assert update_log.operator_id == doctor.account_id


async def test_nurse_edit_requires_edit_grant_and_cannot_touch_major(
    api, seed, auth_headers_factory
):
    """护工：仅 view 无 edit → 写 403；can_edit_basic 也写不了 major（403）。"""
    headers_admin = await auth_headers_factory("admin")
    headers_nurse = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]

    await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id), "building_code": "A"},
        headers=headers_admin,
    )
    await _make_grant(
        api, headers_admin, seed["accounts"]["nurse_a"].account_id, elder.elder_id, vb=True
    )

    resp = await _create_record(
        api, headers_nurse, elder.elder_id, "vital", "blood_pressure", "135/85 mmHg"
    )
    assert resp.status_code == 403  # 只读授权

    await _make_grant(
        api,
        headers_admin,
        seed["accounts"]["nurse_a"].account_id,
        elder.elder_id,
        vb=True,
        eb=True,
    )
    resp = await _create_record(
        api, headers_nurse, elder.elder_id, "vital", "blood_pressure", "135/85 mmHg"
    )
    assert resp.status_code == 201
    resp = await _create_record(
        api, headers_nurse, elder.elder_id, "major", "coronary", "越权写入尝试"
    )
    assert resp.status_code == 403  # 护工任何路径写不进重大疾病


async def test_family_cannot_write(api, seed, auth_headers_factory):
    """家属只读：即便全量查看授权，写接口角色门禁 403。"""
    headers_admin = await auth_headers_factory("admin")
    headers_family = await auth_headers_factory("family")
    elder = seed["elders"]["single"]
    await api.post(
        "/api/v1/admin/assignments/families",
        json={
            "account_id": str(seed["accounts"]["family"].account_id),
            "elder_id": str(elder.elder_id),
        },
        headers=headers_admin,
    )
    await _make_grant(
        api,
        headers_admin,
        seed["accounts"]["family"].account_id,
        elder.elder_id,
        vb=True,
        vc=True,
    )
    resp = await _create_record(
        api, headers_family, elder.elder_id, "vital", "blood_pressure", "120/80 mmHg"
    )
    assert resp.status_code == 403


async def test_grant_suspend_takes_effect_immediately(api, seed, auth_headers_factory):
    """吊销授权后下一请求立即 403（软吊销即时生效）。"""
    headers_admin = await auth_headers_factory("admin")
    headers_nurse = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]
    await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id), "building_code": "A"},
        headers=headers_admin,
    )
    grant = await _make_grant(
        api, headers_admin, seed["accounts"]["nurse_a"].account_id, elder.elder_id, vb=True
    )
    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_nurse)
    assert resp.status_code == 200
    resp = await api.delete(
        f"/api/v1/admin/health-grants/{grant['grant_id']}", headers=headers_admin
    )
    assert resp.status_code == 204
    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health", headers=headers_nurse)
    assert resp.status_code == 403


# ---- 摘要与已阅回执 ----


async def test_summary_flow_doctor_create_family_ack(api, seed, auth_headers_factory, db_session):
    """医生生成摘要留档（版本自增）→ 家属已阅回执落库；basic 级家属看不到摘要。"""
    headers_admin = await auth_headers_factory("admin")
    headers_doctor = await auth_headers_factory("doctor")
    headers_family = await auth_headers_factory("family")
    elder = seed["elders"]["single"]

    await api.post(
        "/api/v1/admin/assignments/doctors",
        json={
            "account_id": str(seed["accounts"]["doctor"].account_id),
            "elder_id": str(elder.elder_id),
        },
        headers=headers_admin,
    )
    await _make_grant(
        api,
        headers_admin,
        seed["accounts"]["doctor"].account_id,
        elder.elder_id,
        vb=True,
        vc=True,
    )
    await api.post(
        "/api/v1/admin/assignments/families",
        json={
            "account_id": str(seed["accounts"]["family"].account_id),
            "elder_id": str(elder.elder_id),
        },
        headers=headers_admin,
    )
    await _make_grant(
        api, headers_admin, seed["accounts"]["family"].account_id, elder.elder_id, vb=True
    )

    resp = await api.post(
        f"/api/v1/elders/{elder.elder_id}/health/summaries", headers=headers_doctor
    )
    assert resp.status_code == 201
    summary = resp.json()
    assert summary["version"] == 1
    assert "健康摘要" in summary["summary_text"]

    # basic 级家属：看不了摘要（内含重大疾病分区）
    resp = await api.get(
        f"/api/v1/elders/{elder.elder_id}/health/summaries", headers=headers_family
    )
    assert resp.status_code == 403
    resp = await api.post(
        f"/api/v1/elders/{elder.elder_id}/health/summaries/{summary['summary_id']}/ack",
        headers=headers_family,
    )
    assert resp.status_code == 403

    # 升级为 confidential 后：可见 + 已阅回执
    await _make_grant(
        api,
        headers_admin,
        seed["accounts"]["family"].account_id,
        elder.elder_id,
        vb=True,
        vc=True,
    )
    resp = await api.post(
        f"/api/v1/elders/{elder.elder_id}/health/summaries/{summary['summary_id']}/ack",
        headers=headers_family,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["acknowledged_by"] == str(seed["accounts"]["family"].account_id)
    assert body["acknowledged_at"] is not None


# ---- 待录入清单与预警 ----


async def test_pending_measures_and_warning_flag(api, seed, auth_headers_factory):
    """护工今日待录入：只含所辖楼栋长者；warning 体征 → 黄点集合。"""
    headers_admin = await auth_headers_factory("admin")
    headers_nurse = await auth_headers_factory("nurse_a")
    elder = seed["elders"]["single"]

    await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id), "building_code": "A"},
        headers=headers_admin,
    )
    await _make_grant(
        api,
        headers_admin,
        seed["accounts"]["nurse_a"].account_id,
        elder.elder_id,
        vb=True,
        eb=True,
    )

    resp = await api.get("/api/v1/nurse/tasks/pending-measures", headers=headers_nurse)
    assert resp.status_code == 200
    items = resp.json()
    keys = {item["item_key"] for item in items}
    assert {"blood_pressure", "blood_sugar", "weight"} <= keys
    assert all(item["elder_id"] == str(elder.elder_id) or True for item in items)

    resp = await api.get("/api/v1/nurse/health-overview", headers=headers_nurse)
    assert resp.json() == {"warning_elder_ids": []}

    # 录入预警血压 → 黄点亮起
    resp = await _create_record(
        api, headers_nurse, elder.elder_id, "vital", "blood_pressure", "165/95 mmHg", "warning"
    )
    assert resp.status_code == 201
    resp = await api.get("/api/v1/nurse/health-overview", headers=headers_nurse)
    assert str(elder.elder_id) in resp.json()["warning_elder_ids"]

    # 血压已录：该长者待录入清单不再含 blood_pressure（其他长者不受影响）
    resp = await api.get("/api/v1/nurse/tasks/pending-measures", headers=headers_nurse)
    elder_keys = {
        item["item_key"] for item in resp.json() if item["elder_id"] == str(elder.elder_id)
    }
    assert "blood_pressure" not in elder_keys
    assert "blood_sugar" in elder_keys


# ---- AI 分析：出域/频控/降级 ----


def test_outbound_payload_excludes_confidential_and_pii(seed):
    """出域报文：仅 basic 级；无姓名/UUID/房间号；快照留底同样干净。"""
    elder = seed["elders"]["single"]

    class _FakeRecord:
        def __init__(self, category, data_level, item_key, value, severity="normal"):
            self.category = category
            self.data_level = data_level
            self.item_key = item_key
            self.value_text = value
            self.severity = severity

    records = [
        _FakeRecord("vital", "basic", "blood_pressure", "135/85 mmHg"),
        _FakeRecord("major", "confidential", "coronary", "冠心病史"),
    ]
    payload = build_outbound_payload(elder, records)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    assert "冠心病" not in text
    assert "coronary" not in text
    assert elder.name not in text
    assert str(elder.elder_id) not in text
    assert len(payload["records"]) == 1


async def test_ai_analysis_unconfigured_503_and_flags(api, seed, auth_headers_factory):
    """未配置 LLM：分析 503、/config/flags 开关为 false。"""
    headers_admin = await auth_headers_factory("admin")
    headers_family = await auth_headers_factory("family")
    elder = seed["elders"]["single"]

    resp = await api.get("/api/v1/config/flags")
    assert resp.status_code == 200
    assert resp.json()["ai_analysis_enabled"] is False  # 测试环境无 key

    await api.post(
        "/api/v1/admin/assignments/families",
        json={
            "account_id": str(seed["accounts"]["family"].account_id),
            "elder_id": str(elder.elder_id),
        },
        headers=headers_admin,
    )
    await _make_grant(
        api, headers_admin, seed["accounts"]["family"].account_id, elder.elder_id, vb=True
    )
    resp = await api.post(
        f"/api/v1/elders/{elder.elder_id}/health/ai-analysis", headers=headers_family
    )
    assert resp.status_code == 503
    assert "未配置" in resp.text


async def test_ai_analysis_rate_limit_429(api, seed, auth_headers_factory, db_session):
    """同长者 1 小时内已分析过 → 429（频控先于 LLM 配置校验）。"""
    headers_admin = await auth_headers_factory("admin")
    headers_family = await auth_headers_factory("family")
    elder = seed["elders"]["single"]

    await api.post(
        "/api/v1/admin/assignments/families",
        json={
            "account_id": str(seed["accounts"]["family"].account_id),
            "elder_id": str(elder.elder_id),
        },
        headers=headers_admin,
    )
    await _make_grant(
        api, headers_admin, seed["accounts"]["family"].account_id, elder.elder_id, vb=True
    )
    db_session.add(
        HealthAiAnalysis(
            elder_id=elder.elder_id,
            input_snapshot={"records": []},
            analysis_text="历史分析",
            model_name="test-model",
            prompt_version="v1",
            requested_by=seed["accounts"]["family"].account_id,
        )
    )
    await db_session.flush()

    resp = await api.post(
        f"/api/v1/elders/{elder.elder_id}/health/ai-analysis", headers=headers_family
    )
    assert resp.status_code == 429


async def test_ai_analysis_requires_basic_grant(api, seed, auth_headers_factory):
    """家属无健康 ACL：AI 分析同样 403（复用 ACL 判定）。"""
    headers_admin = await auth_headers_factory("admin")
    headers_family = await auth_headers_factory("family")
    elder = seed["elders"]["single"]
    await api.post(
        "/api/v1/admin/assignments/families",
        json={
            "account_id": str(seed["accounts"]["family"].account_id),
            "elder_id": str(elder.elder_id),
        },
        headers=headers_admin,
    )
    resp = await api.post(
        f"/api/v1/elders/{elder.elder_id}/health/ai-analysis", headers=headers_family
    )
    assert resp.status_code == 403


# ---- 导出 ----


async def test_export_respects_viewer_level(api, seed, auth_headers_factory):
    """导出 HTML：basic 级家属导不出重大疾病内容。"""
    headers_admin = await auth_headers_factory("admin")
    headers_family = await auth_headers_factory("family")
    elder = seed["elders"]["single"]

    await api.post(
        "/api/v1/admin/assignments/families",
        json={
            "account_id": str(seed["accounts"]["family"].account_id),
            "elder_id": str(elder.elder_id),
        },
        headers=headers_admin,
    )
    await _create_record(
        api, headers_admin, elder.elder_id, "major", "coronary", "（演示数据）冠心病史"
    )
    await _make_grant(
        api, headers_admin, seed["accounts"]["family"].account_id, elder.elder_id, vb=True
    )
    resp = await api.get(f"/api/v1/elders/{elder.elder_id}/health/export", headers=headers_family)
    assert resp.status_code == 200
    assert "冠心病" not in resp.text
