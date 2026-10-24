from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from ..config import Settings
from ..db import Database
from ..responses import ApiError, ok, page_data
from ..serializers import dataset_public
from ..services import (
    create_dataset,
    dataset_download,
    delete_dataset,
)
from ..storage import Storage
from .deps import client_ip, current_customer, get_db, get_settings, get_storage

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


@router.get("")
def list_datasets(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, max_length=120),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_datasets(customer["id"], page, size, q)
    return ok(page_data([dataset_public(row) for row in rows], total, page, size))


@router.post("")
async def upload_dataset(
    request: Request,
    name: str | None = Form(None),
    description: str | None = Form(None),
    files: list[UploadFile] = File(...),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    dataset, dataset_files = await create_dataset(
        db=db,
        storage=storage,
        settings=settings,
        customer=customer,
        name=name,
        description=description,
        uploads=files,
        ip=client_ip(request),
    )
    return ok(dataset_public(dataset, dataset_files), message="数据集已上传", code=201)


@router.get("/{dataset_id}")
def get_dataset(
    dataset_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    dataset = db.get_dataset(customer["id"], dataset_id)
    if dataset is None:
        raise ApiError("数据集不存在", 404)
    return ok(dataset_public(dataset, db.get_dataset_files(dataset_id)))


@router.get("/{dataset_id}/download")
async def download_dataset(
    request: Request,
    dataset_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileResponse:
    path, filename = await run_in_threadpool(
        dataset_download, db=db, storage=storage, customer=customer, dataset_id=dataset_id
    )
    db.add_activity(
        customer["id"],
        "dataset.download",
        target_type="dataset",
        target_id=dataset_id,
        detail=filename,
        ip=client_ip(request),
    )
    return FileResponse(Path(path), filename=filename)


@router.delete("/{dataset_id}")
def remove_dataset(
    request: Request,
    dataset_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    delete_dataset(
        db=db,
        storage=storage,
        customer=customer,
        dataset_id=dataset_id,
        ip=client_ip(request),
    )
    return ok(None, message="数据集已删除")
