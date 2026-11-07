"""创建首个管理员账号（幂等）：仅当系统中不存在 admin 角色账号时创建。

用法：
    python scripts/seed_admin.py
用户名与密码从交互输入或环境变量 SEED_ADMIN_USERNAME / SEED_ADMIN_PASSWORD 读取；
密码交互输入不回显，任何情况下不落日志。
"""

import asyncio
import getpass
import os
import sys

from sqlalchemy import func, select

from app.auth.passwords import hash_password
from app.db.session import get_engine, get_session_factory
from app.models import Account
from app.models.enums import AccountRole, AccountStatus

MIN_PASSWORD_LENGTH = 8


async def seed_admin() -> int:
    engine = get_engine()
    factory = get_session_factory()
    try:
        async with factory() as session:
            admin_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Account)
                    .where(Account.role == AccountRole.ADMIN)
                )
            ).scalar_one()
            if admin_count > 0:
                print("已存在管理员账号，跳过创建（幂等）。")
                return 0

            username = os.environ.get("SEED_ADMIN_USERNAME") or input("管理员用户名: ").strip()
            if not username or len(username) > 64:
                print("错误：用户名不能为空且不超过 64 字符。")
                return 1

            password = os.environ.get("SEED_ADMIN_PASSWORD") or getpass.getpass("管理员密码: ")
            if len(password) < MIN_PASSWORD_LENGTH:
                print(f"错误：密码长度不得少于 {MIN_PASSWORD_LENGTH} 位。")
                return 1

            account = Account(
                username=username,
                display_name=username,
                role=AccountRole.ADMIN,
                password_hash=hash_password(password),
                status=AccountStatus.ACTIVE,
            )
            session.add(account)
            await session.commit()
            print(f"管理员账号已创建：{username}（account_id={account.account_id}）")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    try:
        sys.exit(asyncio.run(seed_admin()))
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)


if __name__ == "__main__":
    main()
