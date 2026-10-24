from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from ..db import Database
from ..responses import ApiError, ok, page_data
from ..runner import RunExecutor
from ..schemas import RunCreate
from ..serializers import run_public
from ..services import (
    create_run,
    delete_run,
    get_run_or_404,
    output_file,
    output_zip,
    read_run_log_slice,
    run_log_file,
    run_outputs,
)
from ..storage import Storage
from .deps import (
    client_ip,
    current_customer,
    get_db,
    get_executor,
    get_storage,
)

router = APIRouter(prefix="/api/runs", tags=["runs"])

RUN_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


def _has_logs(storage: Storage, run: dict) -> bool:
    path = (
        Path(run["log_path"])
        if run.get("log_path")
        else storage.run_log_path(run["customer_id"], run["id"])
    )
    return path.is_file()


@router.get("")
def list_runs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    q: str | None = Query(None, max_length=120),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    if status and status not in RUN_STATUSES:
        raise ApiError(f"status 只能是：{', '.join(RUN_STATUSES)}", 400)
    rows, total = db.list_runs(customer["id"], page, size, status, q)
    records = [run_public(row, has_logs=_has_logs(storage, row)) for row in rows]
    return ok(page_data(records, total, page, size))


@router.post("")
def submit_run(
    request: Request,
    payload: RunCreate,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    executor: RunExecutor = Depends(get_executor),
) -> dict:
    run = create_run(
        db=db,
        settings=request.app.state.settings,
        customer=customer,
        notebook_id=payload.notebookId,
        dataset_id=payload.datasetId,
        name=payload.name,
        kernel=payload.kernel,
        parameters=payload.parameters,
        timeout_seconds=payload.timeoutSeconds,
        ip=client_ip(request),
    )
    executor.enqueue(run["id"])
    return ok(run_public(run, has_logs=False), message="训练任务已入队", code=201)


@router.get("/{run_id}")
def get_run(
    run_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    run = get_run_or_404(db, customer, run_id)
    payload = run_public(run, has_logs=_has_logs(storage, run))
    log_path = (
        Path(run["log_path"])
        if run.get("log_path")
        else storage.run_log_path(customer["id"], run_id)
    )
    payload["logBytes"] = log_path.stat().st_size if log_path.is_file() else 0
    payload["outputs"] = run_outputs(storage, customer, run)
    return ok(payload)


@router.post("/{run_id}/cancel")
def cancel_run(
    request: Request,
    run_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    executor: RunExecutor = Depends(get_executor),
) -> dict:
    run = get_run_or_404(db, customer, run_id)
    if run["status"] not in ("queued", "running"):
        raise ApiError("训练任务已结束，无法取消", 409)
    accepted = executor.request_cancel(run_id, run["status"])
    refreshed = db.get_run(customer["id"], run_id) or run
    if not accepted and refreshed["status"] in ("queued", "running"):
        raise ApiError("取消请求未能生效，请重试", 409)
    db.add_activity(
        customer["id"],
        "run.cancel",
        target_type="run",
        target_id=run_id,
        detail=run["name"],
        ip=client_ip(request),
    )
    return ok(run_public(refreshed), message="已请求取消训练")


@router.get("/{run_id}/logs")
def get_run_logs(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(256 * 1024, ge=1, le=1024 * 1024),
    tail: bool = Query(False),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    run = get_run_or_404(db, customer, run_id)
    return ok(
        read_run_log_slice(
            db=db,
            storage=storage,
            customer=customer,
            run=run,
            offset=offset,
            limit=limit,
            tail=tail,
        )
    )


@router.get("/{run_id}/logs/download")
async def download_run_logs(
    request: Request,
    run_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    run = get_run_or_404(db, customer, run_id)
    path, filename = await run_in_threadpool(
        run_log_file, storage=storage, customer=customer, run=run
    )
    db.add_activity(
        customer["id"],
        "log.download",
        target_type="run",
        target_id=run_id,
        detail=run["name"],
        ip=client_ip(request),
    )
    return FileResponse(Path(path), filename=filename, media_type="text/plain; charset=utf-8")


@router.get("/{run_id}/outputs")
def list_run_outputs(
    run_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    run = get_run_or_404(db, customer, run_id)
    return ok(run_outputs(storage, customer, run))


@router.get("/{run_id}/outputs/download")
async def download_run_outputs(
    request: Request,
    run_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    run = get_run_or_404(db, customer, run_id)
    path, filename = await run_in_threadpool(
        output_zip, storage=storage, customer=customer, run=run
    )
    db.add_activity(
        customer["id"],
        "outputs.download",
        target_type="run",
        target_id=run_id,
        detail=run["name"],
        ip=client_ip(request),
    )
    return FileResponse(Path(path), filename=filename, media_type="application/zip")


@router.get("/{run_id}/outputs/file")
async def download_single_output(
    request: Request,
    run_id: str,
    path: str = Query(..., min_length=1, max_length=1024),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    run = get_run_or_404(db, customer, run_id)
    resolved, filename, media_type = output_file(
        storage=storage, customer=customer, run=run, relative=path
    )
    db.add_activity(
        customer["id"],
        "output.download",
        target_type="run",
        target_id=run_id,
        detail=path,
        ip=client_ip(request),
    )
    return FileResponse(Path(resolved), filename=filename, media_type=media_type)


@router.delete("/{run_id}")
def remove_run(
    request: Request,
    run_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    delete_run(
        db=db,
        storage=storage,
        customer=customer,
        run_id=run_id,
        ip=client_ip(request),
    )
    return ok(None, message="训练任务已删除")
