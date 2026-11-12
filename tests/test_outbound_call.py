"""Day7 段4：设备解析与主动外呼编排（RTC / WS 降级 / 无设备建漏服单）。"""

import asyncio
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    Device,
    Elder,
    MedicationEvent,
    MedicationOrder,
    MedicationPlan,
    Room,
    Workorder,
)
from app.models.enums import (
    MedicationEventSource,
    MedicationEventType,
    MedicationOrderStatus,
    MedicationPlanStatus,
)
from app.realtime.session import OutboundCall
from app.services import outbound
from app.services.medication_order import ensure_order

SLOT = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(db_connection):
    return async_sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


class FakeManager:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.events: list[dict] = []

    def get(self, device_id):
        return object() if self.connected else None

    async def send_event_to(self, device_id, event) -> bool:
        if not self.connected:
            return False
        self.events.append(event)
        return True


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: dict[str, object] = {}
        self.closed: list[str] = []

    def add(self, call) -> None:
        self.calls[call.call_id] = call

    def get(self, call_id):
        return self.calls.get(call_id)

    def find_by_device(self, device_id):
        return None

    async def close(self, call_id) -> None:
        self.closed.append(call_id)
        self.calls.pop(call_id, None)


class FakeCall:
    def __init__(self, *, fail: bool = False) -> None:
        self.call_id = uuid.uuid4().hex
        self.fail = fail

    async def create_offer(self, pcm: bytes, sample_rate: int = 16000) -> str:
        if self.fail:
            raise RuntimeError("offer failed")
        return "fake-offer"

    async def wait_answered(self, timeout: float) -> None:
        return None


async def _make_plan(db_session, elder) -> MedicationPlan:
    plan = MedicationPlan(
        elder_id=elder.elder_id,
        drug_name="降压药",
        dose="1 片",
        schedule={"times": ["08:00"], "weekdays": []},
        start_date=date(2026, 11, 1),
        end_date=None,
        status=MedicationPlanStatus.ACTIVE,
    )
    db_session.add(plan)
    await db_session.flush()
    return plan


async def _make_order(session_factory, plan) -> uuid.UUID:
    async with session_factory() as session:
        order, _ = await ensure_order(
            session,
            plan=plan,
            scheduled_at=SLOT,
            created_source=MedicationEventSource.SCHEDULER,
        )
        await session.commit()
        return order.order_id


async def test_resolve_device_prefers_bed_then_room_level(db_session, session_factory, seed):
    elder = seed["elders"]["single"]
    async with session_factory() as session:
        resolved = await outbound.resolve_elder_device(session, elder)
    assert resolved is not None
    assert resolved.device_id == seed["devices"]["single"].device_id

    bed_device = Device(
        device_code="TAB-SINGLE-BED1",
        room_id=seed["rooms"]["single"].room_id,
        bed_no=1,
        status="active",
    )
    db_session.add(bed_device)
    await db_session.flush()

    async with session_factory() as session:
        resolved = await outbound.resolve_elder_device(session, elder)
    assert resolved is not None
    assert resolved.device_id == bed_device.device_id


async def test_resolve_device_none_without_room(db_session, session_factory, seed):
    elder = Elder(
        name="无房间长者",
        persona_id=seed["personas"]["on"].persona_id,
        room_id=None,
        bed_no=None,
        status="active",
    )
    db_session.add(elder)
    await db_session.flush()
    async with session_factory() as session:
        assert await outbound.resolve_elder_device(session, elder) is None


async def test_no_device_marks_missed_and_creates_single_workorder(
    db_session, session_factory, seed
):
    room = Room(building_code="C", floor=1, room_number="301", bed_count=1)
    db_session.add(room)
    await db_session.flush()
    elder = Elder(
        name="无设备长者",
        persona_id=seed["personas"]["on"].persona_id,
        room_id=room.room_id,
        bed_no=1,
        status="active",
    )
    db_session.add(elder)
    await db_session.flush()
    plan = await _make_plan(db_session, elder)
    order_id = await _make_order(session_factory, plan)

    result = await outbound.trigger_medication_call(
        order_id, session_factory=session_factory, session_manager=FakeManager()
    )
    order = await db_session.get(MedicationOrder, order_id)
    workorders = (
        await db_session.execute(
            select(Workorder).where(Workorder.medication_order_id == order_id)
        )
    ).scalars().all()
    events = (
        await db_session.execute(
            select(MedicationEvent.event_type).where(MedicationEvent.order_id == order_id)
        )
    ).scalars().all()

    assert result.degraded == "no_device"
    assert result.channel == "none"
    assert order.status == MedicationOrderStatus.MISSED
    assert len(workorders) == 1
    assert workorders[0].type == "missed_medication"
    assert MedicationEventType.MISSED in events


