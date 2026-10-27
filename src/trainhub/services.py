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
    shared: bool = False,
    ip: str | None = None,
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
) -> dict[str, Any]:
    """平台控制面推送的闭环执行：只落输入并排队，不解释代码版本。"""
    if not jupyter_available():
        raise ApiError("服务器尚未安装 Jupyter 执行环境，无法提交训练", 503)
    ensure_executor_idle(db, settings)
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
        Database.SYSTEM_CUSTOMER_ID,
        "execution.create",
        target_type="run",
        target_id=run_id,
        detail=display_name,
        ip=None,
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
    "output_file",
    "output_zip",
    "read_run_log_slice",
    "run_log_file",
    "run_outputs",
    "run_result_manifest",
]
