from __future__ import annotations

from typing import Any

from fastapi import Request

from ..config import Settings
from ..db import Database
from ..responses import ApiError
from ..runner import RunExecutor
from ..security import constant_time_equal, extract_admin_token, extract_request_token
from ..storage import Storage


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_executor(request: Request) -> RunExecutor:
    return request.app.state.executor


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_customer(request: Request) -> dict[str, Any]:
    """把请求头里的令牌解析为具体客户；所有业务接口都依赖它。"""
    db = get_db(request)
    token = extract_request_token(request.headers)
    if not token:
        raise ApiError("缺少访问令牌：请在请求头携带 Authorization: Bearer <token>", 401)
    customer = db.get_customer_by_token(token)
    if customer is None:
        raise ApiError("访问令牌无效", 401)
    if customer["status"] != "active":
        raise ApiError("该账号已停用，请联系管理员", 403)
    db.touch_customer(customer["id"])
    return customer


def current_admin(request: Request) -> None:
    settings = get_settings(request)
    if not settings.admin_token:
        raise ApiError("管理接口未启用（未配置 TRAINHUB_ADMIN_TOKEN）", 503)
    provided = extract_admin_token(request.headers) or extract_request_token(request.headers)
    if not constant_time_equal(provided, settings.admin_token):
        raise ApiError("管理令牌无效", 401)
