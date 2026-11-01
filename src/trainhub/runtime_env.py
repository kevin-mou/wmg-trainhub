from __future__ import annotations

import os
from pathlib import Path

from .config import PROJECT_ROOT, Settings

# Notebook 运行时助手包（trainhub_runtime）所在目录：追加进内核 PYTHONPATH，
# 让 Notebook 可以直接 `from trainhub_runtime import ensure_model`，而不把助手装进训练 venv。
RUNTIME_PACKAGE_ROOT = PROJECT_ROOT / "runtime"

# 本机内核只继承运行所需的非敏感变量；TRAINHUB_ADMIN_TOKEN / HF_TOKEN / DB 凭据等
# 默认不进入内核。需要额外变量时用 TRAINHUB_KERNEL_ENV_ALLOW 显式放行。
_SAFE_HOST_ENV_KEYS = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "CONDA_PREFIX",
    "VIRTUAL_ENV",
    "CUDA_HOME",
    "CUDA_PATH",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "HF_ENDPOINT",
    "HF_HUB_DISABLE_TELEMETRY",
    "TOKENIZERS_PARALLELISM",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
}

# 容器内只保留不依赖宿主路径、非敏感的变量；PATH/HOME/TMPDIR 由镜像与沙箱决定。
_CONTAINER_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "HF_ENDPOINT",
    "HF_HUB_DISABLE_TELEMETRY",
    "TOKENIZERS_PARALLELISM",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
}


def host_base_env(settings: Settings) -> dict[str, str]:
    """本机内核的最小环境：白名单继承宿主变量，绝不整份复制 os.environ。"""
    keys = set(_SAFE_HOST_ENV_KEYS)
    for key in (getattr(settings, "kernel_env_allow", "") or "").split(","):
        key = key.strip()
        if key:
            keys.add(key)
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.setdefault("PATH", os.defpath)
    return env


def sandbox_env(
    settings: Settings,
    host_env: dict[str, str],
    *,
    home_dir: Path,
    model_dir: Path,
    network: str,
) -> dict[str, str]:
    """容器环境：只带 WMG 契约、运行助手与非敏感基础变量。"""
    env = {key: host_env[key] for key in _CONTAINER_ENV_KEYS if key in host_env}
    for key, value in host_env.items():
        if key.startswith("WMG_"):
            env[key] = value
    env.update(
        {
            "HOME": str(home_dir),
            "USER": "trainhub",
            "LOGNAME": "trainhub",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": "/tmp",
            "TEMP": "/tmp",
            "TMP": "/tmp",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(RUNTIME_PACKAGE_ROOT),
            "WMG_MODEL_CACHE": str(model_dir),
            "HF_HUB_CACHE": str(model_dir),
            "HF_XET_CACHE": "/tmp/hf-xet-cache",
        }
    )
    if network == "none":
        env.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
            }
        )
    return env


def ensure_model_dirs(settings: Settings) -> Path:
    """创建共享模型缓存目录（HF cache 根 / 锁 / xet 缓存），返回缓存根目录。"""
    root = settings.model_dir_path
    for sub in (".trainhub-locks", "xet"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def kernel_runtime_env(
    settings: Settings, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    """内核 / 训练任务的统一运行时环境。

    - ``WMG_MODEL_CACHE``：TrainHub 服务级模型缓存根目录（Notebook 助手按它解析）
    - ``HF_HUB_CACHE`` / ``HF_XET_CACHE``：让 huggingface_hub / transformers 默认复用同一份
      缓存，避免每个会话的 HOME 彼此隔离导致重复下载
    - ``PYTHONPATH``：追加运行时助手包目录（保留调用方已有的 PYTHONPATH）
    """
    root = ensure_model_dirs(settings)
    base = base_env if base_env is not None else {}
    existing = str(base.get("PYTHONPATH") or "")
    parts: list[str] = [str(RUNTIME_PACKAGE_ROOT)]
    parts.extend(
        part for part in existing.split(os.pathsep) if part and part != str(RUNTIME_PACKAGE_ROOT)
    )
    return {
        "WMG_MODEL_CACHE": str(root),
        "HF_HUB_CACHE": str(root),
        "HF_XET_CACHE": str(root / "xet"),
        "PYTHONPATH": os.pathsep.join(parts),
    }
