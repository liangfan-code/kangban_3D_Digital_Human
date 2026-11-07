"""/health 路由测试。"""

import uuid

from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app_env"]
    # 响应头回传 trace_id
    assert resp.headers.get("X-Trace-ID")
    assert body["trace_id"] == resp.headers["X-Trace-ID"]


async def test_health_does_not_touch_database(client: AsyncClient):
    """存活检查不访问数据库：无 DB 连接时依然 200（由 ASGI 内存客户端保证）。"""
    resp = await client.get("/health")
    assert resp.status_code == 200


async def test_health_with_valid_trace_id_passthrough(client: AsyncClient):
    trace_id = str(uuid.uuid4())
    resp = await client.get("/health", headers={"X-Trace-ID": trace_id})
    assert resp.status_code == 200
    assert resp.headers["X-Trace-ID"] == trace_id
    assert resp.json()["trace_id"] == trace_id
