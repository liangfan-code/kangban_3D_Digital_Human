"""Day2 接口演示：真实 HTTP 请求走一遍 Day2 认证链路（登录→自查→三类攻击全部被拦截）。

使用方法：
  第 1 步：终端启动服务并保持运行：python -m uvicorn app.main:app --reload
  第 2 步：在 PyCharm 中右键本文件 → Run，按控制台提示输入管理员用户名和密码。
前置条件：Docker Desktop 已开（kangban-pg 运行中）、.env 已填 JWT_SECRET_KEY。
"""

import getpass

import httpx

BASE_URL = "http://127.0.0.1:8000"

passed: list[str] = []
failed: list[str] = []


def check(名称: str, 实际: int, 期望: int, 说明: str) -> None:
    """统一打印每一步的结果对比，并记录通过/失败。"""
    mark = "✓ 通过" if 实际 == 期望 else "✗ 未通过"
    print(f"  实际 {实际} / 期望 {期望} → {mark}")
    print(f"  💡 {说明}\n")
    (passed if 实际 == 期望 else failed).append(名称)


def main() -> None:
    client = httpx.Client(base_url=BASE_URL, timeout=10)

    # ---- 第 0 步：确认服务已启动 ----
    print("=" * 60)
    print("第 0 步：健康检查 /health")
    print("=" * 60)
    try:
        resp = client.get("/health")
    except httpx.ConnectError:
        print("✗ 连不上服务。请先在终端执行（保持窗口不关）：")
        print("    python -m uvicorn app.main:app --reload")
        print("  看到成功后重新运行本文件。")
        return
    print(f"  实际 {resp.status_code} / 期望 200 → 服务正常\n")

    # ---- 第 1 步：正确登录 ----
    print("=" * 60)
    print("第 1 步：正确登录 POST /api/v1/auth/login")
    print("=" * 60)
    username = input("管理员用户名: ").strip()
    try:
        password = getpass.getpass("管理员密码(输入不回显): ")
    except Exception:                      # 某些控制台不支持不回显时降级
        password = input("管理员密码: ")
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        print(f"  实际 {resp.status_code} / 期望 200 → 登录失败：{resp.json().get('detail')}")
        print("  💡 用户名密码错误返回 401 且不区分'用户不存在/密码错'——本演示结束，请核对后重跑。")
        return
    token = resp.json()["access_token"]
    print(f"  实际 {resp.status_code} / 期望 200 → ✓ 通过")
    print(f"  拿到 Token（前 40 字符）: {token[:40]}...   ← 之后所有请求都靠它自证身份")
    print(f"  角色: {resp.json()['role']} / 有效期: {resp.json()['expires_in']} 秒\n")
    passed.append("登录")

    headers = {"Authorization": f"Bearer {token}"}

    # ---- 第 2 步：身份自查 ----
    print("=" * 60)
    print("第 2 步：身份自查 GET /api/v1/auth/me")
    print("=" * 60)
    resp = client.get("/api/v1/auth/me", headers=headers)
    check("auth/me", resp.status_code, 200,
          "服务端从 Token 里解出 sub 再查库确认账号存在且启用——身份由可信上下文注入，不信请求里自报的字段")
    if resp.status_code == 200:
        for k, v in resp.json().items():
            print(f"    {k} = {v}")
    print()

    # ---- 第 3 步：攻击 1 —— 错误密码（暴力破解尝试）----
    print("=" * 60)
    print("第 3 步：攻击用例-错误密码（暴力破解尝试）")
    print("=" * 60)
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": "肯定错的密码123"})
    check("错误密码拦截", resp.status_code, 401,
          "401 且提示'用户名或密码错误'——故意不区分是哪个错，攻击者无法用它探测系统里有哪些账号")

    # ---- 第 4 步：攻击 2 —— 伪造 Token（攻击用例 A1）----
    print("=" * 60)
    print("第 4 步：攻击用例 A1 伪造 Token GET /api/v1/auth/me")
    print("=" * 60)
    fake_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJoYWNrZXIifQ.伪造签名"
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {fake_token}"})
    check("伪造 Token 拦截", resp.status_code, 401,
          "签名验不过直接 401——没有 JWT_SECRET_KEY 就伪造不出合法签名，这是 JWT 安全性的根基")

    # ---- 第 5 步：攻击 3 —— 人 Token 冒充设备身份（类型互斥）----
    print("=" * 60)
    print("第 5 步：攻击用例-人 Token 调设备接口 GET /api/v1/devices/me")
    print("=" * 60)
    resp = client.get("/api/v1/devices/me", headers=headers)
    check("类型互斥拦截", resp.status_code, 403,
          "Token 本身合法但类型不对：'人证'刷'设备门' → 403（有权限含义）；ADR-005 人/设备身份严格分离")

    # ---- 第 6 步：无 Token 访问受保护接口 ----
    print("=" * 60)
    print("第 6 步：无 Token 访问 GET /api/v1/elders")
    print("=" * 60)
    resp = client.get("/api/v1/elders")
    check("未认证拦截", resp.status_code, 401,
          "受保护接口没带 Token → 401，任何业务数据都不返回")

    # ---- 汇总 ----
    print("=" * 60)
    print(f"汇总：{len(passed)} 项通过，{len(failed)} 项未通过")
    print("=" * 60)
    for name in passed:
        print("  ✓", name)
    for name in failed:
        print("  ✗", name)
    if not failed:
        print("\n全部符合预期。完整 CRUD/设备绑定/三张关联表验证见: python -m pytest -q")
    client.close()


if __name__ == "__main__":
    main()
