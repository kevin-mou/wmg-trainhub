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


def resolve_default_kernel(settings: Settings) -> str | None:
    kernels = list_kernels()
    names = {item["name"] for item in kernels}
    if settings.default_kernel in names:
        return settings.default_kernel
    if FALLBACK_KERNEL_NAME in names:
        return FALLBACK_KERNEL_NAME
    return kernels[0]["name"] if kernels else None


def ensure_default_kernel(settings: Settings) -> None:
    """机器上一个 kernelspec 都没有时，为服务自身解释器注册一个内核。

    部署在 GPU 节点上时，通常已经通过 `python -m ipykernel install` 注册了带
    CUDA 环境的 kernelspec；这里只是保证控制台开箱即用。
    """
    if not jupyter_available():
        logger.warning("未检测到 Jupyter 执行栈，训练任务将无法执行（请安装 requirements.txt）")
        return
    if list_kernels():
        return
    kernel_dir = settings.data_dir / "jupyter" / "kernels" / FALLBACK_KERNEL_NAME
    kernel_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "Python 3 (TrainHub)",
        "language": "python",
    }
    kernel_file = kernel_dir / "kernel.json"
    kernel_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    extra = os.environ.get("JUPYTER_PATH", "")
    os.environ["JUPYTER_PATH"] = str(settings.data_dir / "jupyter") + (
        os.pathsep + extra if extra else ""
    )
    logger.info("已注册内置内核 kernelspec：%s", Path(kernel_file))
