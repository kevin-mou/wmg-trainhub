from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .responses import ApiError

SANDBOX_LABEL = "com.wmg.trainhub.sandbox"
KIND_LABEL = "com.wmg.trainhub.kind"
KEY_LABEL = "com.wmg.trainhub.key"
CUSTOMER_LABEL = "com.wmg.trainhub.customer"
CONTAINER_PREFIX = "trainhub-sbx-"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class SandboxMount:
    source: Path
    read_only: bool = True


@dataclass
class SandboxSpec:
    """一个内核容器的完整启动描述（路径均为宿主机绝对路径，容器内同路径挂载）。"""

    name: str
    kind: str
    key: str
    customer_id: str
    work_dir: Path
    cwd: Path
    output_dir: Path
    home_dir: Path
    ipc_dir: Path
    env: dict[str, str]
    dataset_dirs: list[Path] = field(default_factory=list)
    logs_dir: Path | None = None
    model_dir: Path | None = None
    runtime_dir: Path | None = None
    python: str = "python3"


class DockerSandbox:
    """Linux 节点的 Docker 内核沙箱。

    - 每个内核（交互式会话或训练任务）一个短生命周期容器；
    - 只挂载会话目录、选中的数据集、共享模型缓存与运行时助手；
    - 默认 ``--network none``、只读根文件系统、丢弃全部 capability；
    - 不向容器传宿主环境变量（由调用方用最小环境构造）。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode = getattr(settings, "sandbox_mode", "none")
        self.image = getattr(settings, "sandbox_image", "trainhub-sandbox:latest")
        self.docker = getattr(settings, "sandbox_docker_bin", "docker")
        self.network = getattr(settings, "sandbox_network", "none")
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    @property
    def enabled(self) -> bool:
        return self.mode == "docker"

    # ------------------------------------------------------------------ helpers

    def _run(self, args: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
        # Docker CLI 需要继承宿主环境（DOCKER_HOST / context），但该环境不会进入容器。
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            check=False,
        )

    def _run_as(self) -> str:
        configured = (getattr(self.settings, "sandbox_run_as", "") or "").strip()
        if configured:
            return configured
        return f"{os.getuid()}:{os.getgid()}"

    def new_ipc_dir(self) -> Path:
        base = getattr(self.settings, "sandbox_ipc_dir", None) or Path("/var/tmp/trainhub-ipc")
        base = Path(base).expanduser()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            base.chmod(0o700)
        except OSError:
            pass
        directory = Path(tempfile.mkdtemp(prefix="k-", dir=str(base)))
        directory.chmod(0o700)
        # Unix socket 路径上限约 104 字节（macOS/BSD），留足余量。
        if len(str(directory / "kernel-99999")) > 96:
            shutil.rmtree(directory, ignore_errors=True)
            raise ApiError(
                "沙箱 IPC 目录路径过长，请把 TRAINHUB_SANDBOX_IPC_DIR 配到更短的路径", 500
            )
        return directory

    def _container_name(self, kind: str, key: str) -> str:
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-")[:40] or "x"
        return f"{CONTAINER_PREFIX}{kind}-{safe_key}"

    def container_name(self, kind: str, key: str) -> str:
        return self._container_name(kind, key)

    def _mounts(self, spec: SandboxSpec) -> list[SandboxMount]:
        mounts: list[SandboxMount] = [
            SandboxMount(spec.work_dir, read_only=False),
            SandboxMount(spec.home_dir, read_only=False),
            SandboxMount(spec.ipc_dir, read_only=False),
        ]
        if not _is_within(spec.output_dir, spec.work_dir):
            mounts.append(SandboxMount(spec.output_dir, read_only=False))
        if spec.logs_dir is not None:
            mounts.append(SandboxMount(spec.logs_dir, read_only=False))
        for directory in spec.dataset_dirs:
            mounts.append(SandboxMount(directory, read_only=True))
        if spec.model_dir is not None:
            mounts.append(
                SandboxMount(
                    spec.model_dir, read_only=getattr(self.settings, "sandbox_model_cache_ro", True)
                )
            )
        if spec.runtime_dir is not None:
            mounts.append(SandboxMount(spec.runtime_dir, read_only=True))
        seen: set[str] = set()
        unique: list[SandboxMount] = []
        for mount in mounts:
            path = Path(mount.source)
            if not path.is_absolute():
                raise ApiError(f"沙箱挂载路径必须是绝对路径：{path}", 500)
            key = str(path)
            if key in seen:
                continue
            if ":" in key or "," in key:
                raise ApiError(f"沙箱挂载路径包含不支持的字符：{path}", 500)
            if not path.exists():
                if mount.read_only:
                    continue
                path.mkdir(parents=True, exist_ok=True)
            seen.add(key)
            unique.append(mount)
        return unique

    # ------------------------------------------------------------------ config

    def preflight(self, *, refresh: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "ok": True,
                "mode": self.mode,
                "message": "Docker 沙箱未启用",
            }
        now = time.monotonic()
        if not refresh and self._status_cache and now - self._status_cache[0] < 30:
            return self._status_cache[1]
        result = self._preflight()
        self._status_cache = (now, result)
        return result

    def _preflight(self) -> dict[str, Any]:
        base = {
            "enabled": True,
            "mode": self.mode,
            "image": self.image,
            "docker": self.docker,
            "network": self.network,
        }
        if not sys.platform.startswith("linux"):
            return {
                **base,
                "ok": False,
                "message": "Docker 沙箱仅支持 Linux 节点；macOS/MLX 请使用 TRAINHUB_SANDBOX=none",
            }
        if os.getuid() == 0:
            return {
                **base,
                "ok": False,
                "message": "服务不能以 root 运行 Docker 沙箱；请使用非 root 服务用户",
            }
        run_as = (getattr(self.settings, "sandbox_run_as", "") or "").strip()
        if run_as and not re.fullmatch(r"\d+(?::\d+)?", run_as):
            return {
                **base,
                "ok": False,
                "message": f"TRAINHUB_SANDBOX_RUN_AS 格式应为 uid[:gid]：{run_as}",
            }
        if run_as.split(":", 1)[0] == "0":
            return {**base, "ok": False, "message": "沙箱不能以 root 运行"}
        if run_as and int(run_as.split(":", 1)[0]) != os.getuid():
            return {
                **base,
                "ok": False,
                "message": (
                    "TRAINHUB_SANDBOX_RUN_AS 的 uid 必须与服务用户一致，"
                    "否则无法访问会话目录与 IPC socket"
                ),
            }
        if self.network in {"host", "container"} or self.network.startswith("container:"):
            return {**base, "ok": False, "message": "沙箱不允许 host/container 网络"}
        try:
            version = self._run(
                [self.docker, "version", "--format", "{{.Server.Version}}"], timeout=5
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {**base, "ok": False, "message": f"无法执行 Docker CLI：{exc}"}
        if version.returncode != 0:
            return {
                **base,
                "ok": False,
                "message": f"Docker 守护进程不可用：{(version.stderr or version.stdout).strip()[:300]}",
            }
        try:
            image = self._run(
                [self.docker, "image", "inspect", "--format", "{{.Id}}", self.image],
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {**base, "ok": False, "message": f"检查沙箱镜像失败：{exc}"}
        if image.returncode != 0:
            return {
                **base,
                "ok": False,
                "message": (
                    f"沙箱镜像不存在：{self.image}；请先执行 "
                    "deploy/build-sandbox-image.sh 构建/拉取镜像"
                ),
            }
        try:
            probe_dir = self.new_ipc_dir()
            shutil.rmtree(probe_dir, ignore_errors=True)
            probe = getattr(self.settings, "sandbox_ipc_dir", None) or Path("/var/tmp/trainhub-ipc")
            probe.mkdir(parents=True, exist_ok=True, mode=0o700)
        except (OSError, ApiError) as exc:
            return {**base, "ok": False, "message": f"沙箱 IPC 目录不可写：{exc}"}
        return {
            **base,
            "ok": True,
            "dockerVersion": version.stdout.strip(),
            "imageId": image.stdout.strip(),
            "runAs": self._run_as(),
            "message": "Docker 沙箱可用",
        }

    def ensure_available(self) -> dict[str, Any]:
        status = self.preflight()
        if not status.get("ok"):
            raise ApiError(f"Docker 沙箱不可用：{status.get('message')}", 503)
        return status

    def status(self) -> dict[str, Any]:
        return self.preflight()

    # ------------------------------------------------------------------ lifecycle

    def build_run_args(self, spec: SandboxSpec) -> list[str]:
        if not self.enabled:
            raise ApiError("Docker 沙箱未启用", 503)
        if not _NAME_RE.match(spec.name):
            raise ApiError(f"非法容器名：{spec.name}", 500)
        run_as = self._run_as()
        if not re.fullmatch(r"\d+(?::\d+)?", run_as) or run_as.split(":", 1)[0] == "0":
            raise ApiError(f"非法沙箱运行用户：{run_as}（必须是非 root 的 uid[:gid]）", 500)
        if self.network in {"host", "container"} or self.network.startswith("container:"):
            raise ApiError("沙箱不允许 host/container 网络", 500)
        args = [
            self.docker,
            "run",
            "-d",
            "--name",
            spec.name,
            "--label",
            f"{SANDBOX_LABEL}=1",
            "--label",
            f"{KIND_LABEL}={spec.kind}",
            "--label",
            f"{KEY_LABEL}={spec.key}",
            "--label",
            f"{CUSTOMER_LABEL}={spec.customer_id}",
            "--user",
            run_as,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--init",
            "--pids-limit",
            str(getattr(self.settings, "sandbox_pids_limit", 512)),
            "--memory",
            f"{getattr(self.settings, 'sandbox_memory_mb', 4096)}m",
            "--cpus",
            str(getattr(self.settings, "sandbox_cpus", 2.0)),
            "--network",
            self.network,
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",
            "--tmpfs",
            "/run:rw,nosuid,nodev,size=64m",
            "--shm-size",
            "512m",
            "--ulimit",
            "nofile=1024:8192",
            "--stop-timeout",
            "5",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=10m",
            "--log-opt",
            "max-file=1",
            "--workdir",
            str(spec.cwd),
        ]
        if (getattr(self.settings, "sandbox_gpus", "") or "").strip():
            args.extend(["--gpus", (getattr(self.settings, "sandbox_gpus", "") or "").strip()])
        for mount in self._mounts(spec):
            suffix = "ro" if mount.read_only else "rw"
            args.extend(["-v", f"{mount.source}:{mount.source}:{suffix}"])
        for key in sorted(spec.env):
            if not _ENV_KEY_RE.match(key):
                raise ApiError(f"非法沙箱环境变量名：{key}", 500)
            args.extend(["--env", f"{key}={spec.env[key]}"])
        args.append(self.image)
        args.extend(
            [
                spec.python,
                "-m",
                "ipykernel_launcher",
                "-f",
                str(spec.ipc_dir / "kernel.json"),
            ]
        )
        return args

    def start_container(self, spec: SandboxSpec) -> None:
        spec.ipc_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 同名残留（上次异常退出）先清掉，避免 docker run 名称冲突。
        self._run([self.docker, "rm", "-f", spec.name], timeout=10)
        args = self.build_run_args(spec)
        try:
            result = self._run(args, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(f"启动沙箱容器失败：{exc}", 503) from exc
        if result.returncode != 0:
            raise ApiError(
                f"启动沙箱容器失败：{(result.stderr or result.stdout).strip()[:500]}", 503
            )
        if not self.container_running(spec.name):
            logs = self.logs(spec.name)
            self.remove_container(spec.name)
            raise ApiError(f"沙箱内核容器未能保持运行：{logs[:800]}", 503)

    def container_running(self, name: str) -> bool:
        try:
            result = self._run(
                [self.docker, "inspect", "--format", "{{.State.Running}}", name], timeout=5
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def interrupt_container(self, name: str) -> None:
        try:
            self._run([self.docker, "kill", "--signal", "SIGINT", name], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def stop_container(self, name: str, *, timeout: int = 5) -> None:
        try:
            self._run([self.docker, "stop", "-t", str(timeout), name], timeout=timeout + 5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def remove_container(self, name: str) -> None:
        try:
            self._run([self.docker, "rm", "-f", name], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def cleanup_container(self, name: str) -> None:
        self.stop_container(name)
        self.remove_container(name)

    def logs(self, name: str, tail: int = 100) -> str:
        try:
            result = self._run([self.docker, "logs", "--tail", str(tail), name], timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"<无法读取容器日志：{exc}>"
        text = "\n".join(part for part in (result.stdout, result.stderr) if part)
        return text.strip()

    def reap_orphans(self, active_names: set[str] | None = None) -> int:
        """清理上次进程遗留的沙箱容器（重启恢复；active_names 内的保留）。"""
        if not self.enabled:
            return 0
        active = active_names or set()
        try:
            listed = self._run(
                [
                    self.docker,
                    "ps",
                    "-aq",
                    "--filter",
                    f"label={SANDBOX_LABEL}=1",
                ],
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0
        if listed.returncode != 0:
            return 0
        removed = 0
        for container_id in filter(None, listed.stdout.split()):
            try:
                inspected = self._run(
                    [self.docker, "inspect", "--format", "{{.Name}}", container_id],
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            name = inspected.stdout.strip().lstrip("/")
            if inspected.returncode != 0 or name in active:
                continue
            self.remove_container(container_id)
            removed += 1
        return removed

    def cleanup_ipc_root(self) -> int:
        """清理遗留的 IPC 目录（只在无活动会话的启动/停止阶段调用）。"""
        base = getattr(self.settings, "sandbox_ipc_dir", None) or Path("/var/tmp/trainhub-ipc")
        base = Path(base).expanduser()
        if not base.is_dir():
            return 0
        removed = 0
        for child in base.glob("k-*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
