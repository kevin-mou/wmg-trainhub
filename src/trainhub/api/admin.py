from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import Database
from ..responses import ApiError, ok, page_data
from ..schemas import CustomerCreate, CustomerStatusUpdate
from ..serializers import customer_public
from ..storage import Storage
from ..utils import new_customer_token
from .deps import current_admin, get_db, get_storage

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(current_admin)])


@router.get("/overview")
def admin_overview(db: Database = Depends(get_db)) -> dict:
    return ok(db.admin_overview())


@router.get("/customers")
def list_customers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, max_length=120),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_customers(page, size, q)
    records = [customer_public(row, include_token=True) for row in rows]
    return ok(page_data(records, total, page, size))


@router.post("/customers")
def create_customer(payload: CustomerCreate, db: Database = Depends(get_db)) -> dict:
    customer = db.create_customer(payload.name, payload.email, new_customer_token())
    return ok(customer_public(customer, include_token=True), message="客户已创建", code=201)


@router.post("/customers/{customer_id}/rotate-token")
def rotate_token(customer_id: str, db: Database = Depends(get_db)) -> dict:
    customer = db.get_customer(customer_id)
    if customer is None:
        raise ApiError("客户不存在", 404)
    updated = db.update_customer_token(customer_id, new_customer_token())
    return ok(customer_public(updated, include_token=True), message="访问令牌已重置")


@router.post("/customers/{customer_id}/status")
def update_status(
    customer_id: str, payload: CustomerStatusUpdate, db: Database = Depends(get_db)
) -> dict:
    customer = db.get_customer(customer_id)
    if customer is None:
        raise ApiError("客户不存在", 404)
    updated = db.update_customer_status(customer_id, payload.status)
    message = "客户已启用" if payload.status == "active" else "客户已停用"
    return ok(customer_public(updated, include_token=True), message=message)


@router.delete("/customers/{customer_id}")
def delete_customer(
    customer_id: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    customer = db.get_customer(customer_id)
    if customer is None:
        raise ApiError("客户不存在", 404)
    if db.count_active_runs(customer_id=customer_id):
        raise ApiError("该客户仍有排队或执行中的训练任务，暂时无法删除", 409)
    db.delete_customer(customer_id)
    storage.remove_tree(storage.customer_dir(customer_id))
    return ok({"id": customer_id}, message="客户及其全部数据已删除")
