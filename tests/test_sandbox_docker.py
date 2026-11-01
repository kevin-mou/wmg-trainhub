from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from helpers import make_settings
from trainhub.runtime_env import RUNTIME_PACKAGE_ROOT
from trainhub.sandbox import DockerSandbox, SandboxSpec
from trainhub.sandbox_kernel import SandboxKernelManager

pytestmark = pytest.mark.skipif(
    os.environ.get("TRAINHUB_TEST_DOCKER_SANDBOX") != "1",
    reason="需要 Linux + Docker；设置 TRAINHUB_TEST_DOCKER_SANDBOX=1 启用",
)


async def test_real_docker_sandbox_kernel_executes(tmp_path):
    image = (
        os.environ.get("TRAINHUB_SANDBOX_TEST_IMAGE")
        or os.environ.get("TRAINHUB_SANDBOX_IMAGE")
        or "trainhub-sandbox:latest"
    )
    if not sys.platform.startswith("linux"):
        pytest.skip("Docker 沙箱仅支持 Linux")
    if not shutil.which("docker"):
        pytest.skip("docker CLI 不可用")
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip(f"Docker 守护进程或镜像不可用：{image}")

    base = tmp_path / "sandbox"
    work = base / "work"
    home = base / "home"
    ipc = Path(tempfile.mkdtemp(prefix="thipc-", dir="/var/tmp"))
    for directory in (work, home):
        directory.mkdir(parents=True, exist_ok=True)

    settings = make_settings(
        tmp_path,
        sandbox_mode="docker",
        sandbox_image=image,
        sandbox_ipc_dir=ipc,
    )
    sandbox = DockerSandbox(settings)
    spec = SandboxSpec(
        name=sandbox.container_name("test", "docker"),
        kind="test",
        key="docker",
        customer_id="test",
        work_dir=work,
        cwd=work,
        output_dir=work,
        home_dir=home,
        ipc_dir=ipc,
        env={
            "HOME": str(home),
            "TMPDIR": "/tmp",
            "PYTHONPATH": str(RUNTIME_PACKAGE_ROOT),
        },
        runtime_dir=RUNTIME_PACKAGE_ROOT,
        python="python3",
    )
    manager = SandboxKernelManager(sandbox=sandbox, spec=spec, kernel_name="python3")
    try:
        manager.start_kernel()
        client = manager.client()
        client.start_channels()
        await client.wait_for_ready(timeout=30)
        msg_id = client.execute("print('docker-sandbox-ok')", allow_stdin=False)
        output = ""
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                msg = await client.get_iopub_msg(timeout=1)
            except Exception:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            if msg.get("msg_type") == "stream":
                output += msg["content"].get("text", "")
            if msg.get("msg_type") == "status" and msg["content"].get("execution_state") == "idle":
                break
        assert "docker-sandbox-ok" in output
        client.stop_channels()
    finally:
        manager.shutdown_kernel(now=True)
        shutil.rmtree(ipc, ignore_errors=True)
