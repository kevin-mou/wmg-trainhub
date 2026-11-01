"""共享模型缓存：本地判断 + 并发安全的 Hugging Face 下载。

缓存布局（根目录由 TrainHub 注入为 ``WMG_MODEL_CACHE``）::

    <root>/models--org--name/...   huggingface_hub 缓存（直接作为 cache_dir）
    <root>/.trainhub-locks/        仓库级下载锁（跨进程）
    <root>/xet/                    hf_xet 分块缓存

``TRAINHUB_MODEL_DIR`` 可以指向一个已有的 HF 缓存目录（例如 Notebook 过去用
``snapshot_download(cache_dir=...)`` 下载出来的目录），无需迁移文件。

设计要点：

- **只在缓存缺失时联网**：先以 ``local_files_only=True`` 查缓存，命中直接返回快照路径；
  未命中才在仓库级文件锁内下载，避免多个内核同时拉同一个基座。
- **HF 是唯一下载实现**：不自己拼 URL、不自己写进度；``snapshot_download`` 负责断点、
  校验与去重。
- **离线模式**：``local_files_only=True`` 或环境变量 ``TRAINHUB_MODEL_OFFLINE=1`` /
  ``HF_HUB_OFFLINE=1`` 时只读缓存，缺失立即报错（气隙节点的可预期行为）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Hugging Face 模型仓库 ID：namespace/name
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def model_cache_root(explicit: str | Path | None = None) -> Path:
    """解析缓存根目录：显式参数 → ``WMG_MODEL_CACHE`` → ``TRAINHUB_MODEL_DIR`` → 用户缓存。"""
    if explicit is not None:
        raw = str(explicit).strip()
        if raw:  # 空串按"未指定"处理：Path('') 会落成当前目录，不能当成缓存根
            return Path(raw).expanduser()
    value = (
        os.environ.get("WMG_MODEL_CACHE") or os.environ.get("TRAINHUB_MODEL_DIR") or ""
    ).strip()
    if value:
        return Path(value).expanduser()
    return Path.home() / ".cache" / "trainhub-models"


def _env_true(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _say(message: str, quiet: bool) -> None:
    if not quiet:
        print(f"[trainhub] {message}", flush=True)


def _state_dir(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _export_cache_env(root: Path) -> None:
    """把共享缓存根同步给同进程的 HF 生态（仅在调用方未显式设置时）。

    TrainHub 内核/训练任务里这两个变量已由服务注入；这里补的是**不经平台注入**的进程
    （运维手工执行 ``models pull``、本地调试），让 hf_xet 分块缓存与 HF 库也落到同一根，
    而不是散落到各自的 ``~/.cache``。
    """
    os.environ.setdefault("HF_HUB_CACHE", str(root))
    os.environ.setdefault("HF_XET_CACHE", str(root / "xet"))


def _check_required(path: Path, required_files: Sequence[str] | None) -> None:
    if not required_files:
        return
    missing = [name for name in required_files if not (path / name).exists()]
    if missing:
        raise RuntimeError(f"模型目录缺少文件：{', '.join(missing)}（{path}）")


def _lock(root: Path, repo_id: str, revision: str | None):
    """仓库级跨进程锁；filelock 缺失时退化为无锁（HF 自身仍有逐文件锁）。"""
    lock_dir = _state_dir(root, ".trainhub-locks")
    try:
        from filelock import FileLock
    except Exception:  # noqa: BLE001 - 助手要能在最小环境里导入
        return contextlib.nullcontext()
    key = f"{repo_id}@{revision or 'main'}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    safe = _SAFE_NAME.sub("--", repo_id).strip("-") or "model"
    return FileLock(str(lock_dir / f"{safe}--{digest}.lock"))


def _snapshot_download(**kwargs: Any) -> str:
    # 延迟导入：应用 venv 不装 huggingface_hub，只有训练内核环境需要它。
    from huggingface_hub import snapshot_download

    return str(snapshot_download(**kwargs))


def _download(
    repo_id: str,
    revision: str | None,
    hub_dir: Path,
    *,
    allow_patterns: Sequence[str] | None,
    ignore_patterns: Sequence[str] | None,
    local_files_only: bool,
) -> str:
    kwargs: dict[str, Any] = {
        "repo_id": repo_id,
        "cache_dir": str(hub_dir),
        "local_files_only": local_files_only,
    }
    if revision:
        kwargs["revision"] = revision
    if allow_patterns:
        kwargs["allow_patterns"] = list(allow_patterns)
    if ignore_patterns:
        kwargs["ignore_patterns"] = list(ignore_patterns)
    return _snapshot_download(**kwargs)


def ensure_model(
    ref: str | Path,
    *,
    revision: str | None = None,
    cache_root: str | Path | None = None,
    required_files: Sequence[str] | None = None,
    allow_patterns: Sequence[str] | None = None,
    ignore_patterns: Sequence[str] | None = None,
    local_files_only: bool = False,
    refresh: bool = False,
    quiet: bool = False,
) -> str:
    """确保模型可用，返回本地目录（快照路径）。

    ``ref`` 可以是本地目录（直接校验并返回），也可以是 Hugging Face 仓库 ID。
    命中共享缓存时不联网；未命中时在仓库级锁内用 ``snapshot_download`` 下载。
    ``refresh=True`` 跳过缓存命中判断，让 Hugging Face 重新校验远端（etag 相同的文件不会重传），
    用于需要跟随 ``main`` 更新的场景；可复现训练请显式传 ``revision``。
    """
    raw = str(ref).strip()
    if not raw:
        raise RuntimeError("模型引用不能为空")

    local = Path(raw).expanduser()
    if local.is_dir():
        resolved = local.resolve()
        _check_required(resolved, required_files)
        return str(resolved)

    if not _REPO_ID.match(raw):
        raise RuntimeError(
            f"无法识别的模型引用：{raw!r}（应为本地目录或 Hugging Face 仓库 ID，如 org/model）"
        )
    if refresh and local_files_only:
        raise RuntimeError("refresh=True 与 local_files_only=True 不能同时使用")

    root = model_cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    _export_cache_env(root)
    hub_dir = root
    offline = local_files_only or _env_true("TRAINHUB_MODEL_OFFLINE") or _env_true("HF_HUB_OFFLINE")
    label = f"{raw}@{revision or 'main'}"

    def cached() -> str | None:
        try:
            path = _download(
                raw,
                revision,
                hub_dir,
                allow_patterns=allow_patterns,
                ignore_patterns=ignore_patterns,
                local_files_only=True,
            )
            _check_required(Path(path), required_files)
            return path
        except Exception:  # noqa: BLE001 - 缓存缺失/不完整统一走下载或离线报错
            return None

    if not refresh:
        hit = cached()
        if hit:
            _say(f"模型缓存命中：{label} → {hit}", quiet)
            return hit
        if offline:
            raise RuntimeError(f"模型未缓存：{label}（缓存目录 {hub_dir}，当前为离线模式）")

    with _lock(root, raw, revision):
        # 等锁期间可能已有其他进程下完，再确认一次，避免重复下载。
        if not refresh:
            hit = cached()
            if hit:
                _say(f"模型缓存命中：{label} → {hit}", quiet)
                return hit
        _say(f"下载模型：{label} → {hub_dir}", quiet)
        path = _download(
            raw,
            revision,
            hub_dir,
            allow_patterns=allow_patterns,
            ignore_patterns=ignore_patterns,
            local_files_only=False,
        )
    _check_required(Path(path), required_files)
    _say(f"模型就绪：{path}", quiet)
    return path


def cached_model(
    ref: str | Path,
    *,
    revision: str | None = None,
    cache_root: str | Path | None = None,
    required_files: Sequence[str] | None = None,
) -> str | None:
    """只查缓存、不联网；可用时返回本地目录，否则返回 None。"""
    try:
        return ensure_model(
            ref,
            revision=revision,
            cache_root=cache_root,
            required_files=required_files,
            local_files_only=True,
            quiet=True,
        )
    except RuntimeError:
        return None
