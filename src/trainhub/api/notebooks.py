from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from ..config import Settings
from ..db import Database
from ..responses import ApiError, ok, page_data
from ..serializers import notebook_public, run_public
from ..services import create_notebook, delete_notebook, notebook_download
from ..storage import Storage
from .deps import client_ip, current_customer, get_db, get_settings, get_storage

router = APIRouter(prefix="/api/notebooks", tags=["notebooks"])


@router.get("")
def list_notebooks(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, max_length=120),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_notebooks(customer["id"], page, size, q)
    return ok(page_data([notebook_public(row) for row in rows], total, page, size))


@router.post("")
async def upload_notebook(
    request: Request,
    name: str | None = Form(None),
    file: UploadFile = File(...),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    notebook = await create_notebook(
        db=db,
        storage=storage,
        settings=settings,
        customer=customer,
        name=name,
        upload=file,
        ip=client_ip(request),
    )
    return ok(notebook_public(notebook), message="Notebook 已提交", code=201)


@router.get("/{notebook_id}")
def get_notebook(
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    return ok(notebook_public(notebook))


@router.get("/{notebook_id}/last-run")
def get_notebook_last_run(
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """提交训练时回填该 Notebook 最近一次运行的参数（没有运行则返回 null）。"""
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    run = db.latest_run_for_notebook(customer["id"], notebook_id)
    return ok(run_public(run) if run else None)


@router.get("/{notebook_id}/download")
async def download_notebook(
    request: Request,
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> FileResponse:
    path, filename = await run_in_threadpool(
        notebook_download, db=db, customer=customer, notebook_id=notebook_id
    )
    db.add_activity(
        customer["id"],
        "notebook.download",
        target_type="notebook",
        target_id=notebook_id,
        detail=filename,
        ip=client_ip(request),
    )
    return FileResponse(Path(path), filename=filename)


@router.delete("/{notebook_id}")
def remove_notebook(
    request: Request,
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    delete_notebook(
        db=db,
        storage=storage,
        customer=customer,
        notebook_id=notebook_id,
        ip=client_ip(request),
    )
    return ok(None, message="Notebook 已删除")
