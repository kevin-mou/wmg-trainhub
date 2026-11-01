from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _clear(target: Path) -> None:
    if not target.exists():
        return
    for child in target.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            continue


def sync_dataset_mount(files: Iterable[Mapping[str, Any]], target: Path) -> tuple[int, list[str]]:
    """把数据集文件挂载到 ``target``（符号链接，失败退化为复制）。

    - 先清空目标目录：``WMG_DATASET_DIR`` 是平台管理的挂载点，切换数据集即整体替换；
    - 单个文件缺失/文件名非法时跳过并返回文件名，由调用方决定如何提示；
    - 返回 ``(成功数, 缺失/失败的文件名列表)``。
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    _clear(target)
    linked = 0
    missing: list[str] = []
    for item in files:
        name = str(item.get("filename") or "").strip().replace("\\", "/")
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            missing.append(name or "?")
            continue
        source = Path(str(item.get("storage_path") or "")).expanduser()
        if not source.is_absolute():
            source = source.resolve()
        if not source.is_file():
            missing.append(name)
            continue
        destination = target.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(source, destination)
            linked += 1
        except OSError:
            try:
                shutil.copy2(source, destination)
                linked += 1
            except OSError:
                missing.append(name)
    return linked, missing
