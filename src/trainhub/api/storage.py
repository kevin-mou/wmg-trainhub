from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..db import Database
from ..responses import ok
from ..storage import Storage
from .deps import current_customer, get_db, get_storage

router = APIRouter(tags=["storage"])

# 明细只展示占用最大的若干项；完整列表由数据集 / Notebooks / 运行页承担。
ITEM_LIMIT = 50
# 单客户条目通常不多；给列表查询一个显式上限，避免异常数据把一次请求拖垮。
LIST_LIMIT = 10_000


def _top(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, bool]:
    items.sort(key=lambda item: (-int(item.get("bytes") or 0), str(item.get("name") or "")))
    return items[:ITEM_LIMIT], len(items), len(items) > ITEM_LIMIT


@router.get("/api/storage")
def storage_usage(
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    """客户目录占用与磁盘空间（概览「存储」统计项的详情页数据源）。"""
    customer_id = customer["id"]
    root = storage.customer_dir(customer_id)

    dataset_rows, dataset_count = db.list_datasets(customer_id, 1, LIST_LIMIT, include_shared=False)
    notebook_rows, notebook_count = db.list_notebooks(customer_id, 1, LIST_LIMIT)
    run_rows, run_count = db.list_runs(customer_id, 1, LIST_LIMIT)

    datasets = [
        {
            "id": row["id"],
            "name": row["name"],
            "bytes": storage.dir_size(storage.dataset_dir(customer_id, row["id"])),
            "fileCount": int(row.get("file_count") or 0),
            "createdAt": row.get("created_at"),
        }
        for row in dataset_rows
    ]
    notebooks = [
        {
            "id": row["id"],
            "name": row["name"],
            "bytes": storage.dir_size(storage.notebook_dir(customer_id, row["id"])),
            "cellCount": int(row.get("cell_count") or 0),
            "updatedAt": row.get("updated_at"),
        }
        for row in notebook_rows
    ]
    runs = [
        {
            "id": row["id"],
            "name": row["name"],
            "status": row.get("status"),
            "notebookName": row.get("notebook_name"),
            "bytes": storage.dir_size(storage.run_dir(customer_id, row["id"])),
            "createdAt": row.get("created_at"),
        }
        for row in run_rows
    ]

    dataset_bytes = storage.dir_size(root / "datasets")
    notebook_bytes = storage.dir_size(root / "notebooks")
    run_bytes = storage.dir_size(root / "runs")
    customer_bytes = storage.dir_size(root)
    other_bytes = max(0, customer_bytes - dataset_bytes - notebook_bytes - run_bytes)

    def details(items: list[dict[str, Any]]) -> dict[str, Any]:
        top, total, truncated = _top(items)
        return {"items": top, "total": total, "truncated": truncated}

    return ok(
        {
            "customerBytes": customer_bytes,
            "disk": storage.disk_usage(),
            "categories": [
                {"key": "datasets", "bytes": dataset_bytes, "count": dataset_count},
                {"key": "notebooks", "bytes": notebook_bytes, "count": notebook_count},
                {"key": "runs", "bytes": run_bytes, "count": run_count},
                {"key": "other", "bytes": other_bytes, "count": 0},
            ],
            "details": {
                "datasets": details(datasets),
                "notebooks": details(notebooks),
                "runs": details(runs),
            },
        }
    )
