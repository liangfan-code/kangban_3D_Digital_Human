"""Day8 段1：夜间健康采集调度测试（不启动真实 APScheduler、不连开发库）。"""

from contextlib import asynccontextmanager

from app.core.config import get_settings
from app.models import Elder, Room
from app.services.health_scheduler import (
    collect_candidate_ids,
    register_health_scheduler,
    run_nightly_collection,
)


def _factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def test_collect_candidates_filters_active_and_device_bound(
    db_session, seed
) -> None:
    room_no_device = Room(
        building_code="C", floor=3, room_number="301", bed_count=1
    )
    db_session.add(room_no_device)
    await db_session.flush()

    persona_id = seed["personas"]["on"].persona_id
    db_session.add_all(
        [
            Elder(
                name="无设备房",
                persona_id=persona_id,
                room_id=room_no_device.room_id,
                bed_no=1,
                status="active",
            ),
            Elder(
                name="已退住",
                persona_id=persona_id,
                room_id=seed["rooms"]["other"].room_id,
                bed_no=2,
                status="discharged",
            ),
        ]
    )
    await db_session.flush()

    ids = set(await collect_candidate_ids(db_session))
    expected = {
        seed["elders"][name].elder_id
        for name in ("single", "d1", "d2", "other")
    }
    assert ids == expected


async def test_nightly_collection_isolates_single_failure(db_session, seed) -> None:
    called: list = []

    async def trigger(elder_id, **kwargs):
        called.append(elder_id)
        if elder_id == seed["elders"]["d1"].elder_id:
            raise RuntimeError("设备发送失败")

    summary = await run_nightly_collection(
        session_factory=_factory(db_session),
        trigger=trigger,
        settings=get_settings(),
    )
    assert summary == {"candidates": 4, "triggered": 3, "failed": 1}
    assert len(called) == 4


async def test_nightly_collection_skips_when_disabled(db_session, seed) -> None:
    called: list = []

    async def trigger(elder_id, **kwargs):
        called.append(elder_id)

    settings = get_settings().model_copy(update={"health_collect_enabled": False})
    summary = await run_nightly_collection(
        session_factory=_factory(db_session), trigger=trigger, settings=settings
    )
    assert summary == {"candidates": 0, "triggered": 0, "failed": 0}
    assert called == []


async def test_register_health_scheduler_adds_nightly_job(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.health_scheduler._is_test_environment", lambda settings: False
    )
    added: dict = {}

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            added["func"] = func
            added.update(kwargs)

    settings = get_settings()
    ok = register_health_scheduler(FakeScheduler(), settings)
    assert ok is True
    assert added["id"] == "health_collection_nightly"
    assert added["max_instances"] == 1
    # 时刻/时区绑定：health_collect_hour 或 app_timezone 改动被破坏时这里会失败
    trigger = added["trigger"]
    hour = min(23, max(0, settings.health_collect_hour))
    assert f"hour='{hour}'" in str(trigger)
    assert str(trigger.timezone) == settings.app_timezone


async def test_register_health_scheduler_skips_in_test_env() -> None:
    class FakeScheduler:
        def add_job(self, func, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("测试环境不应注册 job")

    assert register_health_scheduler(FakeScheduler(), get_settings()) is False
