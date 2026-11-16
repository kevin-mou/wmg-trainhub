"""客户账号密码的哈希与校验（仅标准库，训练节点无需额外依赖）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def hash_password(password: str) -> str:
    value = str(password)
    if not value.strip() or not (MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH):
        raise ValueError(f"密码长度必须为 {MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, ITERATIONS)
    return "$".join(
        (
            ALGORITHM,
            str(ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str | None, encoded: str | None) -> bool:
    if not password or not encoded:
        return False
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        rounds = int(iterations)
        if rounds < 1 or rounds > 1_000_000:
            return False
        salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
        expected = base64.b64decode(digest_text.encode("ascii"), validate=True)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def password_is_set(encoded: str | None) -> bool:
    return bool(encoded)
