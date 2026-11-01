from __future__ import annotations

import os
import random
import shutil
import time
from typing import Any

from jupyter_client import KernelManager
from jupyter_client.asynchronous import AsyncKernelClient

from .sandbox import DockerSandbox, SandboxSpec

_PORT_RANGE = range(10_000, 60_000)


class SandboxKernelManager(KernelManager):
    """通过 Unix socket（IPC）连接 Docker 容器内的 ipykernel。

    KernelManager 在本机只负责协议客户端；真正的内核进程在容器里。IPC 目录
    同时挂载到容器同路径，因此宿主客户端和容器内核看到的是同一组 socket 文件。
    """

    client_factory = AsyncKernelClient

    def __init__(
        self,
        *,
        sandbox: DockerSandbox,
        spec: SandboxSpec,
        kernel_name: str = "python3",
        **kwargs: Any,
    ) -> None:
        super().__init__(kernel_name=kernel_name, **kwargs)
        self.transport = "ipc"
        self.ip = str(spec.ipc_dir / "kernel-ipc")
        self.connection_file = str(spec.ipc_dir / "kernel.json")
        ports = random.SystemRandom().sample(_PORT_RANGE, 5)
        self.shell_port = ports[0]
        self.iopub_port = ports[1]
        self.stdin_port = ports[2]
        self.hb_port = ports[3]
        self.control_port = ports[4]
        self._sandbox = sandbox
        self._spec = spec
        self._container_name = spec.name
        self._started = False
        self._alive = False
        self._alive_at = 0.0

    @property
    def container_name(self) -> str:
        return self._container_name

    @property
    def has_kernel(self) -> bool:
        return self._started

    def start_kernel(self, **kwargs: Any) -> None:
        if self._started:
            return
        self._spec.ipc_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.write_connection_file()
        try:
            os.chmod(self.connection_file, 0o600)
        except OSError:
            pass
        try:
            self._sandbox.start_container(self._spec)
        except Exception:
            self._cleanup_ipc()
            raise
        self._started = True
        self._alive = True
        self._alive_at = time.monotonic()

    def is_alive(self) -> bool:
        if not self._started:
            return False
        now = time.monotonic()
        if now - self._alive_at < 2.0:
            return self._alive
        self._alive = self._sandbox.container_running(self._container_name)
        self._alive_at = now
        return self._alive

    def interrupt_kernel(self) -> None:
        self._sandbox.interrupt_container(self._container_name)

    def shutdown_kernel(self, now: bool = False, restart: bool = False) -> None:
        # rm -f 立即结束内核；不再等待 stop 的宽限期，避免阻塞事件循环。
        self._sandbox.remove_container(self._container_name)
        self._started = False
        self._alive = False
        self._cleanup_ipc()

    def cleanup_resources(self, restart: bool = False) -> None:
        if self._started:
            self.shutdown_kernel(now=True)
        self._sandbox.remove_container(self._container_name)
        self._cleanup_ipc()

    def _cleanup_ipc(self) -> None:
        try:
            self.cleanup_ipc_files()
        except Exception:
            pass
        try:
            shutil.rmtree(self._spec.ipc_dir, ignore_errors=True)
        except OSError:
            pass

    def logs(self, tail: int = 100) -> str:
        return self._sandbox.logs(self._container_name, tail=tail)
