from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse

from .. import __version__
from ..config import Settings
from ..db import Database
from ..kernels import default_kernel_name, jupyter_available, list_kernels
from ..metrics import DEFAULT_MAX_POINTS
from ..responses import ApiError, ok, page_data
from ..runner import RunExecutor
from ..schemas import CustomerCreate, CustomerStatusUpdate
from ..serializers import customer_public, run_public
from ..services import (
    create_execution,
    output_zip,
    run_environment,
    run_log_file,
    run_metrics,
    run_outputs,
    run_result_manifest,
)
from ..storage import Storage
from ..system_info import cpu_info, gpu_info
from ..utils import new_customer_token
from .deps import current_admin, get_db, get_executor, get_settings, get_storage

RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _read_log_chunk(path: Path, offset: int) -> tuple[bytes, int]:
    if not path.is_file():
        return b"", offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(64 * 1024)
            return chunk, offset + len(chunk)
    except OSError:
        return b"", offset


def _run_log_path(storage: Storage, run: dict[str, Any]) -> Path:
    if run.get("log_path"):
        return Path(run["log_path"])
    return storage.run_log_path(run["customer_id"], run["id"])


def _has_logs(storage: Storage, run: dict) -> bool:
    path = (
        Path(run["log_path"])
        if run.get("log_path")
        else storage.run_log_path(run["customer_id"], run["id"])
    )
    return path.is_file()


def _run_customer(db: Database, run: dict) -> dict | None:
    return db.get_customer(run["customer_id"])


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(current_admin)])


@router.get("/runs")
def admin_list_runs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    customerId: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=120),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    """平台成果桥：跨客户列出训练任务（含结果清单摘要）。"""
    if status and status not in RUN_STATUSES:
        raise ApiError(f"status 只能是：{', '.join(RUN_STATUSES)}", 400)
    rows, total = db.list_runs_admin(page, size, status, customerId, q)
    records = []
    for row in rows:
        customer = _run_customer(db, row)
        item = run_public(row, has_logs=_has_logs(storage, row))
        item["customer"] = {"id": customer["id"], "name": customer["name"]} if customer else None
        item["result"] = (
            run_result_manifest(storage, customer, row, include_manifest=False)
            if customer
            else {"present": False, "manifest": None, "error": "客户不存在"}
        )
        records.append(item)
    return ok(page_data(records, total, page, size))


