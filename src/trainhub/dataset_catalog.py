"""执行期数据集清单：把「可见数据集」解析好的绝对路径交给内核。

数据集在磁盘上按「所有者 + 数据集 ID」存放
（``<data>/customers/<owner>/datasets/<dataset_id>/files``）：

- 名称不是目录名，也不保证全局唯一；
- 共享数据集的文件目录属于所有者，凭当前客户 ID 拼路径必然错。

因此清单由服务端生成并写入会话/任务的 work 目录，运行时助手
（``trainhub_runtime.load_dataset`` / ``dataset_path``）只按名称选条目、直接使用条目里的
绝对路径，不在内核侧重新拼路径。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .storage import Storage

CATALOG_DIR = ".trainhub"
CATALOG_FILENAME = "datasets.json"
CATALOG_VERSION = 1


def dataset_catalog_path(work_dir: Path) -> Path:
    return Path(work_dir) / CATALOG_DIR / CATALOG_FILENAME


def dataset_entry(
    *,
    name: str,
    directory: str | Path,
    dataset_id: str | None = None,
    owner_id: str = "",
    owner_name: str = "",
    shared: bool = False,
    selected: bool = False,
    file_count: int = 0,
    size_bytes: int = 0,
) -> dict[str, Any]:
    """单条数据集描述；``dir`` 已是服务端解析好的绝对路径。"""
    return {
        "name": str(name or "").strip(),
        "dataset_id": (str(dataset_id).strip() if dataset_id else None) or None,
        "owner_id": str(owner_id or ""),
        "owner_name": str(owner_name or ""),
        # shared 表示「来自其他客户」，与前端 datasetShared 的口径一致；
        # 不是数据集自身的 visibility 字段。
        "shared": bool(shared),
        "selected": bool(selected),
        "file_count": int(file_count or 0),
        "size_bytes": int(size_bytes or 0),
        "dir": str(Path(directory)),
    }


def entries_from_rows(
    rows: list[dict[str, Any]],
    storage: Storage,
    *,
    customer_id: str,
    selected_id: str | None = None,
) -> list[dict[str, Any]]:
    entries = []
    for row in rows:
        dataset_id = str(row.get("id") or "")
        owner_id = str(row.get("customer_id") or "")
        if not dataset_id or not owner_id:
            continue
        entries.append(
            dataset_entry(
                name=str(row.get("name") or dataset_id),
                directory=storage.dataset_files_dir(owner_id, dataset_id),
                dataset_id=dataset_id,
                owner_id=owner_id,
                owner_name=str(row.get("owner_name") or ""),
                shared=owner_id != customer_id,
                selected=dataset_id == selected_id,
                file_count=int(row.get("file_count") or 0),
                size_bytes=int(row.get("size_bytes") or 0),
            )
        )
    return entries


def accessible_dataset_entries(
    db: Any,
    storage: Storage,
    customer_id: str,
    *,
    selected_id: str | None = None,
) -> list[dict[str, Any]]:
    """当前客户可见数据集（自有 + 共享）的清单条目；路径按所有者解析。"""
    rows = db.list_accessible_datasets(customer_id)
    return entries_from_rows(rows, storage, customer_id=customer_id, selected_id=selected_id)


def write_dataset_catalog(
    work_dir: Path, *, customer_id: str, entries: list[dict[str, Any]]
) -> Path:
    """原子写入数据集清单；内容未变化时不重写（每次执行单元格都会刷新）。"""
    path = dataset_catalog_path(work_dir)
    payload = {
        "version": CATALOG_VERSION,
        "customer_id": customer_id,
        "datasets": list(entries),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return path
        except OSError:
            pass
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path
