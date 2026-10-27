from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from . import __version__


def _nvidia_gpus() -> list[dict[str, Any]]:
    """只探测 NVIDIA（通过 nvidia-smi），不触发 macOS 的 system_profiler。"""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [
        {"name": line.strip(), "vendor": "nvidia"}
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def environment_snapshot(
    *,
    requested_kernel: str | None,
    kernel_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """采集一次训练启动时的执行环境快照。

    区分 ``service``（TrainHub 进程）与 ``kernel``（Notebook 实际内核）：
    生产上训练内核通常来自另一个 conda 环境，把服务进程的 Python 当成
    内核 Python 会误导复现。GPU 只探测 NVIDIA；Apple GPU 的实时信息仍由
    executor/status 提供，避免启动时调用 system_profiler。
    """
    spec = kernel_spec if isinstance(kernel_spec, dict) else {}
    return {
        "trainhubVersion": __version__,
        "service": {
            "pythonVersion": platform.python_version(),
            "pythonExecutable": sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or None,
            "hostname": platform.node() or None,
            "cpuCount": os.cpu_count(),
        },
        "kernel": {
            "requested": requested_kernel or None,
            "effective": str(spec.get("name") or "").strip() or None,
            "displayName": str(spec.get("display_name") or "").strip() or None,
            "language": str(spec.get("language") or "").strip() or None,
        },
        "gpu": _nvidia_gpus(),
        "capturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