@router.get("/runs/{run_id}")
def admin_run_detail(
    run_id: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    run = db.get_run_any(run_id)
    if run is None:
        raise ApiError("训练任务不存在", 404)
    customer = _run_customer(db, run)
    if customer is None:
        raise ApiError("训练客户不存在", 404)
    payload = run_public(run, has_logs=_has_logs(storage, run))
    payload["customer"] = {"id": customer["id"], "name": customer["name"]}
    payload["outputs"] = run_outputs(storage, customer, run)
    payload["result"] = run_result_manifest(storage, customer, run)
    payload["environment"] = run_environment(storage=storage, customer=customer, run=run)
    log_path = (
        Path(run["log_path"])
        if run.get("log_path")
        else storage.run_log_path(customer["id"], run_id)
    )
    payload["logBytes"] = log_path.stat().st_size if log_path.is_file() else 0
    return ok(payload)


@router.get("/runs/{run_id}/metrics")
def admin_run_metrics(
    run_id: str,
    maxPoints: int = Query(DEFAULT_MAX_POINTS, ge=100, le=5000),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    run = db.get_run_any(run_id)
    if run is None:
        raise ApiError("训练任务不存在", 404)
    customer = _run_customer(db, run)
    if customer is None:
        raise ApiError("训练客户不存在", 404)
    return ok(run_metrics(storage=storage, customer=customer, run=run, max_points=maxPoints))


@router.get("/runs/{run_id}/outputs/download")
async def admin_download_run_outputs(
    run_id: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    run = db.get_run_any(run_id)
    if run is None:
        raise ApiError("训练任务不存在", 404)
    customer = _run_customer(db, run)
    if customer is None:
        raise ApiError("训练客户不存在", 404)
    path, filename = await run_in_threadpool(
        output_zip, storage=storage, customer=customer, run=run
    )
    return FileResponse(Path(path), filename=filename, media_type="application/zip")


@router.get("/runs/{run_id}/logs")
def admin_download_run_logs(
    run_id: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    run = db.get_run_any(run_id)
    if run is None:
        raise ApiError("训练任务不存在", 404)
    customer = _run_customer(db, run)
    if customer is None:
        raise ApiError("训练客户不存在", 404)
    path, filename = run_log_file(storage=storage, customer=customer, run=run)
    return FileResponse(Path(path), filename=filename, media_type="text/plain")


@router.post("/runs/{run_id}/cancel")
def admin_cancel_run(
    run_id: str,
    db: Database = Depends(get_db),
    executor: RunExecutor = Depends(get_executor),
) -> dict:
    run = db.get_run_any(run_id)
    if run is None:
        raise ApiError("训练任务不存在", 404)
    if run["status"] not in ("queued", "running"):
        raise ApiError("训练任务已结束，无法取消", 409)
    accepted = executor.request_cancel(run_id, run["status"])
    refreshed = db.get_run_any(run_id) or run
    if not accepted and refreshed["status"] in ("queued", "running"):
        raise ApiError("取消请求未能生效，请重试", 409)
    db.add_activity(
        Database.SYSTEM_CUSTOMER_ID,
        "run.cancel",
        target_type="run",
        target_id=run_id,
        detail=run["name"],
        ip=None,
    )
    return ok(run_public(refreshed), message="已请求取消训练")


@router.get("/executor/status")
async def admin_executor_status(
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    """调度与资源治理（只读）：控制面据此判断 TrainHub 繁忙程度，不做资源预留。"""
    counts = db.global_run_status_counts()
    gpus = await run_in_threadpool(gpu_info)
    cpu = await run_in_threadpool(cpu_info)
    kernels = await run_in_threadpool(list_kernels)
    return ok(
        {
            "maxConcurrentRuns": settings.max_concurrent_runs,
            "queuedRuns": counts.get("queued", 0),
            "runningRuns": counts.get("running", 0),
            "activeRuns": db.list_active_runs(),
            "gpus": gpus,
            "cpu": cpu,
            "disk": storage.disk_usage(),
            "kernels": [str(item.get("name") or "") for item in kernels if item.get("name")],
            "jupyterAvailable": jupyter_available(),
            "defaultKernel": default_kernel_name(),
            "version": __version__,
        }
    )


@router.get("/runs/{run_id}/logs/stream")
async def admin_run_log_stream(
    run_id: str,
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> StreamingResponse:
    """实时日志流（SSE）：任务必须存在且尚未结束。

    事件：`status`（状态变化）、`log`（日志文本块）、`end`（终态收尾）。
    消费方在收到 `end` 后可查询 `/api/admin/runs/{id}` 并下载产物。
    """
    run = db.get_run_any(run_id)
    if run is None:
        raise ApiError("训练任务不存在", 404)
    if run["status"] in TERMINAL_RUN_STATUSES:
        raise ApiError("训练任务已结束，请直接查询结果或下载产物", 409)
    log_path = _run_log_path(storage, run)

    async def event_stream():
        offset = 0
        last_status = run["status"]
        last_ping = time.monotonic()
        yield _sse(
            "status",
            {"runId": run_id, "status": last_status, "controlJobId": run.get("control_job_id")},
        )
        while True:
            chunk, offset = await asyncio.to_thread(_read_log_chunk, log_path, offset)
            if chunk:
                yield _sse(
                    "log",
                    {"runId": run_id, "text": chunk.decode("utf-8", errors="replace")},
                )
            row = await asyncio.to_thread(db.get_run_any, run_id)
            if row is None:
                yield _sse("end", {"runId": run_id, "status": "missing"})
                return
            status = str(row.get("status") or "")
            if status != last_status:
                last_status = status
                yield _sse(
                    "status",
                    {
                        "runId": run_id,
                        "status": status,
                        "controlJobId": row.get("control_job_id"),
                    },
                )
            if status in TERMINAL_RUN_STATUSES:
                chunk, offset = await asyncio.to_thread(_read_log_chunk, log_path, offset)
                if chunk:
                    yield _sse(
                        "log",
                        {"runId": run_id, "text": chunk.decode("utf-8", errors="replace")},
                    )
                yield _sse(
                    "end",
                    {
                        "runId": run_id,
                        "status": status,
                        "exitCode": row.get("exit_code"),
                        "message": row.get("message"),
                        "controlJobId": row.get("control_job_id"),
                    },
                )
                return
            if time.monotonic() - last_ping >= 15.0:
                yield ": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/executions")
async def admin_create_execution(
    request: Request,
    code: UploadFile = File(...),
    dataset: UploadFile | None = File(None),
    assets: UploadFile | None = File(None),
    name: str = Form(""),
    entry: str = Form(""),
    kernel: str = Form(""),
    parameters: str = Form(""),
    timeoutSeconds: int = Form(0),
    baseModel: str = Form(""),
    controlJobId: str = Form(""),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    executor: RunExecutor = Depends(get_executor),
) -> dict:
    """平台控制面推送一次闭环执行：代码包 + 数据集包 + 参数。

    只排队执行，不保存 notebook 版本；版本语义由控制面作业记录承担。
    """
    params: dict[str, Any] | None = None
    raw = (parameters or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError("parameters 必须是合法 JSON", 400) from exc
        if not isinstance(parsed, dict):
            raise ApiError("parameters 必须是 JSON 对象", 400)
        params = parsed
    run = await create_execution(
        db=db,
        storage=storage,
        settings=request.app.state.settings,
        name=name,
        code=code,
        dataset=dataset,
        assets=assets,
        entry=entry,
        kernel=kernel,
        parameters=params,
        timeout_seconds=timeoutSeconds or None,
        base_model=baseModel,
        control_job_id=controlJobId,
    )
    executor.enqueue(run["id"])
    return ok(run_public(run, has_logs=False), message="平台执行任务已入队", code=201)


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
