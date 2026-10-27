"""节点系统信息采集（CPU / GPU）。

CUDA 节点用 nvidia-smi；macOS/MLX 节点用系统接口报告 Apple GPU 与 CPU。
只做只读探测，失败返回空/None，不影响 TrainHub 服务。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from typing import Any, Optional


def _run(argv: list[str], timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _nvidia_gpus() -> list[dict[str, Any]]:
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    cards: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            total = float(parts[1])
            used = float(parts[2])
            util = float(parts[3])
        except ValueError:
            continue
        cards.append(
            {
                "name": parts[0],
                "vendor": "nvidia",
                "memoryTotalMb": int(total),
                "memoryUsedMb": int(used),
                "utilizationPercent": util,
            }
        )
    return cards


def _apple_gpus() -> list[dict[str, Any]]:
    if sys.platform != "darwin":
        return []
    out = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=8.0)
    if not out:
        return []
    try:
        payload = json.loads(out)
    except ValueError:
        return []
    cards: list[dict[str, Any]] = []
    for item in payload.get("SPDisplaysDataType") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("sppci_model") or item.get("_name") or "Apple GPU")
        cores: Optional[int] = None
        raw_cores = item.get("sppci_cores")
        if raw_cores is not None:
            try:
                cores = int(str(raw_cores).strip())
            except ValueError:
                cores = None
        cards.append(
            {
                "name": name,
                "vendor": "apple",
                "cores": cores,
                "metal": item.get("spdisplays_mtlgpufamilysupport"),
                "unifiedMemory": True,
                "memoryTotalMb": None,
                "memoryUsedMb": None,
                "utilizationPercent": None,
            }
        )
    return cards


def gpu_info() -> list[dict[str, Any]]:
    cards = _nvidia_gpus()
    if cards:
        return cards
    return _apple_gpus()


def _cpu_model() -> Optional[str]:
    if sys.platform == "darwin":
        value = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        if value:
            return value
        value = _run(["sysctl", "-n", "hw.model"]).strip()
        if value:
            return value
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or None


def _memory_mb() -> tuple[Optional[int], Optional[int]]:
    if sys.platform == "darwin":
        raw = _run(["sysctl", "-n", "hw.memsize"]).strip()
        try:
            return (int(raw) // (1024 * 1024), None) if raw else (None, None)
        except ValueError:
            return (None, None)
    if sys.platform.startswith("linux"):
        total = available = None
        try:
            with open("/proc/meminfo", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) // 1024 if line.split()[1].isdigit() else None
                    elif line.startswith("MemAvailable:"):
                        available = (
                            int(line.split()[1]) // 1024 if line.split()[1].isdigit() else None
                        )
        except (OSError, ValueError):
            pass
        used = total - available if total is not None and available is not None else None
        return total, used
    return None, None


def cpu_info() -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = None
    total_mb, used_mb = _memory_mb()
    return {
        "model": _cpu_model(),
        "cores": os.cpu_count(),
        "load1": round(load1, 2) if load1 is not None else None,
        "load5": round(load5, 2) if load5 is not None else None,
        "load15": round(load15, 2) if load15 is not None else None,
        "memoryTotalMb": total_mb,
        "memoryUsedMb": used_mb,
        "memoryUnified": sys.platform == "darwin",
    }
