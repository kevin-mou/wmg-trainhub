from __future__ import annotations

import os
import shutil
import threading
import zipfile
from pathlib import Path

from .responses import ApiError

_ILLEGAL_NAME_CHARS = set('<>:"|?*\\/')


def safe_filename(raw: str | None) -> str:
    """把上传文件名收敛为安全的单段文件名（保留 Unicode）。"""
    name = (raw or "file").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in _ILLEGAL_NAME_CHARS)
    name = name.strip().strip(".")
    if not name:
        name = "file"
    return name[:180]


def unique_filename(existing: set[str], name: str) -> str:
    if name not in existing:
        return name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    for index in range(1, 1000):
        candidate = f"{stem} ({index}){dot}{suffix}"
        if candidate not in existing:
            return candidate
    return f"{stem} ({os.getpid()}){dot}{suffix}"


def resolve_within(base: Path, relative: str) -> Path:
    """把相对路径安全地解析到 base 之内，拒绝穿越与绝对路径。"""
    cleaned = relative.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/"):
        raise ApiError("非法文件路径", 400)
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ApiError("非法文件路径", 400)
    resolved_base = base.resolve()
    candidate = (resolved_base / Path(*parts)).resolve()
    if os.path.commonpath([str(resolved_base), str(candidate)]) != str(resolved_base):
        raise ApiError("非法文件路径", 400)
    return candidate


def build_zip(source_dir: Path, dest: Path) -> int:
    """把目录内容打包为 zip，返回写入的文件数。先写临时文件再原子替换。

    使用 os.walk(followlinks=False)，不跟随符号链接目录，避免把数据集/产物
    里指向外部的链接内容打进压缩包。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    count = 0
    source_root = source_dir.resolve()
    try:
        with zipfile.ZipFile(
            temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
        ) as archive:
            for current, dirs, files in os.walk(source_root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink())
                for name in sorted(files):
                    path = Path(current) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    archive.write(path, path.relative_to(source_root).as_posix())
                    count += 1
        os.replace(temp, dest)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return count


def iter_files(root: Path, limit: int = 2000) -> tuple[list[tuple[str, int]], bool]:
    """列出目录下所有普通文件（相对路径 + 字节数），返回 (列表, 是否被截断)。

    不跟随符号链接目录（训练产物里可能有指向外部的链接）。
    """
    files: list[tuple[str, int]] = []
    truncated = False
    if not root.exists():
        return files, truncated
    root_resolved = root.resolve()
    for current, dirs, names in os.walk(root_resolved, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink())
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append((path.relative_to(root_resolved).as_posix(), path.stat().st_size))
            if len(files) >= limit:
                return files, True
    return files, truncated


class Storage:
    """所有落盘路径的唯一事实源。"""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir).expanduser().resolve()
        self.customers_dir = self.root / "customers"
        self.jupyter_dir = self.root / "jupyter"

    def ensure_base_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.customers_dir.mkdir(parents=True, exist_ok=True)
        self.jupyter_dir.mkdir(parents=True, exist_ok=True)

    # -- customers / resources

    def customer_dir(self, customer_id: str) -> Path:
        return self.customers_dir / customer_id

    def dataset_dir(self, customer_id: str, dataset_id: str) -> Path:
        return self.customer_dir(customer_id) / "datasets" / dataset_id

    def dataset_files_dir(self, customer_id: str, dataset_id: str) -> Path:
        return self.dataset_dir(customer_id, dataset_id) / "files"

    def dataset_zip_path(self, customer_id: str, dataset_id: str) -> Path:
        return self.dataset_dir(customer_id, dataset_id) / "dataset.zip"

    def notebook_dir(self, customer_id: str, notebook_id: str) -> Path:
        return self.customer_dir(customer_id) / "notebooks" / notebook_id

    def notebook_path(self, customer_id: str, notebook_id: str, filename: str) -> Path:
        return self.notebook_dir(customer_id, notebook_id) / filename

    # -- runs

    def run_dir(self, customer_id: str, run_id: str) -> Path:
        return self.customer_dir(customer_id) / "runs" / run_id

    def run_work_dir(self, customer_id: str, run_id: str) -> Path:
        return self.run_dir(customer_id, run_id) / "work"

    def run_output_dir(self, customer_id: str, run_id: str) -> Path:
        return self.run_dir(customer_id, run_id) / "output"

    def run_log_path(self, customer_id: str, run_id: str) -> Path:
        return self.run_dir(customer_id, run_id) / "logs" / "run.log"

    def run_home_dir(self, customer_id: str, run_id: str) -> Path:
        return self.run_dir(customer_id, run_id) / "home"

    def run_tmp_dir(self, customer_id: str, run_id: str) -> Path:
        return self.run_dir(customer_id, run_id) / "tmp"

    # -- housekeeping

    def remove_tree(self, path: Path) -> None:
        resolved = path.resolve()
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise RuntimeError(f"拒绝删除数据目录之外的路径：{resolved}")
        shutil.rmtree(resolved, ignore_errors=True)

    def dir_size(self, path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        for current, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                item = Path(current) / name
                try:
                    if not item.is_symlink():
                        total += item.stat().st_size
                except OSError:
                    continue
        return total

    @staticmethod
    def disk_usage() -> dict[str, int]:
        usage = shutil.disk_usage("/")
        return {"totalBytes": usage.total, "freeBytes": usage.free, "usedBytes": usage.used}
