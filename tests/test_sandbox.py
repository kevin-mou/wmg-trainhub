from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
import pytest

from helpers import make_settings
from trainhub.runtime_env import RUNTIME_PACKAGE_ROOT, host_base_env, sandbox_env
from trainhub.sandbox import DockerSandbox, SandboxSpec
from trainhub.sandbox_kernel import SandboxKernelManager


def _spec(tmp_path: Path, *, name: str = "trainhub-sbx-session-test") -> SandboxSpec:
    work = tmp_path / "work"
    output = tmp_path / "output"
    home = tmp_path / "home"
    ipc = tmp_path / "ipc"
    dataset = tmp_path / "dataset"
    model = tmp_path / "models"
    runtime = tmp_path / "runtime"
    for directory in (work, output, home, ipc, dataset, model, runtime):
        directory.mkdir(parents=True, exist_ok=True)
    return SandboxSpec(
        name=name,
        kind="session",
        key="ks_test",
        customer_id="c1",
        work_dir=work,
        cwd=work,
        output_dir=output,
        home_dir=home,
        ipc_dir=ipc,
        env={"WMG_WORK_DIR": str(work), "HOME": str(home), "PYTHONUNBUFFERED": "1"},
        dataset_dirs=[dataset],
        model_dir=model,
        runtime_dir=runtime,
    )


def test_explicit_docker_rejected_on_macos(monkeypatch):
    if not sys.platform.startswith("darwin"):
        pytest.skip("只在 macOS 上验证平台守卫")
    monkeypatch.setenv("TRAINHUB_SANDBOX", "docker")
    from trainhub.config import Settings

    with pytest.raises(RuntimeError, match="Linux"):
        Settings.from_env()


