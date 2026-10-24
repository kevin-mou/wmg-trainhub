from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值：{raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} 不能小于 {minimum}，当前值：{value}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """加载 `.env`（进程环境优先，与 start.sh 的 source 语义对齐）。

    CLI 命令不经过 start.sh：没有这一步时 `trainhub.cli create-customer` 会写默认
    data 目录，而 `./start.sh` 启动的服务读 `.env` 指定的目录 —— 两边库不一致，
    表现为“客户创建成功但令牌登录无效”（2026-10-24 实际踩到）。
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    admin_token: str | None
    host: str
    port: int
    max_concurrent_runs: int
    default_timeout_seconds: int
    cell_timeout_seconds: int
    max_dataset_mb: int
    max_notebook_mb: int
    max_log_mb: int
    keep_run_workdir: bool
    default_kernel: str
    log_level: str

    @property
    def max_dataset_bytes(self) -> int:
        return self.max_dataset_mb * 1024 * 1024

    @property
    def max_notebook_bytes(self) -> int:
        return self.max_notebook_mb * 1024 * 1024

    @property
    def max_log_bytes(self) -> int:
        return self.max_log_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(PROJECT_ROOT / ".env")
        data_dir = Path(os.environ.get("TRAINHUB_DATA_DIR") or (PROJECT_ROOT / "data")).expanduser()
        db_path = Path(
            os.environ.get("TRAINHUB_DB_PATH") or (data_dir / "trainhub.db")
        ).expanduser()
        admin_token = (os.environ.get("TRAINHUB_ADMIN_TOKEN") or "").strip() or None
        return cls(
            data_dir=data_dir,
            db_path=db_path,
            admin_token=admin_token,
            host=os.environ.get("TRAINHUB_HOST", "0.0.0.0"),
            port=_int_env("TRAINHUB_PORT", 7470, minimum=1),
            max_concurrent_runs=_int_env("TRAINHUB_MAX_CONCURRENT_RUNS", 1, minimum=1),
            default_timeout_seconds=_int_env("TRAINHUB_RUN_TIMEOUT_SECONDS", 0),
            cell_timeout_seconds=_int_env("TRAINHUB_CELL_TIMEOUT_SECONDS", 0),
            max_dataset_mb=_int_env("TRAINHUB_MAX_DATASET_MB", 10240),
            max_notebook_mb=_int_env("TRAINHUB_MAX_NOTEBOOK_MB", 10),
            max_log_mb=_int_env("TRAINHUB_MAX_LOG_MB", 100),
            keep_run_workdir=_bool_env("TRAINHUB_KEEP_RUN_WORKDIR", False),
            default_kernel=(os.environ.get("TRAINHUB_DEFAULT_KERNEL") or "python3").strip(),
            log_level=os.environ.get("TRAINHUB_LOG_LEVEL", "info").lower(),
        )
