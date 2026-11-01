from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .runtime_env import ensure_model_dirs


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _scan_files(base: Path, seen: set[tuple[int, int]] | None = None) -> tuple[int, float]:
    """统计目录下的真实文件占用（跟随符号链接、按 inode 去重）。

    huggingface_hub 1.x 的缓存把内容放在根级 ``blobs/``（多个模型共享），
    ``models--*`` 下的 ``snapshots/`` 是指向这些 blob 的符号链接；只数模型目录里
    的普通文件会漏掉绝大部分体积。
    """
    seen = seen if seen is not None else set()
    size = 0
    latest = 0.0
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                info = path.stat()  # 跟随符号链接，拿到真实文件
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
            size += info.st_size
            latest = max(latest, info.st_mtime)
    return size, latest


def model_cache_summary(settings: Settings) -> dict:
    """共享模型缓存的只读概览（服务级基础资源）。

    ``totalBytes`` 是缓存目录的真实磁盘占用（按 inode 去重，共享 blob 只算一次）；
    每个模型的 ``sizeBytes`` 是它引用的文件逻辑大小之和（多个模型共享同一份 blob 时
    会在各自的逻辑大小里重复计入），用于判断"哪个模型最占地方"。
    """
    root = ensure_model_dirs(settings)
    models: list[dict] = []
    total, _ = _scan_files(root)
    for entry in sorted(root.glob("models--*")):
        if not entry.is_dir():
            continue
        size, latest = _scan_files(entry)
        refs_dir = entry / "refs"
        refs = (
            sorted(item.name for item in refs_dir.iterdir() if item.is_file())
            if refs_dir.is_dir()
            else []
        )
        # HF 缓存目录把仓库 ID 的 "/" 编码成 "--"；仅用于展示。
        repo_id = entry.name[len("models--") :].replace("--", "/")
        models.append(
            {
                "repoId": repo_id,
                "sizeBytes": size,
                "refs": refs,
                "updatedAt": _iso(latest or entry.stat().st_mtime),
            }
        )
    models.sort(key=lambda item: item["sizeBytes"], reverse=True)
    return {
        "modelDir": str(root),
        "totalBytes": total,
        "models": models,
    }
