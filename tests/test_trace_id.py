"""trace_id 中间件测试：合法透传、非法重生成、响应头回传。"""

import uuid

from httpx import AsyncClient


async def test_valid_trace_id_passthrough(client: AsyncClient):
    trace_id = str(uuid.uuid4())
    resp = await client.get("/health", headers={"X-Trace-ID": trace_id})
    assert resp.status_code == 200
    assert resp.headers["X-Trace-ID"] == trace_id
    assert resp.json()["trace_id"] == trace_id


async def test_invalid_trace_id_regenerated(client: AsyncClient):
    resp = await client.get("/health", headers={"X-Trace-ID": "not-a-uuid; DROP TABLE users"})
    assert resp.status_code == 200
    returned = resp.headers["X-Trace-ID"]
    # 非法值被替换为合法 UUID
    assert returned != "not-a-uuid; DROP TABLE users"
    uuid.UUID(returned)  # 不抛异常即为合法 UUID


async def test_missing_trace_id_generated(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    uuid.UUID(resp.headers["X-Trace-ID"])


async def test_trace_id_reachable_in_response_body(client: AsyncClient):
    """响应体中的 trace_id 与响应头一致，且绑定到 contextvars。"""
    resp = await client.get("/health")
    body = resp.json()
    assert body["trace_id"] == resp.headers["X-Trace-ID"]