def test_host_env_strips_sensitive_values(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAINHUB_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    settings = make_settings(tmp_path)
    env = host_base_env(settings)
    assert env["PATH"] == "/usr/bin:/bin"
    assert "TRAINHUB_ADMIN_TOKEN" not in env
    assert "HF_TOKEN" not in env
    assert "HOME" not in env


def test_sandbox_env_keeps_only_contract_and_safe_values(tmp_path):
    settings = make_settings(tmp_path)
    host = {
        "PATH": "/host/bin",
        "HOME": "/root",
        "HF_TOKEN": "secret",
        "LANG": "C.UTF-8",
        "WMG_WORK_DIR": "/data/work",
        "WMG_PARAMS": "{}",
    }
    env = sandbox_env(
        settings,
        host,
        home_dir=tmp_path / "home",
        model_dir=tmp_path / "models",
        network="none",
    )
    assert env["HOME"] == str(tmp_path / "home")
    assert env["TMPDIR"] == "/tmp"
    assert env["WMG_WORK_DIR"] == "/data/work"
    assert env["HF_HUB_CACHE"] == str(tmp_path / "models")
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["PYTHONPATH"] == str(RUNTIME_PACKAGE_ROOT)
    assert "PATH" not in env
    assert "HF_TOKEN" not in env


def test_build_run_args_is_hardened(tmp_path):
    settings = make_settings(
        tmp_path,
        sandbox_mode="docker",
        sandbox_image="trainhub-sandbox:test",
        sandbox_network="none",
        sandbox_memory_mb=2048,
        sandbox_cpus=1.5,
        sandbox_pids_limit=128,
    )
    sandbox = DockerSandbox(settings)
    spec = _spec(tmp_path)
    args = sandbox.build_run_args(spec)
    text = " ".join(args)

    assert args[0] == "docker"
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--memory") + 1] == "2048m"
    assert args[args.index("--cpus") + 1] == "1.5"
    assert args[args.index("--pids-limit") + 1] == "128"
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "--privileged" not in args
    assert "trainhub-sandbox:test" in args
    assert args[-5:] == [
        "python3",
        "-m",
        "ipykernel_launcher",
        "-f",
        str(spec.ipc_dir / "kernel.json"),
    ]
    assert "TRAINHUB_ADMIN_TOKEN" not in text
    assert "HF_TOKEN" not in text
    assert ".env" not in text
    assert f"{spec.work_dir}:{spec.work_dir}:rw" in text
    assert f"{spec.dataset_dirs[0]}:{spec.dataset_dirs[0]}:ro" in text
    assert f"{spec.model_dir}:{spec.model_dir}:ro" in text
    assert f"{spec.runtime_dir}:{spec.runtime_dir}:ro" in text


def test_preflight_platform_and_docker_checks(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path,
        sandbox_mode="docker",
        sandbox_image="trainhub-sandbox:test",
    )
    sandbox = DockerSandbox(settings)
    monkeypatch.setattr(sys, "platform", "darwin")
    status = sandbox.preflight(refresh=True)
    assert status["ok"] is False
    assert "Linux" in status["message"]

    monkeypatch.setattr(sys, "platform", "linux")
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=15.0):
        calls.append(args)
        if "version" in args:
            return subprocess.CompletedProcess(args, 0, "29.0.0\n", "")
        if "inspect" in args:
            return subprocess.CompletedProcess(args, 0, "sha256:abc\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(sandbox, "_run", fake_run)
    status = sandbox.preflight(refresh=True)
    assert status["ok"] is True
    assert status["imageId"] == "sha256:abc"
    assert status["runAs"] == f"{os.getuid()}:{os.getgid()}"
    assert any("version" in call for call in calls)


def test_sandbox_kernel_manager_starts_and_cleans_up(tmp_path):
    class FakeSandbox:
        def __init__(self):
            self.started = []
            self.removed = []
            self.running = True

        def start_container(self, spec):
            self.started.append(spec)

        def container_running(self, name):
            return self.running

        def interrupt_container(self, name):
            pass

        def cleanup_container(self, name):
            self.removed.append(name)

        def remove_container(self, name):
            self.removed.append(name)

        def logs(self, name, tail=100):
            return ""

    settings = make_settings(tmp_path, sandbox_mode="docker")
    fake = FakeSandbox()
    spec = _spec(tmp_path)
    manager = SandboxKernelManager(
        sandbox=fake,  # type: ignore[arg-type]
        spec=spec,
        kernel_name="python3",
    )
    manager.start_kernel()
    assert fake.started and fake.started[0].name == spec.name
    assert manager.transport == "ipc"
    assert Path(manager.connection_file).is_file()
    assert manager.is_alive() is True
    from jupyter_client.asynchronous import AsyncKernelClient

    client = manager.client()
    assert isinstance(client, AsyncKernelClient)
    assert client.transport == "ipc"
    manager.shutdown_kernel(now=True)
    assert spec.name in fake.removed
    assert not spec.ipc_dir.exists()


async def test_sandbox_kernel_manager_connects_to_ipc_kernel(tmp_path):
    import subprocess
    import tempfile

    class LocalKernelSandbox:
        def __init__(self):
            self.proc = None
            self.removed = []

        def start_container(self, spec):
            env = os.environ.copy()
            env.update({"HOME": str(spec.home_dir), "TMPDIR": str(spec.home_dir)})
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    str(spec.ipc_dir / "kernel.json"),
                ],
                cwd=str(spec.cwd),
                env=env,
            )

        def container_running(self, name):
            return self.proc is not None and self.proc.poll() is None

        def interrupt_container(self, name):
            pass

        def remove_container(self, name):
            self.removed.append(name)
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

        def logs(self, name, tail=100):
            return ""

    settings = make_settings(tmp_path, sandbox_mode="docker")
    ipc_dir = Path(tempfile.mkdtemp(prefix="thipc-", dir="/tmp"))
    spec = _spec(tmp_path)
    spec.ipc_dir = ipc_dir
    fake = LocalKernelSandbox()
    manager = SandboxKernelManager(sandbox=fake, spec=spec, kernel_name="python3")  # type: ignore[arg-type]
    try:
        manager.start_kernel()
        client = manager.client()
        client.start_channels()
        await client.wait_for_ready(timeout=15)
        msg_id = client.execute("print('sandbox-ipc-ok')", allow_stdin=False)
        output = ""
        deadline = time.time() + 10
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
        assert "sandbox-ipc-ok" in output
        client.stop_channels()
    finally:
        manager.shutdown_kernel(now=True)
        import shutil

        shutil.rmtree(ipc_dir, ignore_errors=True)


