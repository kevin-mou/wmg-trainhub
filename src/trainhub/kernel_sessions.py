from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .dataset_mount import sync_dataset_mount
from .db import Database
from .kernels import default_kernel_name, list_kernels
from .responses import ApiError
from .runtime_env import (
    RUNTIME_PACKAGE_ROOT,
    host_base_env,
    kernel_runtime_env,
    sandbox_env,
)
from .sandbox import DockerSandbox, SandboxSpec
from .sandbox_kernel import SandboxKernelManager
from .storage import Storage

logger = logging.getLogger("trainhub.kernel_sessions")

MAX_STREAM_CHARS = 500_000
MAX_OUTPUT_TEXT_CHARS = 1_000_000
MAX_IMAGE_BASE64_CHARS = 8 * 1024 * 1024
KERNEL_START_TIMEOUT_SECONDS = 30


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def dataset_signature(files: list[dict[str, Any]]) -> str:
    """数据集文件清单指纹：解压/增删文件后让运行中的会话重新挂载。"""
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: str(entry.get("filename") or "")):
        digest.update(
            f"{item.get('filename')}:{item.get('size_bytes')}:"
            f"{item.get('storage_path')}".encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def run_public(
    run: dict[str, Any],
    *,
    session_id: str | None = None,
    include_outputs: bool = True,
    event_seq: int | None = None,
) -> dict[str, Any]:
    """运行态的外发形状；轮询/初载可关掉 outputs，终态再取一次完整结果。"""
    cells = []
    for entry in run.get("cells") or []:
        item = {
            "cellId": entry.get("cellId") or "",
            "status": entry.get("status"),
            "executionCount": entry.get("executionCount"),
            "message": entry.get("message"),
        }
        if include_outputs:
            item["outputs"] = entry.get("outputs") or []
        else:
            item["hasOutputs"] = bool(entry.get("outputs"))
        cells.append(item)
    payload = {
        "runId": run.get("runId"),
        "mode": run.get("mode"),
        "status": run.get("status"),
        "startedAt": run.get("startedAt"),
        "finishedAt": run.get("finishedAt"),
        "currentCellId": run.get("currentCellId"),
        "failedCellId": run.get("failedCellId"),
        "message": run.get("message"),
        "cells": cells,
    }
    if session_id is not None:
        payload["sessionId"] = session_id
    if event_seq is not None:
        payload["lastEventSeq"] = event_seq
    return payload


def plan_execution(
    cells: list[dict[str, Any]],
    executed: dict[str, str],
    force_cell_id: str | None = None,
    force_all: bool = False,
) -> list[dict[str, Any]]:
    """规划一次执行的单元格序列。

    - 默认（``force_all=False``）：「运行到此处」——已在本内核中以相同源码执行成功过的
      **前置**单元格跳过（改过的代码会重新执行）；目标单元格（``force_cell_id``）总是
      重新执行——用户点的就是它，跳过会让点击毫无反应。
    - ``force_all=True``：「运行全部」——不跳过已执行单元格，按顺序全部执行（空源码仍跳过）。
    """
    plan: list[dict[str, Any]] = []
    for item in cells:
        cell_id = str(item.get("cellId") or "")
        source = str(item.get("source") or "")
        digest = source_digest(source)
        done = bool(cell_id) and executed.get(cell_id) == digest
        forced = bool(cell_id) and cell_id == force_cell_id
        plan.append(
            {
                "cellId": cell_id,
                "source": source,
                "digest": digest,
                "skip": not source.strip() or ((done and not forced) and not force_all),
            }
        )
    return plan


def _value_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    if value is None:
        return ""
    return str(value)


def _clip(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（输出已截断）"


def _normalize_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, Any] = {}
    for mime, value in data.items():
        if not isinstance(mime, str):
            continue
        if isinstance(value, list):
            normalized[mime] = "".join(str(part) for part in value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            normalized[mime] = value
        else:
            normalized[mime] = value
        if mime.startswith("image/") and isinstance(normalized[mime], str):
            # 截断 base64 会得到损坏图片；超限直接省略，让前端显示纯文本/不支持提示。
            normalized[mime] = (
                normalized[mime] if len(normalized[mime]) <= MAX_IMAGE_BASE64_CHARS else ""
            )
    return normalized


class KernelSession:
    def __init__(
        self,
        *,
        session_id: str,
        customer_id: str,
        notebook_id: str,
        kernel_name: str,
        base_dir: Path,
        km: Any,
        client: Any,
    ) -> None:
        self.session_id = session_id
        self.customer_id = customer_id
        self.notebook_id = notebook_id
        self.kernel_name = kernel_name
        self.base_dir = base_dir
        self.km = km
        self.client = client
        self.lock = asyncio.Lock()
        self.started_at = _utc_now()
        self.last_used = time.monotonic()
        self._closed = False
        # cellId → 最近一次成功执行时的源码哈希（「运行到此处」据此跳过未改动的前置单元格）
        self.executed: dict[str, str] = {}
        # 最近一次后台运行（运行中或已结束）与后台任务：刷新页面后可查询/取回结果
        self.latest_run: dict[str, Any] | None = None
        self.run_task: asyncio.Task | None = None
        # 运行事件订阅者（SSE）：每个连接一个队列，事件由后台运行实时发布
        self.run_subscribers: set[asyncio.Queue] = set()
        # 事件单调序号：SSE 快照与增量去重用（快照带 lastEventSeq，更小的事件丢弃）
        self.run_event_seq = 0
        # 会话级数据集挂载（WMG_DATASET_DIR）：切换数据集时整体替换
        self.dataset_dir = self.base_dir / "work" / "dataset"
        self.dataset_id: str | None = None
        self.dataset_name: str | None = None
        self.dataset_signature: str | None = None
        self.dataset_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        *,
        customer_id: str,
        notebook_id: str,
        notebook_name: str,
        kernel_name: str,
        settings: Settings,
        storage: Storage,
        session_id: str,
        dataset: dict[str, Any] | None = None,
        dataset_files: list[dict[str, Any]] | None = None,
        sandbox: DockerSandbox | None = None,
    ) -> KernelSession:
        from jupyter_client import KernelManager
        from jupyter_client.asynchronous import AsyncKernelClient

        base_dir = storage.notebook_session_dir(customer_id, notebook_id, session_id)
        work_dir = base_dir / "work"
        home_dir = base_dir / "home"
        tmp_dir = base_dir / "tmp"
        dataset_dir = work_dir / "dataset"
        output_dir = work_dir / "output"
        for directory in (work_dir, dataset_dir, output_dir, home_dir, tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

        dataset_files = dataset_files or []
        sync_dataset_mount(dataset_files, dataset_dir)

        env = host_base_env(settings)
        env.update(
            {
                "WMG_CUSTOMER_ID": customer_id,
                "WMG_NOTEBOOK_NAME": notebook_name,
                "WMG_SESSION_ID": session_id,
                "WMG_WORK_DIR": str(work_dir),
                "WMG_DATASET_DIR": str(dataset_dir),
                "DATASET_DIR": str(dataset_dir),
                "WMG_DATASET_NAME": dataset["name"] if dataset else "",
                "WMG_OUTPUT_DIR": str(output_dir),
                "OUTPUT_DIR": str(output_dir),
                "WMG_PARAMS": "{}",
                "HOME": str(home_dir),
                "XDG_CACHE_HOME": str(home_dir / ".cache"),
                "MPLCONFIGDIR": str(home_dir / ".matplotlib"),
                "IPYTHONDIR": str(home_dir / ".ipython"),
                "JUPYTER_RUNTIME_DIR": str(home_dir / "runtime"),
                "TMPDIR": str(tmp_dir),
                "TEMP": str(tmp_dir),
                "TMP": str(tmp_dir),
                "PYTHONUNBUFFERED": "1",
            }
        )
        # 共享模型缓存 + 运行时助手（trainhub_runtime）：所有内核复用同一份模型缓存。
        env.update(kernel_runtime_env(settings, env))

        km = None
        client = None
        try:
            if getattr(settings, "sandbox_mode", "none") == "docker":
                sandbox = sandbox or DockerSandbox(settings)
                sandbox.ensure_available()
                ipc_dir = sandbox.new_ipc_dir()
                sandbox_environment = sandbox_env(
                    settings,
                    env,
                    home_dir=home_dir,
                    model_dir=settings.model_dir_path,
                    network=settings.sandbox_network,
                )
                dataset_dirs: list[Path] = []
                if dataset is not None:
                    dataset_dirs.append(
                        storage.dataset_files_dir(dataset["customer_id"], dataset["id"])
                    )
                spec = SandboxSpec(
                    name=sandbox.container_name("session", session_id),
                    kind="session",
                    key=session_id,
                    customer_id=customer_id,
                    work_dir=work_dir,
                    cwd=work_dir,
                    output_dir=output_dir,
                    home_dir=home_dir,
                    ipc_dir=ipc_dir,
                    env=sandbox_environment,
                    dataset_dirs=dataset_dirs,
                    model_dir=settings.model_dir_path,
                    runtime_dir=RUNTIME_PACKAGE_ROOT,
                    python=getattr(settings, "sandbox_python", "python3"),
                )
                km = SandboxKernelManager(sandbox=sandbox, spec=spec)
            else:
                km = KernelManager(kernel_name=kernel_name)
            await asyncio.to_thread(km.start_kernel, cwd=str(work_dir), env=env)
            client = AsyncKernelClient()
            client.load_connection_file(km.connection_file)
            client.start_channels()
            await client.wait_for_ready(timeout=KERNEL_START_TIMEOUT_SECONDS)
        except Exception as exc:
            if client is not None:
                try:
                    client.stop_channels()
                except Exception:
                    pass
            if km is not None:
                try:
                    await asyncio.to_thread(km.shutdown_kernel, now=True)
                except Exception:
                    pass
                try:
                    await asyncio.to_thread(km.cleanup_resources)
                except Exception:
                    pass
            shutil.rmtree(base_dir, ignore_errors=True)
            if isinstance(exc, ApiError):
                # 沙箱预检/启动失败本身是 503/409 等业务错误，不要改写成 500。
                raise
            detail = str(exc)
            if isinstance(km, SandboxKernelManager):
                logs = km.logs()
                if logs:
                    detail = f"{detail}\n容器日志：\n{logs}"
            raise ApiError(f"启动 Jupyter 内核失败：{detail}", 500) from exc

        logger.info(
            "已启动 Notebook 内核会话 %s（客户 %s，Notebook %s，内核 %s，沙箱 %s）",
            session_id,
            customer_id,
            notebook_id,
            kernel_name,
            getattr(settings, "sandbox_mode", "none"),
        )
        session = cls(
            session_id=session_id,
            customer_id=customer_id,
            notebook_id=notebook_id,
            kernel_name=kernel_name,
            base_dir=base_dir,
            km=km,
            client=client,
        )
        session.dataset_id = dataset["id"] if dataset else None
        session.dataset_name = dataset["name"] if dataset else None
        session.dataset_signature = dataset_signature(dataset_files) if dataset else None
        return session

    def info(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "kernel": self.kernel_name,
            "status": "ready" if self.is_alive() else "dead",
            "startedAt": self.started_at,
            "lastUsedAt": _utc_now(),
        }

    def is_alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.km.is_alive())
        except Exception:
            return False

    async def execute(
        self, code: str, timeout_seconds: int, cell_id: str | None = None
    ) -> dict[str, Any]:
        if not self.is_alive():
            raise ApiError("内核会话已停止，请重新运行单元格", 409)
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=1.0)
        except asyncio.TimeoutError as exc:
            raise ApiError("该 Notebook 的单元格正在运行，请稍后再试", 409) from exc
        try:
            self.last_used = time.monotonic()
            result = await self._execute_locked(code, timeout_seconds)
            self._record_execution(cell_id, code, result)
            return result
        finally:
            self.last_used = time.monotonic()
            self.lock.release()

    def _record_execution(
        self, cell_id: str | None, source: str, result: dict[str, Any]
    ) -> None:
        if not cell_id:
            return
        if result.get("status") == "ok":
            self.executed[cell_id] = source_digest(source)
        else:
            # 失败/超时的单元格不算已执行：下次「运行到此处」要重新跑它
            self.executed.pop(cell_id, None)

    async def execute_sequence(
        self,
        cells: list[dict[str, Any]],
        timeout_seconds: int,
        force_cell_id: str | None = None,
        force_all: bool = False,
    ) -> list[dict[str, Any]]:
        """按顺序执行一串代码单元格（「运行到此处」/「运行全部」）。

        默认跳过已执行且源码未变的单元格；``force_all=True`` 时不跳过。
        遇到失败/超时立即停止，把失败结果返回给调用方。
        整个序列持有同一把执行锁，期间不会被单格执行插入。
        """
        if not self.is_alive():
            raise ApiError("内核会话已停止，请重新运行单元格", 409)
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=1.0)
        except asyncio.TimeoutError as exc:
            raise ApiError("该 Notebook 的单元格正在运行，请稍后再试", 409) from exc
        results: list[dict[str, Any]] = []
        try:
            for item in plan_execution(
                cells, self.executed, force_cell_id, force_all=force_all
            ):
                if item["skip"]:
                    results.append(
                        {
                            "cellId": item["cellId"],
                            "status": "skipped",
                            "executionCount": None,
                            "outputs": [],
                            "message": None,
                        }
                    )
                    continue
                self.last_used = time.monotonic()
                result = await self._execute_locked(item["source"], timeout_seconds)
                status = str(result.get("status") or "")
                if item["cellId"]:
                    if status == "ok":
                        self.executed[item["cellId"]] = item["digest"]
                    else:
                        self.executed.pop(item["cellId"], None)
                results.append({"cellId": item["cellId"], **result})
                if status != "ok":
                    break
        finally:
            self.last_used = time.monotonic()
            self.lock.release()
        return results

    async def _execute_locked(
        self, code: str, timeout_seconds: int, on_update: Any = None
    ) -> dict[str, Any]:
        """执行一个单元格。``on_update(outputs, execution_count)`` 在每条输出后回调（后台运行用它做实时回显）。"""

        def emit() -> None:
            if on_update is None:
                return
            try:
                on_update(outputs, execution_count)
            except Exception:  # noqa: BLE001 - 回显失败不能影响执行
                logger.debug("运行输出回显回调失败", exc_info=True)

        msg_id = self.client.execute(code, allow_stdin=False, stop_on_error=False)
        outputs: list[dict[str, Any]] = []
        execution_count: int | None = None
        timed_out = False
        deadline = time.monotonic() + max(1, int(timeout_seconds))

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                msg = await asyncio.wait_for(
                    self.client.get_iopub_msg(timeout=remaining), timeout=remaining
                )
            except asyncio.TimeoutError:
                timed_out = True
                break
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            msg_type = msg.get("msg_type")
            content = msg.get("content") or {}
            if msg_type == "status" and content.get("execution_state") == "idle":
                break
            if msg_type == "execute_input":
                count = content.get("execution_count")
                if isinstance(count, int):
                    execution_count = count
                    emit()
            elif msg_type == "stream":
                name = str(content.get("name") or "stdout")
                text = _clip(_value_text(content.get("text")), MAX_STREAM_CHARS)
                if (
                    outputs
                    and outputs[-1].get("output_type") == "stream"
                    and outputs[-1].get("name") == name
                ):
                    outputs[-1]["text"] = _clip(
                        f"{outputs[-1].get('text', '')}{text}", MAX_OUTPUT_TEXT_CHARS
                    )
                    emit()
                else:
                    outputs.append({"output_type": "stream", "name": name, "text": text})
                emit()
            elif msg_type in ("execute_result", "display_data", "update_display_data"):
                data = _normalize_data(content.get("data"))
                item: dict[str, Any] = {
                    "output_type": (
                        "display_data" if msg_type != "execute_result" else "execute_result"
                    ),
                    "data": data,
                    "metadata": content.get("metadata") or {},
                }
                if msg_type == "execute_result":
                    item["execution_count"] = content.get("execution_count")
                    if isinstance(item["execution_count"], int):
                        execution_count = item["execution_count"]
                outputs.append(item)
                emit()
            elif msg_type == "error":
                outputs.append(
                    {
                        "output_type": "error",
                        "ename": _value_text(content.get("ename")),
                        "evalue": _value_text(content.get("evalue")),
                        "traceback": [
                            _clip(_value_text(line), MAX_OUTPUT_TEXT_CHARS)
                            for line in (content.get("traceback") or [])
                        ],
                    }
                )
                emit()
            elif msg_type == "clear_output":
                outputs.clear()
                emit()

        if timed_out:
            try:
                await asyncio.to_thread(self.km.interrupt_kernel)
            except Exception:
                logger.warning("中断超时的 Notebook 内核失败", exc_info=True)
            # 尽量把中断产生的 idle 消息读掉，避免下一次执行读到旧状态。
            drain_deadline = time.monotonic() + 2.0
            while time.monotonic() < drain_deadline:
                try:
                    msg = await asyncio.wait_for(
                        self.client.get_iopub_msg(timeout=0.3), timeout=0.3
                    )
                except asyncio.TimeoutError:
                    break
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                if (
                    msg.get("msg_type") == "status"
                    and msg.get("content", {}).get("execution_state") == "idle"
                ):
                    break
            outputs.append(
                {
                    "output_type": "error",
                    "ename": "TimeoutError",
                    "evalue": f"单元格执行超过 {timeout_seconds} 秒",
                    "traceback": [],
                }
            )
            emit()
            return {
                "status": "timeout",
                "executionCount": execution_count,
                "outputs": outputs,
                "message": f"单元格执行超过 {timeout_seconds} 秒",
            }

        reply: dict[str, Any] | None = None
        try:
            candidate = await asyncio.wait_for(self.client.get_shell_msg(timeout=5), timeout=5)
            if candidate.get("parent_header", {}).get("msg_id") == msg_id:
                reply = candidate.get("content") or {}
        except asyncio.TimeoutError:
            reply = None

        if reply:
            count = reply.get("execution_count")
            if isinstance(count, int):
                execution_count = count
            if reply.get("status") == "error" and not any(
                item.get("output_type") == "error" for item in outputs
            ):
                outputs.append(
                    {
                        "output_type": "error",
                        "ename": _value_text(reply.get("ename")),
                        "evalue": _value_text(reply.get("evalue")),
                        "traceback": [
                            _clip(_value_text(line), MAX_OUTPUT_TEXT_CHARS)
                            for line in (reply.get("traceback") or [])
                        ],
                    }
                )

        error_message = None
        for item in outputs:
            if item.get("output_type") == "error":
                error_message = ": ".join(
                    part for part in (item.get("ename"), item.get("evalue")) if part
                )
                break
        if not self.is_alive() and error_message is None:
            error_message = "内核已停止"
            outputs.append(
                {
                    "output_type": "error",
                    "ename": "KernelDied",
                    "evalue": "内核已停止",
                    "traceback": [],
                }
            )

        return {
            "status": "error" if error_message else "ok",
            "executionCount": execution_count,
            "outputs": outputs,
            "message": error_message,
        }

    def subscribe_run(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.run_subscribers.add(queue)
        return queue

    def unsubscribe_run(self, queue: asyncio.Queue) -> None:
        self.run_subscribers.discard(queue)

    def publish_run(self, event: dict[str, Any]) -> None:
        self.run_event_seq += 1
        event = {**event, "seq": self.run_event_seq}
        for queue in list(self.run_subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 客户端跟不上：清空后让它用 state 快照重同步
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    queue.put_nowait({"e": "resync"})
                except asyncio.QueueFull:
                    pass

    def start_run(
        self,
        *,
        mode: str,
        cells: list[dict[str, Any]],
        force_cell_id: str | None,
        timeout_seconds: int,
        force_all: bool = False,
    ) -> dict[str, Any]:
        """把一次执行登记为后台任务并立即返回运行态。"""
        run: dict[str, Any] = {
            "runId": "run_" + secrets.token_hex(8),
            "mode": mode,
            "status": "running",
            "startedAt": _utc_now(),
            "finishedAt": None,
            "currentCellId": None,
            "failedCellId": None,
            "message": None,
            "cells": [],
            "timeout": timeout_seconds,
        }
        self.latest_run = run
        self.run_task = asyncio.create_task(
            self._run_cells(run, cells, force_cell_id, force_all),
            name=f"trainhub-run-{run['runId']}",
        )
        return run_public(run)

    async def _run_cells(
        self,
        run: dict[str, Any],
        cells: list[dict[str, Any]],
        force_cell_id: str | None,
        force_all: bool = False,
    ) -> None:
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=1.0)
        except asyncio.TimeoutError:
            run.update(
                status="failed",
                finishedAt=_utc_now(),
                message="该 Notebook 的单元格正在运行，请稍后再试",
            )
            self.run_task = None
            return
        except asyncio.CancelledError:
            run.update(
                status="cancelled",
                finishedAt=_utc_now(),
                message="运行已取消（内核会话被关闭）",
            )
            self.run_task = None
            raise
        try:
            plan = plan_execution(
                cells, self.executed, force_cell_id, force_all=force_all
            )
            run["cells"] = [
                {
                    "cellId": item["cellId"],
                    "status": "pending",
                    "executionCount": None,
                    "outputs": [],
                    "message": None,
                    "startedAt": None,
                    "finishedAt": None,
                }
                for item in plan
            ]
            snapshots: dict[str, dict[str, Any]] = {}

            def publish_outputs(entry: dict[str, Any], outputs: list, count: Any) -> None:
                """把输出变化折算成最小增量事件（stream 只发新增文本）。"""
                cell_id = entry.get("cellId") or ""
                prev = snapshots.setdefault(
                    cell_id, {"count": 0, "last_type": None, "last_stream": "", "executionCount": None}
                )
                events: list[dict[str, Any]] = []
                if count is not None and count != prev["executionCount"]:
                    prev["executionCount"] = count
                    events.append(
                        {
                            "e": "count",
                            "runId": run["runId"],
                            "cellId": cell_id,
                            "executionCount": count,
                        }
                    )
                delta: dict[str, Any] | None = None
                if len(outputs) < prev["count"]:
                    delta = {"e": "outputs", "outputs": outputs}
                elif len(outputs) == prev["count"]:
                    if outputs:
                        last = outputs[-1]
                        if (
                            last.get("output_type") == "stream"
                            and prev["last_type"] == "stream"
                        ):
                            text = str(last.get("text") or "")
                            old_text = prev["last_stream"]
                            if text.startswith(old_text):
                                if len(text) > len(old_text):
                                    delta = {
                                        "e": "stream",
                                        "name": last.get("name") or "stdout",
                                        "text": text[len(old_text):],
                                    }
                            else:
                                delta = {"e": "outputs", "outputs": outputs}
                        else:
                            delta = {"e": "outputs", "outputs": outputs}
                elif len(outputs) == prev["count"] + 1:
                    delta = {"e": "output", "output": outputs[-1]}
                else:
                    delta = {"e": "outputs", "outputs": outputs}
                if delta is not None:
                    events.append({"runId": run["runId"], "cellId": cell_id, **delta})
                prev["count"] = len(outputs)
                if outputs:
                    last = outputs[-1]
                    prev["last_type"] = last.get("output_type")
                    prev["last_stream"] = (
                        str(last.get("text") or "")
                        if last.get("output_type") == "stream"
                        else ""
                    )
                else:
                    prev["last_type"] = None
                    prev["last_stream"] = ""
                for event in events:
                    self.publish_run(event)
            failed: dict[str, Any] | None = None
            for entry, item in zip(run["cells"], plan):
                if item["skip"]:
                    entry["status"] = "skipped"
                    continue
                entry["status"] = "running"
                entry["startedAt"] = _utc_now()
                run["currentCellId"] = item["cellId"] or None
                self.last_used = time.monotonic()
                self.publish_run(
                    {
                        "e": "cell",
                        "runId": run["runId"],
                        "cellId": item["cellId"] or "",
                        "status": "running",
                    }
                )

                def on_update(outputs: list, count: Any, entry=entry) -> None:
                    entry["outputs"] = outputs
                    if count is not None:
                        entry["executionCount"] = count
                    publish_outputs(entry, outputs, count)

                result = await self._execute_locked(
                    item["source"], int(run.get("timeout") or 300), on_update
                )
                status = str(result.get("status") or "")
                entry["status"] = (
                    "ok" if status == "ok" else ("timeout" if status == "timeout" else "error")
                )
                entry["executionCount"] = result.get("executionCount")
                entry["outputs"] = result.get("outputs") or []
                entry["message"] = result.get("message")
                entry["finishedAt"] = _utc_now()
                self.publish_run(
                    {
                        "e": "cell",
                        "runId": run["runId"],
                        "cellId": item["cellId"] or "",
                        "status": entry["status"],
                        "executionCount": entry.get("executionCount"),
                        "message": entry.get("message"),
                    }
                )
                if item["cellId"]:
                    if status == "ok":
                        self.executed[item["cellId"]] = item["digest"]
                    else:
                        self.executed.pop(item["cellId"], None)
                if status != "ok":
                    failed = entry
                    break
            if failed is None:
                run["status"] = "succeeded"
            else:
                run["status"] = "failed"
                run["failedCellId"] = failed.get("cellId") or None
                run["message"] = failed.get("message") or str(failed.get("status"))
        except asyncio.CancelledError:
            run["status"] = "cancelled"
            run["message"] = "运行已取消（内核会话被关闭）"
            raise
        except Exception as exc:  # noqa: BLE001 - 把意外异常暴露给轮询端
            run["status"] = "failed"
            run["message"] = str(exc) or exc.__class__.__name__
        finally:
            run["finishedAt"] = run.get("finishedAt") or _utc_now()
            run["currentCellId"] = None
            self.last_used = time.monotonic()
            self.run_task = None
            self.publish_run(
                {
                    "e": "run",
                    "runId": run["runId"],
                    "status": run.get("status"),
                    "failedCellId": run.get("failedCellId"),
                    "message": run.get("message"),
                }
            )
            if self.lock.locked():
                self.lock.release()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self.run_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.to_thread(self.client.stop_channels)
        except Exception:
            logger.debug("停止内核通道失败", exc_info=True)
        try:
            await asyncio.to_thread(self.km.shutdown_kernel, now=True)
        except Exception:
            logger.debug("关闭内核失败", exc_info=True)
        try:
            await asyncio.to_thread(self.km.cleanup_resources)
        except Exception:
            logger.debug("清理内核资源失败", exc_info=True)
        shutil.rmtree(self.base_dir, ignore_errors=True)
        logger.info("已关闭 Notebook 内核会话 %s", self.session_id)


class KernelSessionManager:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        storage: Storage,
        *,
        sandbox: DockerSandbox | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._storage = storage
        self._sandbox = sandbox or DockerSandbox(settings)
        self._sessions: dict[tuple[str, str], KernelSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._cleanup_task is not None:
            return
        self._stopping = False
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="trainhub-kernel-session-cleanup"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()

    def _notebook_dataset(
        self, customer_id: str, notebook: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        dataset_id = (notebook.get("dataset_id") or "").strip() or None
        if not dataset_id or self._db is None:
            return None, []
        dataset = self._db.get_accessible_dataset(customer_id, dataset_id)
        if dataset is None:
            logger.warning(
                "Notebook 数据集不可用，已清除选择：notebook=%s dataset=%s",
                notebook["id"],
                dataset_id,
            )
            self._db.set_notebook_dataset(customer_id, notebook["id"], None)
            return None, []
        return dataset, self._db.get_dataset_files(dataset["id"])

    async def ensure_session(
        self,
        *,
        customer: dict[str, Any],
        notebook: dict[str, Any],
        kernel_name: str | None = None,
    ) -> dict[str, Any]:
        key = (customer["id"], notebook["id"])
        dataset, dataset_files = self._notebook_dataset(customer["id"], notebook)
        desired_dataset_id = dataset["id"] if dataset else None
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.is_alive():
                if getattr(self._settings, "sandbox_mode", "none") != "docker" or (
                    existing.dataset_id == desired_dataset_id
                ):
                    return existing.info()
                busy = bool(
                    (existing.run_task is not None and not existing.run_task.done())
                    or existing.lock.locked()
                )
                if busy:
                    raise ApiError("该 Notebook 的单元格正在运行，请稍后再试", 409)
                self._sessions.pop(key, None)
                await existing.close()
            elif existing is not None:
                self._sessions.pop(key, None)
                await existing.close()

            if len(self._sessions) >= max(1, self._settings.max_kernel_sessions):
                raise ApiError(
                    f"同时运行的 Notebook 内核会话已达上限（{self._settings.max_kernel_sessions}），"
                    "请关闭其他 Notebook 或稍后再试",
                    429,
                )

            kernel = self._resolve_kernel(kernel_name)
            session_id = "ks_" + secrets.token_hex(8)
            session = await KernelSession.create(
                customer_id=customer["id"],
                notebook_id=notebook["id"],
                notebook_name=notebook["name"],
                kernel_name=kernel,
                settings=self._settings,
                storage=self._storage,
                session_id=session_id,
                dataset=dataset,
                dataset_files=dataset_files,
                sandbox=self._sandbox,
            )
            self._sessions[key] = session
            return session.info()

    def _notebook(self, customer_id: str, notebook_id: str) -> dict[str, Any]:
        # 单测会用 db=None 构造 manager（只测内核行为）；此时不查 Notebook。
        if self._db is None:
            return {"id": notebook_id}
        notebook = self._db.get_notebook(customer_id, notebook_id)
        if notebook is None:
            raise ApiError("Notebook 不存在", 404)
        return notebook

    def dataset_busy(self, *, customer_id: str, notebook_id: str) -> bool:
        """会话是否正在执行（执行中不允许切换数据集）。"""
        session = self._sessions.get((customer_id, notebook_id))
        if session is None:
            return False
        return bool(
            (session.run_task is not None and not session.run_task.done()) or session.lock.locked()
        )

    async def refresh_dataset(self, *, customer_id: str, notebook_id: str) -> None:
        """Notebook 的数据集选择变化后，把已有会话的挂载同步过去。

        没有会话时什么都不做（下次运行/启动会话时挂载）；执行中的会话保持原挂载，
        新选择在下次运行时生效。
        """
        session = self._sessions.get((customer_id, notebook_id))
        if session is None:
            return
        if getattr(self._settings, "sandbox_mode", "none") == "docker":
            # 容器挂载在创建时固定；新数据集会在下次 ensure_session 时重建内核。
            return
        if self._db is None:
            return
        notebook = self._db.get_notebook(customer_id, notebook_id)
        if notebook is None:
            return
        await self._apply_dataset(session, customer_id, notebook, raise_if_busy=False)

    async def _apply_dataset(
        self,
        session: KernelSession,
        customer_id: str,
        notebook: dict[str, Any],
        *,
        raise_if_busy: bool = True,
    ) -> None:
        dataset_id = (notebook.get("dataset_id") or "").strip() or None
        if (
            getattr(self._settings, "sandbox_mode", "none") == "docker"
            and session.dataset_id != dataset_id
        ):
            raise ApiError("Docker 隔离模式下切换数据集需要重新启动内核，请重新运行", 409)
        dataset = None
        files: list[dict[str, Any]] = []
        if dataset_id:
            dataset = self._db.get_accessible_dataset(customer_id, dataset_id)
            if dataset is None:
                logger.warning(
                    "Notebook 数据集不可用，已清除选择：notebook=%s dataset=%s",
                    notebook["id"],
                    dataset_id,
                )
                self._db.set_notebook_dataset(customer_id, notebook["id"], None)
                dataset_id = None
            else:
                files = self._db.get_dataset_files(dataset_id)
        signature = dataset_signature(files) if dataset_id else None
        if (
            session.dataset_id == dataset_id
            and session.dataset_signature == signature
            and session.dataset_dir.is_dir()
        ):
            return

        def busy() -> bool:
            return bool(
                (session.run_task is not None and not session.run_task.done())
                or session.lock.locked()
            )

        if busy():
            if raise_if_busy:
                raise ApiError("该 Notebook 正在运行，请结束后再切换数据集", 409)
            return
        async with session.dataset_lock:
            if (
                session.dataset_id == dataset_id
                and session.dataset_signature == signature
                and session.dataset_dir.is_dir()
            ):
                return
            if busy():
                if raise_if_busy:
                    raise ApiError("该 Notebook 正在运行，请结束后再切换数据集", 409)
                return
            # 等锁期间数据集可能被解压/删除，重新取一次清单。
            dataset = None
            files = []
            if dataset_id:
                dataset = self._db.get_accessible_dataset(customer_id, dataset_id)
                if dataset is None:
                    dataset_id = None
                else:
                    files = self._db.get_dataset_files(dataset_id)
            signature = dataset_signature(files) if dataset_id else None
            linked, missing = await asyncio.to_thread(
                sync_dataset_mount, files, session.dataset_dir
            )
            session.dataset_id = dataset_id
            session.dataset_name = dataset["name"] if dataset else None
            session.dataset_signature = signature
            if dataset is not None:
                logger.info(
                    "Notebook 数据集已挂载：notebook=%s dataset=%s files=%s missing=%s",
                    notebook["id"],
                    dataset_id,
                    linked,
                    len(missing),
                )
                for name in missing:
                    logger.warning("数据集文件缺失或无法挂载：%s", name)

    async def execute(
        self,
        *,
        customer_id: str,
        notebook_id: str,
        session_id: str,
        code: str,
        timeout_seconds: int | None = None,
        cell_id: str | None = None,
    ) -> dict[str, Any]:
        session = self._sessions.get((customer_id, notebook_id))
        if session is None or session.session_id != session_id:
            raise ApiError("Notebook 内核会话不存在或已过期，请重新运行单元格", 404)
        if not session.is_alive():
            await self.close(
                customer_id=customer_id, notebook_id=notebook_id, session_id=session_id
            )
            raise ApiError("Notebook 内核会话已停止，请重新运行单元格", 404)
        notebook = self._notebook(customer_id, notebook_id)
        await self._apply_dataset(session, customer_id, notebook)
        timeout = int(timeout_seconds or self._settings.kernel_cell_timeout_seconds or 300)
        timeout = max(1, min(timeout, 3600))
        return await session.execute(code, timeout, cell_id=cell_id)

    async def execute_sequence(
        self,
        *,
        customer_id: str,
        notebook_id: str,
        session_id: str,
        cells: list[dict[str, Any]],
        timeout_seconds: int | None = None,
        target_cell_id: str | None = None,
    ) -> dict[str, Any]:
        """「运行到此处」：按顺序补跑未执行（或源码已变）的前置单元格，再运行目标单元格。"""
        session = self._sessions.get((customer_id, notebook_id))
        if session is None or session.session_id != session_id:
            raise ApiError("Notebook 内核会话不存在或已过期，请重新运行单元格", 404)
        if not session.is_alive():
            await self.close(
                customer_id=customer_id, notebook_id=notebook_id, session_id=session_id
            )
            raise ApiError("Notebook 内核会话已停止，请重新运行单元格", 404)
        notebook = self._notebook(customer_id, notebook_id)
        await self._apply_dataset(session, customer_id, notebook)
        timeout = int(timeout_seconds or self._settings.kernel_cell_timeout_seconds or 300)
        timeout = max(1, min(timeout, 3600))
        results = await session.execute_sequence(cells, timeout, force_cell_id=target_cell_id)
        failed = next(
            (item for item in results if item.get("status") not in ("ok", "skipped")), None
        )
        return {
            "sessionId": session.session_id,
            "cells": results,
            "failedCellId": failed.get("cellId") if failed else None,
            "message": (failed.get("message") or failed.get("status")) if failed else None,
        }

    async def start_run(
        self,
        *,
        customer_id: str,
        notebook_id: str,
        session_id: str,
        mode: str,
        cells: list[dict[str, Any]],
        force_cell_id: str | None = None,
        timeout_seconds: int | None = None,
        force_all: bool = False,
    ) -> dict[str, Any]:
        """把一次执行登记为后台任务并立即返回（刷新页面不影响执行）。"""
        session = self._sessions.get((customer_id, notebook_id))
        if session is None or session.session_id != session_id:
            raise ApiError("Notebook 内核会话不存在或已过期，请重新运行单元格", 404)
        if not session.is_alive():
            await self.close(
                customer_id=customer_id, notebook_id=notebook_id, session_id=session_id
            )
            raise ApiError("Notebook 内核会话已停止，请重新运行单元格", 404)
        if session.run_task is not None and not session.run_task.done():
            raise ApiError("该 Notebook 的单元格正在运行，请稍后再试", 409)
        if session.lock.locked():
            raise ApiError("该 Notebook 的单元格正在运行，请稍后再试", 409)
        notebook = self._notebook(customer_id, notebook_id)
        await self._apply_dataset(session, customer_id, notebook)
        timeout = int(timeout_seconds or self._settings.kernel_cell_timeout_seconds or 300)
        timeout = max(1, min(timeout, 3600))
        run = session.start_run(
            mode=mode,
            cells=cells,
            force_cell_id=force_cell_id,
            timeout_seconds=timeout,
            force_all=force_all,
        )
        run["sessionId"] = session.session_id
        return run

    def current_run(
        self, *, customer_id: str, notebook_id: str, include_outputs: bool = True
    ) -> dict[str, Any] | None:
        """最近一次运行（运行中或已结束）；没有会话或没有运行记录时返回 None。"""
        session = self._sessions.get((customer_id, notebook_id))
        if session is None or session.latest_run is None:
            return None
        return run_public(
            session.latest_run,
            session_id=session.session_id,
            include_outputs=include_outputs,
            event_seq=session.run_event_seq,
        )

    def get_session(
        self, *, customer_id: str, notebook_id: str
    ) -> KernelSession | None:
        return self._sessions.get((customer_id, notebook_id))

    async def restart_session(
        self,
        *,
        customer: dict[str, Any],
        notebook: dict[str, Any],
        kernel_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """关闭当前内核（不清点 session_id 是否匹配）并新建一个干净内核。"""
        key = (customer["id"], notebook["id"])
        session = self._sessions.get(key)
        if session is not None:
            if session.lock.locked():
                raise ApiError("该 Notebook 的单元格正在运行，请稍后再试", 409)
            await self.close(customer_id=customer["id"], notebook_id=notebook["id"], force=True)
        return await self.ensure_session(
            customer=customer, notebook=notebook, kernel_name=kernel_name
        )

    async def close(
        self,
        *,
        customer_id: str,
        notebook_id: str,
        session_id: str | None = None,
        force: bool = False,
    ) -> None:
        key = (customer_id, notebook_id)
        async with self._lock:
            session = self._sessions.get(key)
            if session is None or (
                not force and session_id and session.session_id != session_id
            ):
                return
            self._sessions.pop(key, None)
        await session.close()

    def _resolve_kernel(self, requested: str | None) -> str:
        value = (requested or "").strip()
        if getattr(self._settings, "sandbox_mode", "none") == "docker":
            # 沙箱镜像自带 Python 内核；宿主 kernelspec 只用于本机模式。
            return value or "python3"
        names = {item["name"] for item in list_kernels()}
        if not names:
            raise ApiError("服务器没有可用的 Jupyter 内核，请联系节点管理员", 400)
        if value and value not in names:
            raise ApiError(f"内核不可用：{value}；可用内核：{', '.join(sorted(names))}", 400)
        return value or default_kernel_name() or sorted(names)[0]

    async def _cleanup_loop(self) -> None:
        idle_seconds = max(60, self._settings.kernel_session_idle_seconds)
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale: list[KernelSession] = []
            async with self._lock:
                for key, session in list(self._sessions.items()):
                    if session.lock.locked():
                        continue
                    if now - session.last_used > idle_seconds or not session.is_alive():
                        self._sessions.pop(key, None)
                        stale.append(session)
            for session in stale:
                await session.close()
