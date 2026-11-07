"""Day2 纯函数演示：密码哈希（passwords.py）+ JWT 签发与验签（jwt.py）。

使用方法：在 PyCharm 中直接右键本文件 → Run，无需启动服务、无需连接数据库。
本文件演示的都是"不依赖数据库"的纯函数；依赖数据库的设备凭证请看 demo_day2_接口.py。
"""

import os
import sys
from pathlib import Path

# ---- 让本文件无论从哪里启动都能找到项目根目录和 .env ----
PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)                    # config.py 用相对路径 ".env" 读配置，必须先切到项目根
sys.path.insert(0, str(PROJECT_ROOT))     # 保证 import app.* 成功


def demo_1_密码哈希() -> None:
    """演示 hash_password / verify_password：为什么库里只存哈希也安全。"""
    from app.auth.passwords import hash_password, verify_password

    print("=" * 60)
    print("演示 1：密码哈希与校验（app/auth/passwords.py）")
    print("=" * 60)

    h1 = hash_password("demo12345")          # 把明文密码变成 bcrypt 哈希（这就是入库的那串字符）
    print("第一次哈希:", h1)

    # 哈希串的结构：$2b$12$ 是"算法+轮次"标记，后面 22 位是盐，剩下 31 位是真正的哈希值
    print("算法标记:", h1[:7], "   ← $2b$ = bcrypt 第 2b 版，12 = 计算 2^12 轮（故意算得慢，防暴力破解）")
    print("盐(随机):", h1[7:29], "  ← 每次哈希都随机生成，所以同一密码两次哈希结果不同")
    print("哈希值  :", h1[29:], "  ← 盐+明文 一起算出来的最终值")

    h2 = hash_password("demo12345")          # 同一个密码再哈希一次
    print("第二次哈希:", h2)
    print("两次结果相同吗:", h1 == h2, "  ← 必须是 False！盐随机 → 即使库泄露，攻击者也看不出谁和谁用同一密码")

    ok = verify_password("demo12345", h1)    # 校验"正确的密码"
    print("正确密码校验:", ok, "          ← True = 明文+库里的盐 重算一遍，结果对得上")

    bad = verify_password("wrong123", h1)    # 校验"错误的密码"
    print("错误密码校验:", bad, "          ← False = 重算结果对不上，登录时就是靠它返回 401")

    junk = verify_password("demo12345", "这不是合法哈希")
    print("垃圾哈希校验:", junk, "          ← 也是 False 而不是报错崩溃：格式非法不泄露任何信息")

    print("💡 为什么这样做：数据库只存哈希不存明文 → 库泄露了攻击者也拿不到原密码；")
    print("   校验时不比较明文，而是'用同样的盐再算一遍看结果是否一致'。")


def demo_2_jwt签发与验签() -> None:
    """演示 create_user_access_token / decode_token：Token 里到底装了什么、验什么。"""
    from app.auth.jwt import (
        DEVICE_TOKEN_TYPE,
        USER_TOKEN_TYPE,
        TokenTypeMismatch,
        create_user_access_token,
        decode_token,
    )
    from app.core.config import get_settings

    print()
    print("=" * 60)
    print("演示 2：JWT 签发与验签（app/auth/jwt.py）")
    print("=" * 60)

    if not get_settings().jwt_secret_key:
        print("⚠️ 检测到 .env 中 JWT_SECRET_KEY 为空，本节无法演示。")
        print("   补齐方法：终端执行 python -c \"import secrets; print(secrets.token_hex(32))\"，")
        print("   把输出填进 .env 的 JWT_SECRET_KEY= 后面，重新运行本文件。")
        return

    token = create_user_access_token("00000000-0000-0000-0000-000000000001", "admin")
    print("签发的人 JWT（前 50 字符）:", token[:50] + "...", "  ← eyJ 开头是 JWT 固定格式：头.载荷.签名")

    payload = decode_token(token, USER_TOKEN_TYPE)   # 验签 + 校验类型，成功返回载荷字典
    print("验签成功，载荷内容：")
    print("  sub        =", payload["sub"], " ← 这个 Token 属于哪个账号（account_id）")
    print("  role       =", payload["role"], "            ← 角色，后面 require_role('admin') 靠它判 403")
    print("  token_type =", payload["token_type"], "         ← 'user'：人 Token 的身份证，和设备 Token 互斥")
    print("  iat / exp  =", payload["iat"], "/", payload["exp"], " ← 签发/过期时间（Unix 时间戳），相差默认 60 分钟")
    print("  jti        =", payload["jti"], "  ← 唯一编号，方便日志追踪")

    # ---- 伪造攻击演示：改最后两个字符，签名就对不上了 ----
    fake = token[:-2] + "xx"
    try:
        decode_token(fake, USER_TOKEN_TYPE)
        print("伪造 Token 验签: 意外通过（不应该发生！）")
    except Exception as exc:
        print("伪造 Token 验签: 抛出", type(exc).__name__, "  ← 签名对不上 → 依赖层统一映射为 HTTP 401")
        print("   💡 为什么报错信息不含具体原因：统一 401 不让攻击者知道'是密钥错还是过期了'")

    # ---- 类型互斥演示：人 Token 冒充设备 Token ----
    try:
        decode_token(token, DEVICE_TOKEN_TYPE)
        print("类型冒充: 意外通过（不应该发生！）")
    except TokenTypeMismatch:
        print("人 Token 冒充设备 Token: 抛出 TokenTypeMismatch  ← 映射为 HTTP 403（有权限含义，和 401 区分）")
        print("   💡 为什么是 403 不是 401：Token 本身是合法的，只是'人证'拿去刷'设备门'——")
        print("      ADR-005：人账号和设备两套身份严格分离，互相冒用直接拒绝。")


def main() -> None:
    print("Day2 纯函数演示开始（不需要启动服务）\n")
    demo_1_密码哈希()
    demo_2_jwt签发与验签()
    print()
    print("=" * 60)
    print("演示结束。下一步：启动服务后运行 demo_day2_接口.py 看真实 HTTP 响应。")


if __name__ == "__main__":
    main()
