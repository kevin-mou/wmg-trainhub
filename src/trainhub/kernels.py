from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from .config import Settings

logger = logging.getLogger("trainhub.kernels")

FALLBACK_KERNEL_NAME = "python3"


def jupyter_available() -> bool:
    """服务端是否具备 Jupyter 执行栈。"""
    try:
        import ipykernel  # noqa: F401
        import nbclient  # noqa: F401
        import nbformat  # noqa: F401
    except Exception:
        return False
    return True


def list_kernels() -> list[dict[str, str]]:
    """枚举本机可用的 Jupyter 内核。"""
    try:
        from jupyter_client.kernelspec import KernelSpecManager
    except ImportError:
        return []
    try:
        specs = KernelSpecManager().get_all_specs()
    except Exception:  # 内核目录损坏等情况下不影响服务运行
        logger.warning("读取 Jupyter kernelspec 失败", exc_info=True)
        return []
    kernels = []
    for name, payload in sorted(specs.items()):
        spec = payload.get("spec") or {}
        kernels.append(
            {
                "name": name,
                "displayName": spec.get("display_name") or name,
                "language": spec.get("language") or "python",
            }
        )
    return kernels


def default_kernel_name() -> str | None:
    """仅供 UI 预选：Jupyter 惯例的默认 python3；不存在则第一个可用内核。

    执行时不强制使用该值：任务未指定内核时交给 nbclient/Jupyter 决定。
    """
    names = [item["name"] for item in list_kernels()]
    if not names:
        return None
    return FALLBACK_KERNEL_NAME if FALLBACK_KERNEL_NAME in names else names[0]
