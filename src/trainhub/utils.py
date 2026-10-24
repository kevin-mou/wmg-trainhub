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
