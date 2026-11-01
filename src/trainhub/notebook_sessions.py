from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

import nbformat

from .db import Database
from .responses import ApiError

_MIGRATION_LOCK = threading.Lock()


def source_text(cell: Any) -> str:
    source = cell.get("source") if hasattr(cell, "get") else None
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    return str(source or "")


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def ensure_cell_ids(node: nbformat.NotebookNode) -> bool:
    changed = False
    seen: set[str] = set()
    for cell in node.cells:
        cell_id = str(cell.get("id") or "")
        if not cell_id or cell_id in seen:
            cell_id = secrets.token_hex(8)
            cell["id"] = cell_id
            changed = True
        seen.add(cell_id)
    if changed:
        node["nbformat_minor"] = max(int(node.get("nbformat_minor", 5) or 5), 5)
    return changed


def strip_outputs(node: nbformat.NotebookNode) -> bool:
    changed = False
    for cell in node.cells:
        if cell.get("cell_type") != "code":
            if "outputs" in cell or "execution_count" in cell:
                cell.pop("outputs", None)
                cell.pop("execution_count", None)
                changed = True
            continue
        if cell.get("outputs"):
            changed = True
        if cell.get("execution_count") is not None:
            changed = True
        cell["outputs"] = []
        cell["execution_count"] = None
    return changed


def read_notebook(path: Path) -> nbformat.NotebookNode:
    if path.is_symlink() or not path.is_file():
        raise ApiError("Notebook 源文件缺失", 404)
    try:
        return nbformat.reads(path.read_text(encoding="utf-8"), as_version=4)
    except Exception as exc:
        raise ApiError(f"Notebook 内容不合法，无法编辑：{exc}", 422) from exc


def write_notebook_atomic(path: Path, node: nbformat.NotebookNode) -> bytes:
    raw = nbformat.writes(node).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_bytes(raw)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return raw


def script_payload(node: nbformat.NotebookNode) -> dict[str, Any]:
    payload = json.loads(nbformat.writes(node))
    for cell in payload.get("cells", []):
        source = cell.get("source")
        if isinstance(source, list):
            cell["source"] = "".join(str(part) for part in source)
    return payload


def extract_session_cells(node: nbformat.NotebookNode) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for cell in node.cells:
        if cell.get("cell_type") != "code":
            continue
        outputs = list(cell.get("outputs") or [])
        execution_count = cell.get("execution_count")
        if not outputs and execution_count is None:
            continue
        source = source_text(cell)
        cells.append(
            {
                "cellId": str(cell.get("id") or ""),
                "sourceHash": source_hash(source),
                "executionCount": execution_count,
                "outputs": json.loads(json.dumps(outputs, ensure_ascii=False)),
            }
        )
    return cells


def merge_session_cells(node: nbformat.NotebookNode, cells: list[dict[str, Any]]) -> None:
    by_id = {
        str(item.get("cellId")): item
        for item in cells
        if isinstance(item, dict) and item.get("cellId")
    }
    for cell in node.cells:
        if cell.get("cell_type") != "code":
            continue
        entry = by_id.get(str(cell.get("id") or ""))
        if not entry:
            continue
        if entry.get("sourceHash") != source_hash(source_text(cell)):
            continue
        outputs = entry.get("outputs")
        cell["outputs"] = nbformat.from_dict(outputs) if isinstance(outputs, list) else []
        execution_count = entry.get("executionCount")
        cell["execution_count"] = execution_count if isinstance(execution_count, int) else None


def ensure_script_notebook(
    *, db: Database, customer_id: str, notebook: dict[str, Any]
) -> nbformat.NotebookNode:
    """把磁盘 Notebook 收敛为纯脚本；必要时把旧 outputs 迁移为 import/legacy 快照。"""
    path = Path(notebook["storage_path"])
    with _MIGRATION_LOCK:
        node = read_notebook(path)
        ids_changed = ensure_cell_ids(node)
        legacy_cells = extract_session_cells(node)
        outputs_changed = strip_outputs(node)
        if legacy_cells and db.get_notebook_session(customer_id, notebook["id"]) is None:
            db.upsert_notebook_session(
                customer_id,
                notebook["id"],
                origin="legacy",
                cells=legacy_cells,
            )
        if ids_changed or outputs_changed:
            raw = write_notebook_atomic(path, node)
            db.set_notebook_size(customer_id, notebook["id"], len(raw))
        return node
