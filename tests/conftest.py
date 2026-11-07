"""pytest 公共 fixture。

Day1：app / client（不连数据库）。
Day2：db_engine / db_connection / db_session / api / seed ——
连接 kangban_test（DATABASE_URL_TEST），session 级执行 alembic upgrade head，
函数级事务回滚隔离，严禁写入 kangban_dev。
"""

import os

os.environ.setdefault(
    "JWT_SECRET_KEY", "test-secret-0123456789abcdef0123456789abcdef"
)
# 测试环境强制 LLM 未配置（OS 环境变量优先级高于 .env 文件），
# 使 ai_analysis_enabled=False 与 AI 分析 503 语义不依赖本地 .env 是否配置了真实密钥。
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("LLM_BASE_URL", "")
os.environ.setdefault("LLM_MODEL", "")

import subprocess
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app as fastapi_app

TEST_PASSWORD = "Passw0rd!Local"
# bcrypt cost=12 较慢；整个进程只计算一次
_PASSWORD_HASH_CACHE: dict[str, str] = {}


def _test_db_url() -> str:
    from app.core.config import get_settings

    url = get_settings().database_url_test
    if not url:
        raise RuntimeError(
            "未配置 DATABASE_URL_TEST：请在 .env 中配置指向 kangban_test 的连接串后重试"
        )
    if url == get_settings().database_url or "kangban_dev?" in url or url.endswith("kangban_dev"):
        raise RuntimeError(
            "DATABASE_URL_TEST 不得指向 kangban_dev：测试数据严禁写入开发库"
        )
    return url


@pytest.fixture
def app():
    return fastapi_app


@pytest.fixture
async def client(app) -> AsyncClient:
    """ASGI 内存客户端：不监听端口、不访问数据库。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---- Day2：测试库与事务隔离 ----


@pytest.fixture(scope="session")
def _test_db_url_ready() -> str:
    url = _test_db_url()
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "kangban_test 迁移失败（确认库已创建：CREATE DATABASE kangban_test; "
            "\\c kangban_test; CREATE EXTENSION IF NOT EXISTS vector;）\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return url


@pytest.fixture
async def db_engine(_test_db_url_ready):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(_test_db_url_ready, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_connection(db_engine):
    """函数级事务：测试结束整体回滚，测试数据不落库。"""
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        yield conn
        await trans.rollback()


@pytest.fixture
async def db_session(db_connection):
    """绑定到测试事务的会话；应用内的 commit 释放 SAVEPOINT，不影响外层回滚。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    async with factory() as session:
        yield session


@pytest.fixture
async def api(db_session, app) -> AsyncClient:
    """覆盖 get_session 依赖的客户端：业务请求走 kangban_test 且随事务回滚。"""
    from app.db.session import get_session

    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def seed(db_session) -> dict:
    """函数级种子数据（随事务回滚）：两楼栋房间、设备、四角色账号、长者。"""
    from app.auth.passwords import hash_password
    from app.models import Account, Device, Elder, Persona, Room

    password_hash = _PASSWORD_HASH_CACHE.get(TEST_PASSWORD)
    if password_hash is None:
        password_hash = hash_password(TEST_PASSWORD)
        _PASSWORD_HASH_CACHE[TEST_PASSWORD] = password_hash

    room_s = Room(building_code="A", floor=1, room_number="101", bed_count=1)
    room_d = Room(building_code="A", floor=1, room_number="102", bed_count=2)
    room_other = Room(building_code="B", floor=2, room_number="201", bed_count=2)
    persona = Persona(code="persona-1", name="小伴", is_active=True)
    persona_off = Persona(code="persona-off", name="停用人设", is_active=False)
    db_session.add_all([room_s, room_d, room_other, persona, persona_off])
    await db_session.flush()

    dev_single = Device(
        device_code="TAB-SINGLE", room_id=room_s.room_id, status="active"
    )
    dev_double = Device(
        device_code="TAB-DOUBLE", room_id=room_d.room_id, status="active"
    )
    dev_free = Device(
        device_code="TAB-FREE", room_id=room_other.room_id, status="active"
    )
    db_session.add_all([dev_single, dev_double, dev_free])
    await db_session.flush()

    def _account(username: str, role: str, status: str = "active") -> Account:
        return Account(
            username=username,
            display_name=username,
            role=role,
            password_hash=password_hash,
            status=status,
        )

    accounts = {
        "admin": _account("admin", "admin"),
        "nurse_a": _account("nurse_a", "nurse"),
        "nurse_b": _account("nurse_b", "nurse"),
        "nurse_locked": _account("nurse_locked", "nurse", status="disabled"),
        "doctor": _account("doctor", "doctor"),
        "family": _account("family", "family"),
    }
    db_session.add_all(accounts.values())
    await db_session.flush()

    elder_single = Elder(
        name="王单间", persona_id=persona.persona_id,
        room_id=room_s.room_id, bed_no=1, status="active",
    )
    elder_d1 = Elder(
        name="李双一", persona_id=persona.persona_id,
        room_id=room_d.room_id, bed_no=1, status="active",
    )
    elder_d2 = Elder(
        name="赵双二", persona_id=persona.persona_id,
        room_id=room_d.room_id, bed_no=2, status="active",
    )
    elder_other = Elder(
        name="钱他楼", persona_id=persona.persona_id,
        room_id=room_other.room_id, bed_no=1, status="active",
    )
    db_session.add_all([elder_single, elder_d1, elder_d2, elder_other])
    await db_session.flush()

    return {
        "rooms": {"single": room_s, "double": room_d, "other": room_other},
        "devices": {"single": dev_single, "double": dev_double, "free": dev_free},
        "accounts": accounts,
        "elders": {
            "single": elder_single,
            "d1": elder_d1,
            "d2": elder_d2,
            "other": elder_other,
        },
        "personas": {"on": persona, "off": persona_off},
        "password": TEST_PASSWORD,
    }


async def login(api: AsyncClient, username: str, password: str) -> str:
    """登录并返回 access_token。"""
    resp = await api.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"seed 登录失败: {resp.status_code} {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture
async def auth_headers_factory(api):
    async def _factory(username: str, password: str = TEST_PASSWORD) -> dict:
        token = await login(api, username, password)
        return {"Authorization": f"Bearer {token}"}

    return _factory
