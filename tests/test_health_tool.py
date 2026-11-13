"""Day8 段1：Agent ``health_check`` 工具测试。

验证：缺项追问、irrelevant 的模型传参不改变 elder 归属、红色即时上报、
缺少服务端 elder 上下文时可见降级。
"""

from contextlib import asynccontextmanager

from sqlalchemy import select

import app.agent.tools as tools_module
from app.agent.tools import ToolContext, build_default_registry
from app.models import HealthEvent


def _patch_tool_session(monkeypatch, db_session) -> None:
    @asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tools_module, "get_session_factory", lambda: factory)


def _ctx(elder_id) -> ToolContext:
    return ToolContext(elder_id=str(elder_id), device_id="TAB-SINGLE")


async def test_health_check_asks_next_question(monkeypatch, db_session, seed) -> None:
    _patch_tool_session(monkeypatch, db_session)
    registry = build_default_registry()
    elder_id = seed["elders"]["single"].elder_id

    result = await registry.call(
        "health_check", {"bp_systolic": 130, "bp_diastolic": 80}, _ctx(elder_id)
    )
    assert result.ok
    assert "睡" in result.content

    event = (
        await db_session.execute(
            select(HealthEvent).where(HealthEvent.elder_id == elder_id)
        )
    ).scalar_one()
    assert event.status == "collecting"
    assert event.bp_systolic == 130


async def test_health_check_ignores_model_supplied_elder_id(
    monkeypatch, db_session, seed
) -> None:
    _patch_tool_session(monkeypatch, db_session)
    registry = build_default_registry()
    owner = seed["elders"]["single"].elder_id
    other = seed["elders"]["other"].elder_id

    result = await registry.call(
        "health_check",
        {
            "elder_id": str(other),  # 模型传参必须被忽略
            "bp_systolic": 130,
            "bp_diastolic": 80,
        },
        _ctx(owner),
    )
    assert result.ok
    event = (
        await db_session.execute(select(HealthEvent))
    ).scalar_one()
    assert event.elder_id == owner


async def test_health_check_red_complaint_escalates_immediately(
    monkeypatch, db_session, seed
) -> None:
    _patch_tool_session(monkeypatch, db_session)
    registry = build_default_registry()
    elder_id = seed["elders"]["d1"].elder_id

    result = await registry.call(
        "health_check", {"chief_complaint": "胸口痛，喘不上气"}, _ctx(elder_id)
    )
    assert result.ok
    assert "医生" in result.content
    event = (
        await db_session.execute(
            select(HealthEvent).where(HealthEvent.elder_id == elder_id)
        )
    ).scalar_one()
    assert event.level == "red"
    assert event.status == "pending_confirm"


async def test_health_check_without_elder_context_degrades(
    monkeypatch, db_session, seed
) -> None:
    _patch_tool_session(monkeypatch, db_session)
    registry = build_default_registry()
    result = await registry.call(
        "health_check", {"bp_systolic": 130}, ToolContext(elder_id=None)
    )
    assert result.ok is False
    assert result.degraded == "missing_elder_context"