async def test_rtc_call_success(
    db_session, session_factory, seed, monkeypatch, tmp_path
):
    plan = await _make_plan(db_session, seed["elders"]["single"])
    order_id = await _make_order(session_factory, plan)

    async def fake_synthesize(text, settings=None):
        return b"\x00\x00" * 320, 16000

    monkeypatch.setattr(outbound, "synthesize_pcm", fake_synthesize)
    manager = FakeManager()
    registry = FakeRegistry()
    result = await outbound.trigger_medication_call(
        order_id,
        session_factory=session_factory,
        checkpoint_path=str(tmp_path / "medication.sqlite3"),
        session_manager=manager,
        call_registry=registry,
        call_factory=lambda *args: FakeCall(),
    )
    order = await db_session.get(MedicationOrder, order_id)

    assert result.channel == "rtc"
    assert result.degraded is None
    assert result.attempt == 1
    assert order.status == MedicationOrderStatus.CALLING
    assert manager.events[0]["type"] == "rtc_call_offer"
    assert manager.events[0]["sdp"] == "fake-offer"


async def test_rtc_failure_degrades_to_ws_tts(
    db_session, session_factory, seed, monkeypatch, tmp_path
):
    plan = await _make_plan(db_session, seed["elders"]["single"])
    order_id = await _make_order(session_factory, plan)

    async def fake_synthesize(text, settings=None):
        return b"\x00\x00" * 320, 16000

    monkeypatch.setattr(outbound, "synthesize_pcm", fake_synthesize)
    manager = FakeManager()
    registry = FakeRegistry()
    result = await outbound.trigger_medication_call(
        order_id,
        session_factory=session_factory,
        checkpoint_path=str(tmp_path / "medication.sqlite3"),
        session_manager=manager,
        call_registry=registry,
        call_factory=lambda *args: FakeCall(fail=True),
    )

    assert result.channel == "ws"
    assert result.degraded == "rtc_unavailable"
    assert manager.events[-1]["type"] == "tts"
    assert manager.events[-1]["audio"]
    assert registry.closed, "RTC 失败后应关闭挂起的 PC"


async def test_ws_offline_marks_degraded_without_tts(
    db_session, session_factory, seed, monkeypatch, tmp_path
):
    plan = await _make_plan(db_session, seed["elders"]["single"])
    order_id = await _make_order(session_factory, plan)

    async def fake_synthesize(text, settings=None):  # pragma: no cover - 不应被调用
        raise AssertionError("WS 未连接时不应合成 TTS")

    monkeypatch.setattr(outbound, "synthesize_pcm", fake_synthesize)
    manager = FakeManager(connected=False)
    result = await outbound.trigger_medication_call(
        order_id,
        session_factory=session_factory,
        checkpoint_path=str(tmp_path / "medication.sqlite3"),
        session_manager=manager,
    )

    assert result.channel == "none"
    assert result.degraded == "rtc_unavailable"
    assert manager.events == []


async def test_outbound_call_adds_track_before_create_offer():
    class FakePC:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.localDescription = SimpleNamespace(sdp="sdp", type="offer")

        def addTrack(self, track) -> None:
            self.calls.append("addTrack")

        async def createOffer(self):
            self.calls.append("createOffer")
            return SimpleNamespace(sdp="offer", type="offer")

        async def setLocalDescription(self, desc) -> None:
            self.calls.append("setLocalDescription")

    call = object.__new__(OutboundCall)
    call.device_id = "dev-1"
    call.call_id = "call-1"
    call.pc = FakePC()
    call.answered = asyncio.Event()
    call.failed = asyncio.Event()
    call.audio_track = None
    call._closed = False

    sdp = await call.create_offer(b"\x00\x00" * 320, 16000)

    assert sdp == "sdp"
    assert call.pc.calls.index("addTrack") < call.pc.calls.index("createOffer")