async def test_notebook_client_uses_sandbox_manager(tmp_path):
    import nbformat
    import subprocess
    import tempfile
    from nbclient import NotebookClient

    class LocalKernelSandbox:
        def __init__(self):
            self.proc = None

        def start_container(self, spec):
            env = os.environ.copy()
            env.update({"HOME": str(spec.home_dir), "TMPDIR": str(spec.home_dir)})
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    str(spec.ipc_dir / "kernel.json"),
                ],
                cwd=str(spec.cwd),
                env=env,
            )

        def container_running(self, name):
            return self.proc is not None and self.proc.poll() is None

        def interrupt_container(self, name):
            pass

        def remove_container(self, name):
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

        def logs(self, name, tail=100):
            return ""

    settings = make_settings(tmp_path, sandbox_mode="docker")
    ipc_dir = Path(tempfile.mkdtemp(prefix="thipc-", dir="/tmp"))
    spec = _spec(tmp_path)
    spec.ipc_dir = ipc_dir
    fake = LocalKernelSandbox()
    manager = SandboxKernelManager(sandbox=fake, spec=spec, kernel_name="python3")  # type: ignore[arg-type]
    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_code_cell("print('nb-sandbox-ok')")]
    try:
        client = NotebookClient(nb, km=manager, timeout=30)
        await client.async_execute(cwd=str(spec.cwd), env={})
        text = ""
        for cell in nb.cells:
            for output in cell.get("outputs", []):
                if output.get("output_type") == "stream":
                    text += str(output.get("text") or "")
        assert "nb-sandbox-ok" in text
    finally:
        manager.shutdown_kernel(now=True)
        import shutil

        shutil.rmtree(ipc_dir, ignore_errors=True)


async def test_kernel_session_sandbox_catalog_dataset_access(
    client, app, db, customer, headers, tmp_path
):
    import subprocess
    import tempfile

    from helpers import upload_dataset
    from trainhub.dataset_catalog import accessible_dataset_entries
    from trainhub.kernel_sessions import KernelSession

    class LocalSandbox(DockerSandbox):
        def __init__(self, settings):
            super().__init__(settings)
            self.proc = None
            self.ipc_dirs = []

        def ensure_available(self, **kwargs):
            return {"ok": True}

        def new_ipc_dir(self):
            directory = Path(tempfile.mkdtemp(prefix="thipc-", dir="/tmp"))
            self.ipc_dirs.append(directory)
            return directory

        def start_container(self, spec):
            env = os.environ.copy()
            env.update(spec.env)
            env["PATH"] = os.environ.get("PATH", os.defpath)
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    str(spec.ipc_dir / "kernel.json"),
                ],
                cwd=str(spec.cwd),
                env=env,
            )

        def container_running(self, name):
            return self.proc is not None and self.proc.poll() is None

        def interrupt_container(self, name):
            pass

        def remove_container(self, name):
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

        def logs(self, name, tail=100):
            return ""

    await upload_dataset(client, headers, name="sandbox-dataset")
    entries = accessible_dataset_entries(db, app.state.storage, customer["id"])
    settings = make_settings(tmp_path, sandbox_mode="docker")
    sandbox = LocalSandbox(settings)
    session = None
    try:
        session = await KernelSession.create(
            customer_id=customer["id"],
            notebook_id="nb_sandbox",
            notebook_name="Sandbox",
            kernel_name="python3",
            settings=settings,
            storage=app.state.storage,
            session_id="ks_sandbox",
            catalog_entries=entries,
            sandbox=sandbox,
        )
        result = await session.execute(
            "from pathlib import Path; "
            "from trainhub_runtime import dataset_path, list_datasets; "
            "print('NAME', list_datasets()[0]['name']); "
            "print('FILES', sorted(p.name for p in Path(dataset_path('sandbox-dataset')).iterdir()))",
            30,
        )
        assert result["status"] == "ok", result
        text = "".join(
            str(item.get("text") or "")
            for item in result.get("outputs", [])
            if item.get("output_type") == "stream"
        )
        assert "NAME sandbox-dataset" in text
        assert "FILES ['a.txt']" in text
    finally:
        if session is not None:
            await session.close()
        import shutil

        for directory in sandbox.ipc_dirs:
            shutil.rmtree(directory, ignore_errors=True)


def test_local_ipc_kernel_transport(tmp_path):
    from jupyter_client import KernelManager

    base = Path("/tmp/trainhub-ipc-test")
    base.mkdir(parents=True, exist_ok=True)
    connection = base / "kernel.json"
    km = KernelManager(kernel_name="python3", transport="ipc", connection_file=str(connection))
    try:
        km.start_kernel(cwd=str(base))
        client = km.client()
        client.start_channels()
        client.wait_for_ready(timeout=15)
        msg_id = client.execute("print('ipc-ok')", allow_stdin=False)
        output = ""
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                msg = client.get_iopub_msg(timeout=1)
            except Exception:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            if msg.get("msg_type") == "stream":
                output += msg["content"].get("text", "")
            if msg.get("msg_type") == "status" and msg["content"].get("execution_state") == "idle":
                break
        assert "ipc-ok" in output
    finally:
        try:
            km.shutdown_kernel(now=True)
        except Exception:
            pass
        km.cleanup_resources()
        import shutil

        shutil.rmtree(base, ignore_errors=True)
