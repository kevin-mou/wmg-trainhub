from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import Database
from ..responses import ok, page_data
from ..serializers import activity_public
from .deps import current_customer, get_db

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
def list_activity(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_activity(customer["id"], page, size)
    return ok(page_data([activity_public(row) for row in rows], total, page, size))
