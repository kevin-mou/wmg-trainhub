from __future__ import annotations

import secrets
from typing import Any


def extract_request_token(headers: Any) -> str | None:
    """从 Authorization: Bearer 或 X-API-Token 中取令牌。"""
    authorization = headers.get("authorization")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    token = headers.get("x-api-token")
    if token and token.strip():
        return token.strip()
    return None


def extract_admin_token(headers: Any) -> str | None:
    token = headers.get("x-admin-token")
    if token and token.strip():
        return token.strip()
    return None


def constant_time_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return secrets.compare_digest(left, right)


__all__ = [
    "constant_time_equal",
    "extract_admin_token",
    "extract_request_token",
]
