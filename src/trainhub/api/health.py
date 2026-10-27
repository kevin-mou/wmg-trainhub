from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import Settings
from ..kernels import default_kernel_name, jupyter_available, list_kernels
from ..responses import ok
from .deps import current_customer, get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    """基础设施探活（不需要令牌）。"""
    return {"status": "ok"}


@router.get("/api/health")
def health(
    customer: dict = Depends(current_customer), settings: Settings = Depends(get_settings)
) -> dict:
    kernels = list_kernels()
    return ok(
        {
            "status": "ok",
            "jupyterAvailable": jupyter_available(),
            "kernelCount": len(kernels),
            "defaultKernel": default_kernel_name(),
        }
    )
