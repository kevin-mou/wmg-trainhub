from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from .config import Settings
from .db import TERMINAL_RUN_STATUSES, Database
from .kernels import jupyter_available, list_kernels
from .metrics import DEFAULT_MAX_POINTS, read_metrics_file
from .notebook_sessions import (
    ensure_cell_ids,
    ensure_script_notebook,
    extract_session_cells,
    merge_session_cells,
    script_payload,
    source_hash,
    source_text,
    strip_outputs,
)
from .responses import ApiError
from .runner import scan_outputs
from .storage import (
    Storage,
    build_zip,
    extract_zip_safe,
    iter_files,
    resolve_within,
    safe_filename,
    safe_relative_path,
    strip_common_root,
    unique_relative_path,
)
from .utils import truncate

logger = logging.getLogger("trainhub.services")

CHUNK_SIZE = 1024 * 1024


# ---------------------------------------------------------------------- datasets


async def create_dataset(
    *,
    db: Database,
    storage: Storage,
    settings: Settings,
    customer: dict[str, Any],
    name: str | None,
    description: str | None,
    uploads: list[UploadFile],
    shared: bool = False,
    ip: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not uploads:
        raise ApiError("请至少选择一个文件", 400)

    raw_paths = [safe_relative_path(item.filename) for item in uploads]
    filenames, common_root = strip_common_root(raw_paths)
    display_name = (name or "").strip()
    if not display_name:
        if common_root:
            display_name = common_root
        elif len(filenames) == 1:
            display_name = Path(filenames[0]).stem
        else:
            display_name = _timestamped_name("dataset")
    display_name = truncate(display_name, 120) or _timestamped_name("dataset")

    customer_id = customer["id"]
    dataset_id = "ds_" + secrets.token_hex(6)
    files_dir = storage.dataset_files_dir(customer_id, dataset_id)
    files_dir.mkdir(parents=True, exist_ok=True)

    saved: list[tuple[str, int, str]] = []
    total_bytes = 0
    used: set[str] = set()
    try:
        for upload, raw_name in zip(uploads, filenames):
            filename = unique_relative_path(used, raw_name)
            used.add(filename)
            destination = files_dir / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    total_bytes += len(chunk)
                    if total_bytes > settings.max_dataset_bytes:
                        raise ApiError(f"数据集超过上限（{settings.max_dataset_mb} MB）", 413)
                    handle.write(chunk)
            if size == 0:
                raise ApiError(f"文件 {filename} 为空", 400)
            try:
                os.chmod(destination, 0o444)
            except OSError:
                pass
            saved.append((filename, size, str(destination)))

        dataset = db.create_dataset_with_files(
            customer_id=customer_id,
            name=display_name,
            description=truncate((description or "").strip(), 500) or None,
            files=saved,
            size_bytes=total_bytes,
            file_count=len(saved),
            dataset_id=dataset_id,
            visibility="shared" if shared else "private",
        )
    except Exception:
        shutil.rmtree(files_dir.parent, ignore_errors=True)
        raise
    finally:
        for upload in uploads:
            await upload.close()

    db.add_activity(
        customer_id,
        "dataset.upload",
        target_type="dataset",
        target_id=dataset["id"],
        detail=dataset["name"],
        ip=ip,
    )
    if shared:
        db.add_activity(
            customer_id,
            "dataset.share",
            target_type="dataset",
            target_id=dataset["id"],
            detail=dataset["name"],
            ip=ip,
        )
    return dataset, db.get_dataset_files(dataset["id"])


def delete_dataset(
    *,
    db: Database,
    storage: Storage,
    customer: dict[str, Any],
    dataset_id: str,
    ip: str | None,
) -> None:
    dataset = db.get_dataset(customer["id"], dataset_id)
    if dataset is None:
        raise ApiError("数据集不存在", 404)
    if db.count_active_runs(dataset_id=dataset_id):
        raise ApiError("该数据集仍有排队或执行中的训练任务，暂时无法删除", 409)
    db.delete_dataset(customer["id"], dataset_id)
    storage.remove_tree(storage.dataset_dir(customer["id"], dataset_id))
    db.add_activity(
        customer["id"],
        "dataset.delete",
        target_type="dataset",
        target_id=dataset_id,
        detail=dataset["name"],
        ip=ip,
    )


def set_dataset_sharing(
    *,
    db: Database,
    customer: dict[str, Any],
    dataset_id: str,
    shared: bool,
    ip: str | None,
) -> dict[str, Any]:
    """只有所有者能切换共享；共享是可信场景下的可见性，不是安全边界。"""
    dataset = db.get_dataset(customer["id"], dataset_id)
    if dataset is None:
        raise ApiError("数据集不存在", 404)
    updated = db.set_dataset_visibility(
        customer["id"], dataset_id, "shared" if shared else "private"
    )
    if updated is None:
        raise ApiError("数据集不存在", 404)
    db.add_activity(
        customer["id"],
        "dataset.share" if shared else "dataset.unshare",
        target_type="dataset",
        target_id=dataset_id,
        detail=dataset["name"],
        ip=ip,
    )
    return updated


def extract_dataset_archives(
    *,
    db: Database,
    storage: Storage,
    settings: Settings,
    customer: dict[str, Any],
    dataset_id: str,
    ip: str | None = None,
) -> dict[str, Any]:
    """把数据集里的 ZIP 解压成目录内容（仅所有者；活动任务引用时拒绝）。"""
    dataset = db.get_dataset(customer["id"], dataset_id)
    if dataset is None:
        raise ApiError("数据集不存在", 404)
    if db.count_active_runs(dataset_id=dataset_id):
        raise ApiError("该数据集仍有排队或执行中的训练任务，暂时无法解压", 409)
    files = db.get_dataset_files(dataset_id)
    archives = [item for item in files if Path(item["filename"]).suffix.lower() == ".zip"]
    if not archives:
        raise ApiError("数据集没有可解压的 ZIP 文件", 400)
    files_dir = storage.dataset_files_dir(dataset["customer_id"], dataset_id)
    for item in archives:
        archive = Path(item["storage_path"])
        if not archive.is_file():
            continue
        target = (files_dir / item["filename"]).parent
        extract_zip_safe(
            archive,
            target,
            settings.max_dataset_bytes,
            label="数据集",
            max_files=100_000,
            overwrite=True,
        )
        archive.unlink(missing_ok=True)

    entries: list[tuple[str, int, str]] = []
    listing, _ = iter_files(files_dir, limit=200_000)
    for relative, size in listing:
        path = files_dir / relative
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
        entries.append((relative, size, str(path)))
    db.replace_dataset_files(
        dataset_id,
        entries,
        size_bytes=sum(size for _, size, _ in entries),
        file_count=len(entries),
    )
    storage.dataset_zip_path(dataset["customer_id"], dataset_id).unlink(missing_ok=True)
    db.add_activity(
        customer["id"],
        "dataset.extract",
        target_type="dataset",
        target_id=dataset_id,
        detail=dataset["name"],
        ip=ip,
    )
    updated = db.get_accessible_dataset(customer["id"], dataset_id)
    if updated is None:
        raise ApiError("数据集不存在", 404)
    return updated


def dataset_download(
    *, db: Database, storage: Storage, customer: dict[str, Any], dataset_id: str
) -> tuple[Path, str]:
    dataset = db.get_accessible_dataset(customer["id"], dataset_id)
    if dataset is None:
        raise ApiError("数据集不存在", 404)
    files = db.get_dataset_files(dataset_id)
    if not files:
        raise ApiError("数据集为空", 404)
    if len(files) == 1:
        single = Path(files[0]["storage_path"])
        if single.is_file():
            return single, files[0]["filename"]
    zip_path = storage.dataset_zip_path(dataset["customer_id"], dataset_id)
    if not zip_path.is_file():
        build_zip(storage.dataset_files_dir(dataset["customer_id"], dataset_id), zip_path)
    return zip_path, f"{_download_stem(dataset['name'])}.zip"


# ---------------------------------------------------------------------- notebooks


def _notebook_display_name(name: str | None, fallback: str = "未命名 Notebook") -> str:
    display = (name or "").strip() or fallback
    return truncate(display, 120) or fallback


def _notebook_filename(display_name: str) -> str:
    stem = safe_filename(display_name).strip()
    if stem.lower().endswith(".ipynb"):
        stem = stem[:-6].strip()
    stem = safe_filename(stem).rstrip(". ")[:160] or "notebook"
    return f"{stem}.ipynb"


def _validated_notebook_bytes(content: dict[str, Any], max_bytes: int) -> tuple[bytes, int]:
    try:
        raw = json.dumps(content, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Notebook 内容不合法：{exc}", 400) from exc
    if len(raw) > max_bytes:
        raise ApiError(f"Notebook 超过上限（{max_bytes // (1024 * 1024)} MB）", 413)
    try:
        import nbformat

        node = nbformat.from_dict(content)
        ensure_cell_ids(node)
        strip_outputs(node)
        nbformat.validate(node)
        if not node.cells:
            raise ApiError("Notebook 至少需要一个单元格", 400)
        serialized = nbformat.writes(node).encode("utf-8")
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Notebook 内容不合法：{exc}", 400) from exc
    if len(serialized) > max_bytes:
        raise ApiError(f"Notebook 超过上限（{max_bytes // (1024 * 1024)} MB）", 413)
    cell_count = sum(1 for cell in node.cells if cell.cell_type == "code")
    return serialized, cell_count


def create_manual_notebook(
    *,
    db: Database,
    storage: Storage,
    settings: Settings,
    customer: dict[str, Any],
    name: str | None,
    content: dict[str, Any],
    ip: str | None,
) -> dict[str, Any]:
    display_name = _notebook_display_name(name)
    serialized, cell_count = _validated_notebook_bytes(content, settings.max_notebook_bytes)
    notebook_id = "nb_" + secrets.token_hex(6)
    filename = _notebook_filename(display_name)
    target = storage.notebook_path(customer["id"], notebook_id, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(serialized)

    notebook = db.create_notebook(
        customer_id=customer["id"],
        name=display_name,
        filename=filename,
        size_bytes=len(serialized),
        cell_count=cell_count,
        storage_path=str(target),
        notebook_id=notebook_id,
    )
    db.add_activity(
        customer["id"],
        "notebook.create",
        target_type="notebook",
        target_id=notebook["id"],
        detail=notebook["name"],
        ip=ip,
    )
    return notebook


def notebook_content(*, db: Database, customer: dict[str, Any], notebook_id: str) -> dict[str, Any]:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    node = ensure_script_notebook(db=db, customer_id=customer["id"], notebook=notebook)
    return {
        "name": notebook["name"],
        "filename": notebook["filename"],
        "content": script_payload(node),
    }


def update_notebook(
    *,
    db: Database,
    storage: Storage,
    settings: Settings,
    customer: dict[str, Any],
    notebook_id: str,
    name: str | None,
    content: dict[str, Any],
    ip: str | None,
) -> dict[str, Any]:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    if db.count_active_runs(notebook_id=notebook_id):
        raise ApiError("该 Notebook 仍有排队或执行中的训练任务，暂时无法编辑", 409)

    serialized, cell_count = _validated_notebook_bytes(content, settings.max_notebook_bytes)
    old_path = Path(notebook["storage_path"])
    if old_path.is_symlink():
        raise ApiError("Notebook 存储路径异常，请重新导入", 409)

    display_name = (
        notebook["name"]
        if name is None
        else _notebook_display_name(name, fallback=notebook["name"])
    )
    if display_name == notebook["name"]:
        filename = notebook["filename"]
        target = old_path
    else:
        filename = _notebook_filename(display_name)
        target = storage.notebook_path(customer["id"], notebook_id, filename)

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ApiError("Notebook 存储路径异常，请重新导入", 409)
    target.write_bytes(serialized)
    if target != old_path and old_path.is_file():
        old_path.unlink()

    updated = db.update_notebook(
        customer["id"],
        notebook_id,
        name=display_name,
        filename=filename,
        size_bytes=len(serialized),
        cell_count=cell_count,
        storage_path=str(target),
    )
    if updated is None:
        raise ApiError("Notebook 不存在", 404)
    db.add_activity(
        customer["id"],
        "notebook.update",
        target_type="notebook",
        target_id=notebook_id,
        detail=updated["name"],
        ip=ip,
    )
    return updated


async def create_notebook(
    *,
    db: Database,
    storage: Storage,
    settings: Settings,
    customer: dict[str, Any],
    name: str | None,
    upload: UploadFile,
    ip: str | None,
) -> dict[str, Any]:
    raw_name = safe_filename(upload.filename)
    if not raw_name.lower().endswith(".ipynb"):
        await upload.close()
        raise ApiError("只接受 .ipynb 格式的 Notebook 文件", 400)

    try:
        content = await upload.read(settings.max_notebook_bytes + 1)
    finally:
        await upload.close()
    if len(content) > settings.max_notebook_bytes:
        raise ApiError(f"Notebook 超过上限（{settings.max_notebook_mb} MB）", 413)
    if not content:
        raise ApiError("Notebook 文件为空", 400)

    try:
        import nbformat

        notebook_node = nbformat.reads(content.decode("utf-8"), as_version=4)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(f"Notebook 文件不合法：{exc}", 400) from exc

    ensure_cell_ids(notebook_node)
    session_cells = extract_session_cells(notebook_node)
    strip_outputs(notebook_node)
    try:
        nbformat.validate(notebook_node)
        serialized = nbformat.writes(notebook_node).encode("utf-8")
    except Exception as exc:
        raise ApiError(f"Notebook 文件不合法：{exc}", 400) from exc
    if len(serialized) > settings.max_notebook_bytes:
        raise ApiError(f"Notebook 超过上限（{settings.max_notebook_mb} MB）", 413)

    cell_count = sum(1 for cell in notebook_node.cells if cell.cell_type == "code")
    display_name = (name or "").strip() or Path(raw_name).stem
    display_name = truncate(display_name, 120) or Path(raw_name).stem

    notebook_id = "nb_" + secrets.token_hex(6)
    target = storage.notebook_path(customer["id"], notebook_id, raw_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(serialized)

    notebook = db.create_notebook(
        customer_id=customer["id"],
        name=display_name,
        filename=raw_name,
        size_bytes=len(serialized),
        cell_count=cell_count,
        storage_path=str(target),
        notebook_id=notebook_id,
    )
    if session_cells:
        db.upsert_notebook_session(
            customer["id"],
            notebook_id,
            origin="import",
            cells=session_cells,
        )
    db.add_activity(
        customer["id"],
        "notebook.upload",
        target_type="notebook",
        target_id=notebook["id"],
        detail=notebook["name"],
        ip=ip,
    )
    return notebook


def delete_notebook(
    *,
    db: Database,
    storage: Storage,
    customer: dict[str, Any],
    notebook_id: str,
    ip: str | None,
) -> None:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    if db.count_active_runs(notebook_id=notebook_id):
        raise ApiError("该 Notebook 仍有排队或执行中的训练任务，暂时无法删除", 409)
    db.delete_notebook(customer["id"], notebook_id)
    storage.remove_tree(storage.notebook_dir(customer["id"], notebook_id))
    db.add_activity(
        customer["id"],
        "notebook.delete",
        target_type="notebook",
        target_id=notebook_id,
        detail=notebook["name"],
        ip=ip,
    )


def notebook_download(
    *, db: Database, customer: dict[str, Any], notebook_id: str
) -> tuple[bytes, str]:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    node = ensure_script_notebook(db=db, customer_id=customer["id"], notebook=notebook)
    session = db.get_notebook_session(customer["id"], notebook_id)
    if session and session.get("cells"):
        merge_session_cells(node, session["cells"])
    try:
        import nbformat

        nbformat.validate(node)
        raw = nbformat.writes(node).encode("utf-8")
    except Exception as exc:
        raise ApiError(f"Notebook 或会话快照不合法，无法下载：{exc}", 422) from exc
    return raw, notebook["filename"]


def notebook_session_get(
    *, db: Database, customer: dict[str, Any], notebook_id: str
) -> dict[str, Any]:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    node = ensure_script_notebook(db=db, customer_id=customer["id"], notebook=notebook)
    session = db.get_notebook_session(customer["id"], notebook_id)
    if session is None:
        return {"savedAt": None, "origin": None, "cells": []}
    current_hashes = {
        str(cell.get("id") or ""): source_hash(source_text(cell))
        for cell in node.cells
        if cell.get("cell_type") == "code"
    }
    cells: list[dict[str, Any]] = []
    for item in session.get("cells") or []:
        if not isinstance(item, dict):
            continue
        cell_id = str(item.get("cellId") or "")
        if cell_id not in current_hashes:
            continue
        cells.append(
            {
                "cellId": cell_id,
                "executionCount": item.get("executionCount"),
                "outputs": item.get("outputs") if isinstance(item.get("outputs"), list) else [],
                "sourceHash": item.get("sourceHash"),
                "matchesScript": item.get("sourceHash") == current_hashes[cell_id],
            }
        )
    return {
        "savedAt": session.get("saved_at"),
        "origin": session.get("origin"),
        "cells": cells,
    }


def notebook_session_save(
    *,
    db: Database,
    settings: Settings,
    customer: dict[str, Any],
    notebook_id: str,
    cells: list[Any],
) -> dict[str, Any]:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    node = ensure_script_notebook(db=db, customer_id=customer["id"], notebook=notebook)
    by_id = {
        str(cell.get("id") or ""): cell for cell in node.cells if cell.get("cell_type") == "code"
    }
    source_bytes = sum(len(item.source.encode("utf-8")) for item in cells)
    if source_bytes > settings.max_notebook_bytes:
        raise ApiError(f"会话快照超过上限（{settings.max_notebook_mb} MB）", 413)
    seen: set[str] = set()
    snapshot: list[dict[str, Any]] = []
    mismatched: list[str] = []
    missing: list[str] = []
    for item in cells:
        cell_id = str(item.cellId)
        if cell_id in seen:
            raise ApiError(f"单元格重复：{cell_id}", 400)
        seen.add(cell_id)
        cell = by_id.get(cell_id)
        if cell is None:
            missing.append(cell_id)
            continue
        stored_source = source_text(cell)
        if stored_source != item.source:
            mismatched.append(cell_id)
            continue
        snapshot.append(
            {
                "cellId": cell_id,
                "sourceHash": source_hash(stored_source),
                "executionCount": item.executionCount,
                "outputs": item.outputs,
            }
        )
    if missing:
        raise ApiError(
            "部分单元格尚未保存到 Notebook，请先保存 Notebook：" + ", ".join(missing[:5]),
            409,
        )
    if mismatched:
        raise ApiError(
            "代码有未保存修改，请先保存 Notebook 再保存会话：" + ", ".join(mismatched[:5]),
            409,
        )
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ApiError(f"运行输出不合法：{exc}", 400) from exc
    if len(encoded) > settings.max_notebook_bytes:
        raise ApiError(f"会话快照超过上限（{settings.max_notebook_mb} MB）", 413)

    try:
        import nbformat

        probe = nbformat.reads(nbformat.writes(node), as_version=4)
        probe_by_id = {
            str(cell.get("id") or ""): cell
            for cell in probe.cells
            if cell.get("cell_type") == "code"
        }
        for item in snapshot:
            probe_cell = probe_by_id.get(item["cellId"])
            if probe_cell is not None:
                probe_cell["outputs"] = nbformat.from_dict(item["outputs"])
                probe_cell["execution_count"] = item["executionCount"]
        nbformat.validate(probe)
    except Exception as exc:
        raise ApiError(f"运行输出不合法：{exc}", 400) from exc

    if not snapshot:
        db.delete_notebook_session(customer["id"], notebook_id)
    else:
        db.upsert_notebook_session(
            customer["id"],
            notebook_id,
            origin="session",
            cells=snapshot,
        )
    return notebook_session_get(db=db, customer=customer, notebook_id=notebook_id)


# ---------------------------------------------------------------------- runs


def ensure_executor_idle(db: Database, settings: Settings) -> None:
    """单任务实例：已有排队/运行中的任务时拒绝新提交（409）。"""
    active = db.count_active_runs()
    limit = max(1, int(settings.max_concurrent_runs or 1))
    if active >= limit:
        raise ApiError(
            f"TrainHub 当前已有 {active} 个任务在运行/排队（单任务实例，上限 {limit}），"
            "请等待任务结束后再提交",
            409,
        )


def create_run(
    *,
    db: Database,
    settings: Settings,
    customer: dict[str, Any],
    notebook_id: str,
    dataset_id: str | None,
    name: str | None,
    kernel: str | None,
    parameters: dict[str, Any] | None,
    timeout_seconds: int | None,
    ip: str | None,
) -> dict[str, Any]:
    if not jupyter_available():
        raise ApiError("服务器尚未安装 Jupyter 执行环境，无法提交训练", 503)
    ensure_executor_idle(db, settings)

    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    dataset = None
    if dataset_id:
        dataset = db.get_accessible_dataset(customer["id"], dataset_id)
        if dataset is None:
            raise ApiError("数据集不存在", 404)

    available = {item["name"] for item in list_kernels()}
    if not available:
        raise ApiError("服务器没有可用的 Jupyter 内核，请联系管理员", 503)
    chosen_kernel = (kernel or "").strip() or None
    if chosen_kernel is not None and chosen_kernel not in available:
        raise ApiError(
            f"内核不可用：{chosen_kernel}；可用内核：{', '.join(sorted(available))}",
            400,
        )

    parameters_json = json.dumps(parameters, ensure_ascii=False) if parameters else None
    timeout = settings.default_timeout_seconds if timeout_seconds is None else timeout_seconds
    run_name = (name or "").strip() or f"{notebook['name']} · {_timestamped_name('run')}"

    run = db.create_run(
        customer_id=customer["id"],
        notebook=notebook,
        dataset=dataset,
        name=truncate(run_name, 120) or run_name,
        kernel=chosen_kernel,
        parameters_json=parameters_json,
        timeout_seconds=int(timeout or 0),
    )
    db.add_activity(
        customer["id"],
        "run.create",
        target_type="run",
        target_id=run["id"],
        detail=run["name"],
        ip=ip,
    )
    return run


async def create_execution(
    *,
    db: Database,
    storage: Storage,
    settings: Settings,
    name: str | None,
    code: UploadFile,
    dataset: UploadFile | None,
    assets: UploadFile | None,
    entry: str | None,
    kernel: str | None,
    parameters: dict[str, Any] | None,
    timeout_seconds: int | None,
    base_model: str | None,
    control_job_id: str | None,
    customer_id: str | None = None,
    dataset_name: str | None = None,
) -> dict[str, Any]:
    """平台控制面推送的闭环执行：只落输入并排队，不解释代码版本。

    `customer_id` 为执行身份客户（wmg-server 配置的平台身份）；缺省归系统客户。
    """
    if not jupyter_available():
        raise ApiError("服务器尚未安装 Jupyter 执行环境，无法提交训练", 503)
    ensure_executor_idle(db, settings)
    target_customer_id = (customer_id or "").strip() or Database.SYSTEM_CUSTOMER_ID
    target_customer = db.get_customer(target_customer_id)
    if target_customer is None:
        raise ApiError(f"执行身份客户不存在：{target_customer_id}", 400)
    if target_customer.get("status") != "active":
        raise ApiError(f"执行身份客户已停用：{target_customer_id}", 400)
    available = {item["name"] for item in list_kernels()}
    if not available:
        raise ApiError("服务器没有可用的 Jupyter 内核，请联系管理员", 503)
    chosen_kernel = (kernel or "").strip() or None
    if chosen_kernel is not None and chosen_kernel not in available:
        raise ApiError(
            f"内核不可用：{chosen_kernel}；可用内核：{', '.join(sorted(available))}", 400
        )

    run_id = "tr_" + secrets.token_hex(6)
    code_name = safe_filename(code.filename or "code.zip")
    code_path = storage.execution_code_path(run_id, code_name)
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_sha = hashlib.sha256()
    code_bytes = 0
    try:
        with code_path.open("wb") as handle:
            while True:
                chunk = await code.read(CHUNK_SIZE)
                if not chunk:
                    break
                code_bytes += len(chunk)
                if code_bytes > settings.max_code_bytes:
                    raise ApiError(f"代码包超过上限（{settings.max_code_mb} MB）", 413)
                code_sha.update(chunk)
                handle.write(chunk)
        if code_bytes == 0:
            raise ApiError("代码包为空", 400)

        dataset_path: Path | None = None
        dataset_sha: str | None = None
        if dataset is not None and dataset.filename:
            dataset_path = storage.execution_dataset_path(run_id)
            dataset_sha_hash = hashlib.sha256()
            dataset_bytes = 0
            with dataset_path.open("wb") as handle:
                while True:
                    chunk = await dataset.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    dataset_bytes += len(chunk)
                    if dataset_bytes > settings.max_dataset_bytes:
                        raise ApiError(f"数据集超过上限（{settings.max_dataset_mb} MB）", 413)
                    dataset_sha_hash.update(chunk)
                    handle.write(chunk)
            if dataset_bytes == 0:
                dataset_path.unlink(missing_ok=True)
                dataset_path = None
            else:
                dataset_sha = dataset_sha_hash.hexdigest()

        assets_path: Path | None = None
        assets_sha: str | None = None
        if assets is not None and assets.filename:
            assets_path = storage.execution_assets_path(run_id)
            assets_sha_hash = hashlib.sha256()
            assets_bytes = 0
            with assets_path.open("wb") as handle:
                while True:
                    chunk = await assets.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    assets_bytes += len(chunk)
                    if assets_bytes > settings.max_assets_bytes:
                        raise ApiError(f"资产包超过上限（{settings.max_assets_mb} MB）", 413)
                    assets_sha_hash.update(chunk)
                    handle.write(chunk)
            if assets_bytes == 0:
                assets_path.unlink(missing_ok=True)
                assets_path = None
            else:
                assets_sha = assets_sha_hash.hexdigest()

        parameters_json = json.dumps(parameters, ensure_ascii=False) if parameters else None
        timeout = settings.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        display_name = truncate((name or "").strip() or f"平台执行 · {run_id}", 120) or run_id
        run = db.create_execution_run(
            run_id=run_id,
            name=display_name,
            kernel=chosen_kernel,
            parameters_json=parameters_json,
            timeout_seconds=int(timeout or 0),
            control_job_id=control_job_id,
            code_archive_path=str(code_path),
            code_sha256=code_sha.hexdigest(),
            entry=(entry or "").strip() or None,
            dataset_archive_path=str(dataset_path) if dataset_path else None,
            dataset_sha256=dataset_sha,
            assets_archive_path=str(assets_path) if assets_path else None,
            assets_sha256=assets_sha,
            base_model=(base_model or "").strip() or None,
            customer_id=target_customer_id,
            dataset_name=(dataset_name or "").strip() or None,
        )
    except BaseException:
        storage.remove_tree(storage.execution_dir(run_id))
        raise
    finally:
        await code.close()
        if dataset is not None:
            await dataset.close()
        if assets is not None:
            await assets.close()

    db.add_activity(
        target_customer_id,
        "execution.create",
        target_type="run",
        target_id=run_id,
        detail=display_name,
        ip=None,
    )
    return run


def ensure_cells_allowed(db: Database) -> None:
    """单任务实例：节点正在排队/执行训练任务时，禁止交互式运行单元格（避免争用 GPU）。

    与平台调度共用同一「单例」口径：只要存在 queued/running 的 run（无论客户任务还是
    平台推送的执行），就把节点视为被占用。任务结束后自动恢复。
    """
    counts = db.global_run_status_counts()
    active = int(counts.get("queued") or 0) + int(counts.get("running") or 0)
    if not active:
        return
    current = (db.list_active_runs(limit=1) or [None])[0]
    if current:
        status = "运行中" if current.get("status") == "running" else "排队中"
        detail = f"（{status}：{current.get('name') or current.get('id')}）"
    else:
        detail = ""
    raise ApiError(
        f"训练节点正在被占用，单实例训练期间不能运行单元格{detail}；请等待当前任务结束后重试",
        409,
    )


def get_run_or_404(db: Database, customer: dict[str, Any], run_id: str) -> dict[str, Any]:
    run = db.get_run(customer["id"], run_id)
    if run is None:
        raise ApiError("训练任务不存在", 404)
    return run


def delete_run(
    *, db: Database, storage: Storage, customer: dict[str, Any], run_id: str, ip: str | None
) -> None:
    run = get_run_or_404(db, customer, run_id)
    if run["status"] not in TERMINAL_RUN_STATUSES:
        raise ApiError("训练任务尚未结束，无法删除", 409)
    db.delete_run(customer["id"], run_id)
    storage.remove_tree(storage.run_dir(customer["id"], run_id))
    db.add_activity(
        customer["id"],
        "run.delete",
        target_type="run",
        target_id=run_id,
        detail=run["name"],
        ip=ip,
    )


# ---------------------------------------------------------------------- run logs / outputs


def read_run_log_slice(
    *,
    db: Database,
    storage: Storage,
    customer: dict[str, Any],
    run: dict[str, Any],
    offset: int,
    limit: int,
    tail: bool = False,
) -> dict[str, Any]:
    log_path = (
        Path(run["log_path"])
        if run.get("log_path")
        else storage.run_log_path(customer["id"], run["id"])
    )
    completed = run["status"] in TERMINAL_RUN_STATUSES
    if not log_path.is_file():
        return {
            "content": "",
            "nextOffset": 0,
            "totalBytes": 0,
            "completed": completed,
            "status": run["status"],
        }
    total = log_path.stat().st_size
    if tail:
        start = max(0, total - limit)
        with log_path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read()
        return {
            "content": raw.decode("utf-8", errors="replace"),
            "nextOffset": total,
            "totalBytes": total,
            "completed": completed,
            "status": run["status"],
        }
    offset = max(0, min(offset, total))
    with log_path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(limit)
    end = offset + len(raw)
    if end < total:
        # 不在文件末尾时，退回到完整 UTF-8 字符边界，避免多字节字符被切断。
        raw = raw[: _utf8_safe_cut(raw)]
        end = offset + len(raw)
    return {
        "content": raw.decode("utf-8", errors="replace"),
        "nextOffset": end,
        "totalBytes": total,
        "completed": completed,
        "status": run["status"],
    }


def _utf8_safe_cut(raw: bytes) -> int:
    for back in range(4):
        length = len(raw) - back
        if length <= 0:
            break
        try:
            raw[:length].decode("utf-8")
            return length
        except UnicodeDecodeError:
            continue
    return len(raw)


def run_metrics(
    *,
    storage: Storage,
    customer: dict[str, Any],
    run: dict[str, Any],
    max_points: int = DEFAULT_MAX_POINTS,
) -> dict[str, Any]:
    """读取可选的训练指标；指标缺失/损坏不阻断运行详情。"""
    path = storage.run_metrics_path(customer["id"], run["id"])
    return read_metrics_file(path, max_points=max_points)


MAX_ENVIRONMENT_BYTES = 64 * 1024


def run_environment(
    *, storage: Storage, customer: dict[str, Any], run: dict[str, Any]
) -> dict[str, Any] | None:
    """读取执行器在训练启动时写入的环境快照（老运行没有该文件）。"""
    path = storage.run_environment_path(customer["id"], run["id"])
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_ENVIRONMENT_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def run_log_file(
    *, storage: Storage, customer: dict[str, Any], run: dict[str, Any]
) -> tuple[Path, str]:
    log_path = (
        Path(run["log_path"])
        if run.get("log_path")
        else storage.run_log_path(customer["id"], run["id"])
    )
    if not log_path.is_file():
        raise ApiError("训练日志尚未生成", 404)
    return log_path, f"{_download_stem(run['name'])}.log"


RESULT_MANIFEST_NAME = "trainhub_result.json"
MAX_RESULT_MANIFEST_BYTES = 256 * 1024


def run_result_manifest(
    storage: Storage,
    customer: dict[str, Any],
    run: dict[str, Any],
    *,
    include_manifest: bool = True,
) -> dict[str, Any]:
    """读取产物目录里的 TrainHub 结果清单（平台成果桥契约）。

    只读固定文件名 ``trainhub_result.json``（输出目录根）；大小/JSON/对象形状
    任一不合法都返回 error，由调用方决定是否允许导入模型运营。
    """
    output_dir = (
        Path(run["output_path"])
        if run.get("output_path")
        else storage.run_output_dir(customer["id"], run["id"])
    )
    path = output_dir / RESULT_MANIFEST_NAME
    if path.is_symlink():
        return {"present": True, "manifest": None, "error": "结果清单不能是符号链接"}
    if not path.is_file():
        return {"present": False, "manifest": None, "error": None}
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"present": True, "manifest": None, "error": f"无法读取结果清单: {exc}"}
    if size > MAX_RESULT_MANIFEST_BYTES:
        return {
            "present": True,
            "manifest": None,
            "error": f"结果清单超过 {MAX_RESULT_MANIFEST_BYTES // 1024}KB",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"present": True, "manifest": None, "error": f"结果清单不是合法 JSON: {exc}"}
    if not isinstance(data, dict):
        return {"present": True, "manifest": None, "error": "结果清单必须是 JSON 对象"}
    return {
        "present": True,
        "manifest": data if include_manifest else None,
        "error": None,
    }


def run_outputs(storage: Storage, customer: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    output_dir = (
        Path(run["output_path"])
        if run.get("output_path")
        else storage.run_output_dir(customer["id"], run["id"])
    )
    result = scan_outputs(output_dir)
    result["runId"] = run["id"]
    return result


def output_file(
    *, storage: Storage, customer: dict[str, Any], run: dict[str, Any], relative: str
) -> tuple[Path, str, str]:
    output_dir = (
        Path(run["output_path"])
        if run.get("output_path")
        else storage.run_output_dir(customer["id"], run["id"])
    )
    if not output_dir.is_dir():
        raise ApiError("该训练任务没有产物", 404)
    path = resolve_within(output_dir, relative)
    if path.is_symlink() or not path.is_file():
        raise ApiError("产物文件不存在", 404)
    return path, path.name, "application/octet-stream"


def output_zip(
    *, storage: Storage, customer: dict[str, Any], run: dict[str, Any]
) -> tuple[Path, str]:
    output_dir = (
        Path(run["output_path"])
        if run.get("output_path")
        else storage.run_output_dir(customer["id"], run["id"])
    )
    if not output_dir.is_dir():
        raise ApiError("该训练任务没有产物", 404)
    files, _truncated = scan_output_files(output_dir)
    if not files:
        raise ApiError("该训练任务没有产物", 404)
    zip_path = output_dir.parent / "output.zip"
    build_zip(output_dir, zip_path)
    return zip_path, f"{_download_stem(run['name'])}-outputs.zip"


def scan_output_files(output_dir: Path) -> tuple[list[tuple[str, int]], bool]:
    return iter_files(output_dir, limit=2000)


# ---------------------------------------------------------------------- misc


def _timestamped_name(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def _download_stem(name: str) -> str:
    stem = safe_filename(name).rsplit(".", 1)[0] or "trainhub"
    return stem or "trainhub"


__all__ = [
    "create_dataset",
    "create_notebook",
    "create_run",
    "dataset_download",
    "delete_dataset",
    "delete_notebook",
    "delete_run",
    "get_run_or_404",
    "notebook_download",
    "notebook_session_get",
    "notebook_session_save",
    "output_file",
    "output_zip",
    "read_run_log_slice",
    "run_log_file",
    "run_outputs",
    "run_result_manifest",
]
