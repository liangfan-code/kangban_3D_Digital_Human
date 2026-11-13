"""Day8 段3：健康趋势 + 家属日报（只读）测试。"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.config import get_settings
from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    FamilyElderLink,
    HealthEvent,
    HealthGrant,
    HealthRecord,
    Workorder,
)
from app.models.enums import (
    AssignmentStatus,
    HealthEventLevel,
    HealthEventStatus,
    WorkorderPriority,
    WorkorderStatus,
)
from app.services import health_reports as report_service


def _local(day_offset: int = 0, hour: int = 9) -> datetime:
    tz = ZoneInfo(get_settings().app_timezone)
    base = datetime.now(tz) + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0).astimezone(UTC)


@pytest.fixture
async def grants(seed, db_session):
    """护士 A 与家属对 single 长者有 basic 查看权；医生仅关联但无授权。"""
    elder_id = seed["elders"]["single"].elder_id
    admin_id = seed["accounts"]["admin"].account_id
    db_session.add_all(
        [
            BuildingAssignment(
                account_id=seed["accounts"]["nurse_a"].account_id,
                building_code="A",
                status=AssignmentStatus.ACTIVE,
            ),
            FamilyElderLink(
                account_id=seed["accounts"]["family"].account_id,
                elder_id=elder_id,
                relation="子女",
                status=AssignmentStatus.ACTIVE,
            ),
            HealthGrant(
                account_id=seed["accounts"]["nurse_a"].account_id,
                elder_id=elder_id,
                can_view_basic=True,
                status=AssignmentStatus.ACTIVE,
                granted_by=admin_id,
            ),
            HealthGrant(
                account_id=seed["accounts"]["family"].account_id,
                elder_id=elder_id,
                can_view_basic=True,
                status=AssignmentStatus.ACTIVE,
                granted_by=admin_id,
            ),
            DoctorElderAssignment(
                account_id=seed["accounts"]["doctor"].account_id,
                elder_id=elder_id,
                status="active",
            ),
        ]
    )
    await db_session.flush()
    return {
        "nurse_a": seed["accounts"]["nurse_a"],
        "family": seed["accounts"]["family"],
        "doctor": seed["accounts"]["doctor"],
        "elder_id": elder_id,
    }


async def test_trend_fills_missing_days_and_keeps_last_of_day(
    api, seed, db_session, grants, auth_headers_factory
) -> None:
    elder_id = grants["elder_id"]
    headers = await auth_headers_factory("nurse_a")
    db_session.add_all(
        [
            HealthRecord(
                elder_id=elder_id,
                recorded_at=_local(-2, 8),
                bp_systolic=125,
                bp_diastolic=78,
                sleep_minutes=400,
                mood="一般",
                source="voice",
            ),
            HealthRecord(
                elder_id=elder_id,
                recorded_at=_local(0, 9),
                bp_systolic=130,
                bp_diastolic=80,
                sleep_minutes=420,
                mood="很好",
                source="voice",
            ),
            HealthRecord(
                elder_id=elder_id,
                recorded_at=_local(0, 20),
                bp_systolic=138,
                bp_diastolic=86,
                sleep_minutes=450,
                mood="很好",
                source="manual",
            ),
        ]
    )
    await db_session.flush()

    resp = await api.get(
        f"/api/v1/elders/{elder_id}/health-trend?days=3", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == 3
    assert body["has_data"] is True
    series = body["series"]
    assert len(series) == 3
    # 窗口为 今天-2 / 今天-1 / 今天；昨天无记录 → null
    assert series[0]["bp_systolic"] == 125
    assert series[1]["bp_systolic"] is None
    assert series[1]["sleep_minutes"] is None
    # 同日两条取最后一次
    assert series[2]["bp_systolic"] == 138
    assert series[2]["sleep_minutes"] == 450


async def test_trend_without_health_grant_is_forbidden(
    api, grants, auth_headers_factory
) -> None:
    headers = await auth_headers_factory("doctor")
    resp = await api.get(
        f"/api/v1/elders/{grants['elder_id']}/health-trend", headers=headers
    )
    assert resp.status_code == 403


async def test_trend_for_out_of_scope_elder_is_hidden(
    api, seed, auth_headers_factory
) -> None:
    headers = await auth_headers_factory("doctor")
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['d1'].elder_id}/health-trend", headers=headers
    )
    assert resp.status_code == 404


async def test_daily_report_summarizes_vitals_events_and_missed_medication(
    api, db_session, grants, auth_headers_factory
) -> None:
    elder_id = grants["elder_id"]
    headers = await auth_headers_factory("family")
    db_session.add_all(
        [
            HealthRecord(
                elder_id=elder_id,
                recorded_at=_local(0, 9),
                bp_systolic=132,
                bp_diastolic=84,
                sleep_minutes=430,
                mood="很好",
                chief_complaint="有点头晕",
                source="voice",
            ),
            HealthEvent(
                thread_id=f"health:{elder_id}:{_local(0, 9).date().isoformat()}",
                elder_id=elder_id,
                level=HealthEventLevel.YELLOW,
                status=HealthEventStatus.PENDING_CONFIRM,
                bp_systolic=132,
                bp_diastolic=84,
            ),
            Workorder(
                elder_id=elder_id,
                type="missed_medication",
                title="漏服用药",
                status=WorkorderStatus.OPEN,
                priority=WorkorderPriority.HIGH,
            ),
        ]
    )
    await db_session.flush()

    resp = await api.get(
        f"/api/v1/elders/{elder_id}/health-daily-report", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["elder_name"] == "王单间"
    assert body["level"] == "yellow"
    assert body["pending_events"] == 1
    assert body["missed_medication"] == 1
    text = body["text"]
    assert "血压 132/84" in text
    assert "睡眠 7 小时 10 分" in text
    assert "漏服用药 1 次" in text
    assert len(text) <= report_service.MAX_REPORT_CHARS
    for word in ("诊断", "确诊", "处方", "服药建议"):
        assert word not in text
    # 未请求 tts 时不返回音频
    assert body["audio_pcm_base64"] is None
    assert body["tts_degraded"] is None


async def test_daily_report_without_record_is_explicit(
    api, grants, auth_headers_factory
) -> None:
    headers = await auth_headers_factory("nurse_a")
    resp = await api.get(
        f"/api/v1/elders/{grants['elder_id']}/health-daily-report"
        f"?date={_local(-5).date().isoformat()}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "尚未采集到体征数据" in body["text"]
    assert body["level"] is None


async def test_daily_report_tts_degrades_visibly(
    api, grants, monkeypatch, auth_headers_factory
) -> None:
    async def _broken(_text: str):
        return None

    monkeypatch.setattr(report_service, "synthesize_report_audio", _broken)
    headers = await auth_headers_factory("nurse_a")
    resp = await api.get(
        f"/api/v1/elders/{grants['elder_id']}/health-daily-report?tts=1",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"]
    assert body["tts_degraded"] == "tts_unavailable"
    assert body["audio_pcm_base64"] is None


async def test_health_trend_rejects_out_of_range_days(
    api, grants, auth_headers_factory
) -> None:
    headers = await auth_headers_factory("nurse_a")
    resp = await api.get(
        f"/api/v1/elders/{grants['elder_id']}/health-trend?days=31", headers=headers
    )
    assert resp.status_code == 422
