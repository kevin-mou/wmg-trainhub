from __future__ import annotations

import secrets
from datetime import datetime, timezone


def utc_now() -> str:
    """当前 UTC 时间（ISO 8601，带 +00:00 时区）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def new_id(prefix: str) -> str:
    """生成短 ID，如 c_3f9a1c2b4d5e。"""
    return prefix + secrets.token_hex(6)


def new_customer_id() -> str:
    return "c_" + secrets.token_hex(4)


def new_customer_token() -> str:
    return "trh_" + secrets.token_urlsafe(32)


def mask_token(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 14:
        return token[:4] + "…"
    return f"{token[:8]}…{token[-4:]}"


def truncate(text: str | None, limit: int = 1000) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


MAX_USERNAME_LENGTH = 120


def normalize_username(value: str | None) -> str:
    """登录用户名归一化：去首尾空白、大小写折叠、去掉不可打印字符。

    以归一化后的值落库/查询，保证 SQLite 上大小写不敏感的唯一性。
    """
    text = str(value or "").strip().casefold()
    return "".join(char for char in text if char.isprintable())
