from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import Database
from ..responses import ok
from ..serializers import customer_public
from ..utils import mask_token
from .deps import current_customer, get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me")
def me(customer: dict = Depends(current_customer), db: Database = Depends(get_db)) -> dict:
    counts = db.run_status_counts(customer["id"])
    payload = customer_public(customer)
    payload["tokenMasked"] = mask_token(customer["token"])
    payload["runCounts"] = counts
    return ok(payload)
