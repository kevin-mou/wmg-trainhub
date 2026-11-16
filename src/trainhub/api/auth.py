from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import Database
from ..passwords import hash_password, verify_password
from ..responses import ApiError, ok
from ..schemas import LoginRequest, PasswordUpdate
from ..serializers import customer_public
from ..utils import mask_token, normalize_username
from .deps import current_customer, get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _ensure_active(customer: dict) -> None:
    if customer["status"] != "active":
        raise ApiError("该账号已停用，请联系管理员", 403)


@router.post("/login")
def login(payload: LoginRequest, db: Database = Depends(get_db)) -> dict:
    """登录：访问令牌，或用户名 + 密码；成功后返回该客户的访问令牌。"""
    token = (payload.token or "").strip()
    if token:
        customer = db.get_customer_by_token(token)
        if customer is None:
            raise ApiError("访问令牌无效", 401)
        _ensure_active(customer)
        db.touch_customer(customer["id"])
        return ok({"token": customer["token"], "customer": customer_public(customer)})

    username = normalize_username(payload.username)
    password = payload.password or ""
    if not username or not password:
        raise ApiError("请输入用户名和密码；新账号请使用访问令牌登录", 400)
    customer = db.get_customer_by_username(username)
    if customer is None:
        raise ApiError("用户名或密码错误；新账号请使用访问令牌登录", 401)
    _ensure_active(customer)
    if not customer.get("password_hash"):
        raise ApiError("该账号尚未设置密码，请先使用访问令牌登录并设置密码", 401)
    if not verify_password(password, customer["password_hash"]):
        raise ApiError("用户名或密码错误", 401)
    db.touch_customer(customer["id"])
    return ok({"token": customer["token"], "customer": customer_public(customer)})


@router.get("/me")
def me(customer: dict = Depends(current_customer), db: Database = Depends(get_db)) -> dict:
    counts = db.run_status_counts(customer["id"])
    payload = customer_public(customer)
    payload["tokenMasked"] = mask_token(customer["token"])
    payload["runCounts"] = counts
    return ok(payload)


@router.put("/password")
def update_password(
    payload: PasswordUpdate,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """设置/更新当前客户密码；已设置过密码时必须校验当前密码。"""
    if customer.get("password_hash"):
        if not payload.currentPassword or not verify_password(
            payload.currentPassword, customer["password_hash"]
        ):
            raise ApiError("当前密码不正确", 400)
    try:
        encoded = hash_password(payload.newPassword)
    except ValueError as exc:
        raise ApiError(str(exc), 400) from exc
    updated = db.set_customer_password(customer["id"], encoded)
    if updated is None:
        raise ApiError("客户不存在", 404)
    return ok(
        {"username": updated.get("username"), "hasPassword": True},
        message="密码已更新",
    )
