from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from ..config import Settings
from ..db import Database
from ..kernels import effective_default_kernel, effective_kernels, jupyter_available
from ..responses import ok
from ..serializers import customer_public, run_public
from ..storage import Storage
from ..utils import mask_token
from .deps import current_customer, get_db, get_settings, get_storage

router = APIRouter(tags=["overview"])


def _has_logs(storage: Storage, row: dict) -> bool:
    path = (
        Path(row["log_path"])
        if row.get("log_path")
        else storage.run_log_path(row["customer_id"], row["id"])
    )
    return path.is_file()


@router.get("/api/overview")
def overview(
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    counts = db.run_status_counts(customer["id"])
    # 单任务实例：GPU 全局独占。平台任务（sys_platform）与其它客户的任务同样占位，
    # 客户侧不能只看自己的队列，否则会把"被平台任务占用"误判成 GPU 空闲。
    global_counts = db.global_run_status_counts()
    global_active = global_counts.get("queued", 0) + global_counts.get("running", 0)
    limit = max(1, int(settings.max_concurrent_runs or 1))
    platform_active = db.count_active_runs(customer_id=Database.SYSTEM_CUSTOMER_ID)
    datasets_total = db.list_datasets(customer["id"], 1, 1, include_shared=False)[1]
    notebooks_total = db.list_notebooks(customer["id"], 1, 1)[1]
    succeeded = counts.get("succeeded", 0)
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    active = counts.get("queued", 0) + counts.get("running", 0)
    runs_total = sum(counts.values())
    decided = succeeded + failed
    success_rate = (succeeded / decided) if decided else None

    recent = db.recent_runs(customer["id"], limit=6)
    active_rows = db.active_runs(customer["id"])

    profile = customer_public(customer)
    profile["tokenMasked"] = mask_token(customer["token"])

    return ok(
        {
            "customer": profile,
            "executor": {
                "maxConcurrentRuns": limit,
                "activeRuns": global_active,
                "busy": global_active >= limit,
                "platformActiveRuns": platform_active,
            },
            "stats": {
                "datasets": datasets_total,
                "notebooks": notebooks_total,
                "runs": runs_total,
                "activeRuns": active,
                "succeeded": succeeded,
                "failed": failed,
                "cancelled": cancelled,
                "successRate": success_rate,
            },
            "storage": {
                "customerBytes": storage.dir_size(storage.customer_dir(customer["id"])),
                "disk": storage.disk_usage(),
            },
            "recentRuns": [run_public(row, has_logs=_has_logs(storage, row)) for row in recent],
            "activeRuns": [
                run_public(row, has_logs=_has_logs(storage, row)) for row in active_rows
            ],
        }
    )


@router.get("/api/kernels")
def kernels(
    customer: dict = Depends(current_customer), settings: Settings = Depends(get_settings)
) -> dict:
    available = effective_kernels(settings)
    return ok(
        {
            "kernels": available,
            "default": effective_default_kernel(settings),
            "jupyterAvailable": jupyter_available(),
        }
    )
