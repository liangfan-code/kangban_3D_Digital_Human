"""登录与 /auth/me 人身份测试（Day2 文档 6.4 test_auth_login.py 行）。"""

from datetime import UTC, datetime, timedelta

import jwt as pyjwt


async def test_login_all_roles_200(api, seed):
    for username in ("admin", "nurse_a", "doctor", "family"):
        resp = await api.post(
            "/api/v1/auth/login",
            json={"username": username, "password": seed["password"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_token"] and body["token_type"] == "bearer"


async def test_login_wrong_password_401(api, seed):
    ok = await api.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "bad-password"}
    )
    missing = await api.post(
        "/api/v1/auth/login", json={"username": "no_such_user", "password": "bad-password"}
    )
    assert ok.status_code == 401
    assert missing.status_code == 401
    # 不泄露差异：用户不存在与密码错误的响应体一致（trace_id 每请求随机，剔除后比较）
    body_a = ok.json()["error"]
    body_b = missing.json()["error"]
    body_a.pop("trace_id", None)
    body_b.pop("trace_id", None)
    assert body_a == body_b


async def test_login_disabled_account_401(api, seed):
    resp = await api.post(
        "/api/v1/auth/login",
        json={"username": "nurse_locked", "password": seed["password"]},
    )
    assert resp.status_code == 401


async def test_forged_token_401(api, seed):
    for token in ("not-a-jwt", "eyJhbGciOiJIUzI1NiJ9.forged.sig"):
        resp = await api.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401


async def test_expired_jwt_401(api, seed):
    from app.core.config import get_settings

    payload = {
        "sub": str(seed["accounts"]["admin"].account_id),
        "role": "admin",
        "token_type": "user",
        "exp": datetime.now(UTC) - timedelta(minutes=1),
    }
    token = pyjwt.encode(payload, get_settings().jwt_secret_key, algorithm="HS256")
    resp = await api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_me_user_identity(api, seed, auth_headers_factory):
    headers = await auth_headers_factory("admin")
    resp = await api.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["identity_type"] == "user"
    assert body["role"] == "admin"
    assert str(body["account_id"]) == str(seed["accounts"]["admin"].account_id)


async def test_me_without_token_401(api):
    assert (await api.get("/api/v1/auth/me")).status_code == 401


async def test_user_token_on_device_endpoint_403(api, seed, auth_headers_factory):
    headers = await auth_headers_factory("admin")
    resp = await api.get("/api/v1/devices/me", headers=headers)
    assert resp.status_code == 403
