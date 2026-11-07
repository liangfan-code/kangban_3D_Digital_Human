"""密码哈希与校验：bcrypt，cost >= 12。

密码明文只存在于函数参数与内存中，禁止写入日志、响应、异常信息或任何持久化位置。
"""

import bcrypt

_BCRYPT_ROUNDS = 12


def hash_password(plain_password: str) -> str:
    """生成 bcrypt 哈希（自动生成盐，cost=12），返回可直接入库的字符串。"""
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS))
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码与库内哈希是否匹配；哈希格式非法时一律返回 False（不泄露差异）。"""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False
