from __future__ import annotations

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
from .kernels import jupyter_available, list_kernels, resolve_default_kernel
from .responses import ApiError
from .runner import scan_outputs
from .storage import (
    Storage,
    build_zip,
    iter_files,
    resolve_within,
    safe_filename,
    unique_filename,
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
    ip: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not uploads:
        raise ApiError("请至少选择一个文件", 400)

    filenames = [safe_filename(item.filename) for item in uploads]
    display_name = (name or "").strip()
    if not display_name:
        display_name = (
            Path(filenames[0]).stem if len(filenames) == 1 else _timestamped_name("dataset")
        )
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
            filename = unique_filename(used, raw_name)
            used.add(filename)
            destination = files_dir / filename
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


def dataset_download(
    *, db: Database, storage: Storage, customer: dict[str, Any], dataset_id: str
) -> tuple[Path, str]:
    dataset = db.get_dataset(customer["id"], dataset_id)
    if dataset is None:
        raise ApiError("数据集不存在", 404)
    files = db.get_dataset_files(dataset_id)
    if not files:
        raise ApiError("数据集为空", 404)
    if len(files) == 1:
        single = Path(files[0]["storage_path"])
        if single.is_file():
            return single, files[0]["filename"]
    zip_path = storage.dataset_zip_path(customer["id"], dataset_id)
    if not zip_path.is_file():
        build_zip(Path(files[0]["storage_path"]).parent, zip_path)
    return zip_path, f"{_download_stem(dataset['name'])}.zip"


# ---------------------------------------------------------------------- notebooks


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

    cell_count = sum(1 for cell in notebook_node.cells if cell.cell_type == "code")
    display_name = (name or "").strip() or Path(raw_name).stem
    display_name = truncate(display_name, 120) or Path(raw_name).stem

    notebook_id = "nb_" + secrets.token_hex(6)
    target = storage.notebook_path(customer["id"], notebook_id, raw_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    notebook = db.create_notebook(
        customer_id=customer["id"],
        name=display_name,
        filename=raw_name,
        size_bytes=len(content),
        cell_count=cell_count,
        storage_path=str(target),
        notebook_id=notebook_id,
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
) -> tuple[Path, str]:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    path = Path(notebook["storage_path"])
    if not path.is_file():
        raise ApiError("Notebook 源文件缺失", 404)
    return path, notebook["filename"]


# ---------------------------------------------------------------------- runs


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

    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    dataset = None
    if dataset_id:
        dataset = db.get_dataset(customer["id"], dataset_id)
        if dataset is None:
            raise ApiError("数据集不存在", 404)

    available = {item["name"] for item in list_kernels()}
    if not available:
        raise ApiError("服务器没有可用的 Jupyter 内核，请联系管理员", 503)
    chosen_kernel = (kernel or "").strip() or resolve_default_kernel(settings)
    if chosen_kernel not in available:
        raise ApiError(
            f"内核不可用：{chosen_kernel or '(空)'}；可用内核：{', '.join(sorted(available))}",
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
    "output_file",
    "output_zip",
    "read_run_log_slice",
    "run_log_file",
    "run_outputs",
]
