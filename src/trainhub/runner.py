from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import Settings
from .db import Database
from .kernels import list_kernels
from .storage import Storage, iter_files
from .utils import truncate

logger = logging.getLogger("trainhub.runner")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_CELL_OUTPUT_LIMIT = 20_000
_MESSAGE_LIMIT = 1000


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@lru_cache(maxsize=1)
def _jupyter_bits() -> SimpleNamespace:
    try:
        import nbclient  # noqa: F401
        import nbformat
        from jupyter_core.utils import ensure_async
    except ImportError as exc:  # pragma: no cover - 部署缺依赖时给出明确提示
        raise RuntimeError(
            "服务器缺少 Jupyter 执行环境（nbclient / nbformat / ipykernel），"
            "请按 requirements.txt 安装后再提交训练"
        ) from exc
    return SimpleNamespace(nbformat=nbformat, ensure_async=ensure_async)


@lru_cache(maxsize=1)
def _streaming_client_class() -> type:
    from nbclient import NotebookClient

    class StreamingNotebookClient(NotebookClient):
        """把内核输出实时写入训练日志，同时保留 notebook 自身的输出结构。"""

        def __init__(self, *args: Any, log_writer: RunLogWriter | None = None, **kwargs: Any):
            self._trainhub_log = log_writer
            self.last_error: str | None = None
            super().__init__(*args, **kwargs)

        def output(self, outs: list, msg: dict, display_id: str | None, cell_index: int):
            writer = self._trainhub_log
            if writer is not None:
                try:
                    self._stream_message(writer, msg)
                except Exception:  # 日志写入失败不能影响训练本身
                    logger.debug("写入训练日志失败", exc_info=True)
            return super().output(outs, msg, display_id, cell_index)

        def _stream_message(self, writer: RunLogWriter, msg: dict) -> None:
            msg_type = msg.get("msg_type")
            content = msg.get("content") or {}
            if msg_type == "stream":
                writer.chunk(str(content.get("text") or ""))
            elif msg_type in ("execute_result", "display_data", "update_display_data"):
                data = content.get("data") or {}
                text = data.get("text/plain")
                if isinstance(text, list):
                    text = "".join(str(part) for part in text)
                if text:
                    text = str(text)
                    if len(text) > _CELL_OUTPUT_LIMIT:
                        text = text[:_CELL_OUTPUT_LIMIT] + "\n…（输出过长，已截断）"
                    writer.chunk(text if text.endswith("\n") else text + "\n")
            elif msg_type == "error":
                ename = str(content.get("ename") or "Error")
                evalue = str(content.get("evalue") or "")
                self.last_error = f"{ename}: {evalue}" if evalue else ename
                traceback_lines = content.get("traceback") or []
                if traceback_lines:
                    writer.chunk(
                        "\n".join(_strip_ansi(str(line)) for line in traceback_lines) + "\n"
                    )
                else:
                    writer.chunk(self.last_error + "\n")

        async def async_execute_cell(
            self,
            cell: Any,
            cell_index: int,
            execution_count: int | None = None,
            store_history: bool = True,
        ):
            writer = self._trainhub_log
            is_code = cell.cell_type == "code" and str(cell.source).strip()
            if writer is not None and is_code:
                writer.line(f"── 单元格 [{cell_index + 1}] ──")
            started = time.monotonic()
            try:
                return await super().async_execute_cell(
                    cell,
                    cell_index,
                    execution_count=execution_count,
                    store_history=store_history,
                )
            except Exception:
                if writer is not None and is_code:
                    writer.line(
                        f"✗ 单元格 [{cell_index + 1}] 失败（{time.monotonic() - started:.1f}s）"
                    )
                raise
            else:
                if writer is not None and is_code:
                    writer.line(f"✓ 单元格 [{cell_index + 1}]（{time.monotonic() - started:.1f}s）")

    return StreamingNotebookClient


class RunLogWriter:
    """训练日志落盘：逐行加时间戳，有大小上限。"""

    def __init__(self, path: Path, max_bytes: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8", errors="replace")
        self._max_bytes = max(1024, max_bytes)
        try:
            self._bytes = path.stat().st_size
        except OSError:
            self._bytes = 0
        self._at_line_start = True
        self._truncated = False

    @staticmethod
    def _stamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S ")

    def _write(self, text: str) -> None:
        if self._truncated or not text:
            return
        size = len(text.encode("utf-8"))
        if self._bytes + size > self._max_bytes:
            notice = "\n[日志已达到大小上限，后续输出被截断]\n"
            self._fh.write(notice)
            self._fh.flush()
            self._truncated = True
            return
        self._fh.write(text)
        self._fh.flush()
        self._bytes += size

    def line(self, text: str = "") -> None:
        prefix = "" if self._at_line_start else "\n"
        self._write(f"{prefix}{self._stamp()}{text}\n")
        self._at_line_start = True

    def chunk(self, text: str) -> None:
        if not text:
            return
        parts: list[str] = []
        for piece in text.splitlines(keepends=True):
            if self._at_line_start:
                parts.append(self._stamp())
                self._at_line_start = False
            parts.append(piece)
            if piece.endswith("\n") or piece.endswith("\r"):
                self._at_line_start = True
        self._write("".join(parts))

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def _format_exception(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return truncate(_strip_ansi(text), _MESSAGE_LIMIT) or exc.__class__.__name__


class RunExecutor:
    """单实例内的训练执行器：DB 队列 + 固定并发 worker。"""

    def __init__(self, db: Database, settings: Settings, storage: Storage) -> None:
        self._db = db
        self._settings = settings
        self._storage = storage
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task] = []
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._active_clients: dict[str, Any] = {}
        self._cancel_requested: set[str] = set()
        self._stopping = False
        self._started = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        self._queue = asyncio.Queue()
        self._stopping = False
        interrupted = self._db.recover_interrupted_runs("服务重启，训练中断")
        if interrupted:
            logger.warning("已把 %s 个中断的训练任务标记为失败", interrupted)
        for run_id in self._db.list_queued_run_ids():
            self._queue.put_nowait(run_id)
        worker_count = max(1, self._settings.max_concurrent_runs)
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"trainhub-worker-{index}")
            for index in range(worker_count)
        ]
        self._started = True
        logger.info("训练执行器已启动（并发 %s）", worker_count)

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping = True
        for task in list(self._active_tasks.values()):
            task.cancel()
        for worker in self._workers:
            worker.cancel()
        pending = [*self._workers, *self._active_tasks.values()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._workers.clear()
        self._active_tasks.clear()
        self._active_clients.clear()
        self._db.recover_interrupted_runs("服务停止，训练中断")
        self._started = False
        logger.info("训练执行器已停止")

    def enqueue(self, run_id: str) -> None:
        if self._queue is None:
            logger.error("执行器尚未启动，训练任务 %s 将在服务启动后被拾取", run_id)
            return
        self._queue.put_nowait(run_id)

    def request_cancel(self, run_id: str, status: str) -> bool:
        if status == "queued" and self._db.cancel_queued_run(run_id):
            return True
        # 状态可能在读取与取消之间由 queued 变成 running：以数据库现值再判一次。
        current = self._db.get_run_any(run_id)
        if current is None or current["status"] not in ("queued", "running"):
            return False
        if current["status"] == "queued":
            return self._db.cancel_queued_run(run_id)
        self._cancel_requested.add(run_id)
        task = self._active_tasks.get(run_id)
        if task is not None:
            task.cancel()
        # 任务尚未登记时保留标记，_execute 入口会检查并直接结束。
        return True

    # ------------------------------------------------------------------ workers

    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        while True:
            run_id = await self._queue.get()
            if self._stopping:
                self._queue.task_done()
                return
            task = asyncio.create_task(self._execute(run_id), name=f"trainhub-run-{run_id}")
            self._active_tasks[run_id] = task
            try:
                await task
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                # 单个训练被用户取消，worker 继续下一个任务
            except Exception:
                logger.exception("训练任务执行异常：%s", run_id)
            finally:
                self._active_tasks.pop(run_id, None)
                self._queue.task_done()

    # ------------------------------------------------------------------ execution

    async def _execute(self, run_id: str) -> None:
        run = self._db.get_run_any(run_id)
        if run is None:
            return
        if not self._db.mark_run_running(run_id):
            return
        if run_id in self._cancel_requested:
            self._db.finish_run(
                run_id,
                status="cancelled",
                exit_code=None,
                message="训练已取消",
                log_path=None,
                output_path=None,
            )
            self._cancel_requested.discard(run_id)
            return
        run = self._db.get_run_any(run_id)
        assert run is not None

        customer_id = run["customer_id"]
        run_dir = self._storage.run_dir(customer_id, run_id)
        workdir = self._storage.run_work_dir(customer_id, run_id)
        output_dir = self._storage.run_output_dir(customer_id, run_id)
        log_path = self._storage.run_log_path(customer_id, run_id)
        home_dir = self._storage.run_home_dir(customer_id, run_id)
        tmp_dir = self._storage.run_tmp_dir(customer_id, run_id)

        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        for directory in (workdir / "dataset", output_dir, log_path.parent, home_dir, tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

        writer = RunLogWriter(log_path, self._settings.max_log_bytes)
        nb = None
        nbformat = None
        client = None
        status = "failed"
        exit_code: int | None = 1
        message: str | None = None
        started = time.monotonic()

        try:
            writer.line("TrainHub 训练开始")
            writer.line(f"任务：{run['name']}（{run_id}）")
            writer.line(f"Notebook：{run['notebook_name']}")
            dataset_label = run["dataset_name"] or "无"
            if run.get("dataset_owner_id") and run["dataset_owner_id"] != run["customer_id"]:
                dataset_label += f"（来自 {run.get('dataset_owner_name') or '其他客户'}）"
            writer.line(f"数据集：{dataset_label}")
            writer.line(f"提交时间：{run['created_at']}")

            bits = _jupyter_bits()
            nbformat = bits.nbformat

            notebook = self._db.get_notebook_any(run["notebook_id"])
            if notebook is None:
                raise RuntimeError("Notebook 不存在（可能在排队期间被删除）")
            source_path = Path(notebook["storage_path"])
            if not source_path.is_file():
                raise RuntimeError("Notebook 源文件缺失，请重新上传")
            shutil.copy2(source_path, workdir / "notebook.ipynb")

            self._link_dataset(run.get("dataset_id"), workdir / "dataset", writer)

            kernel = self._resolve_kernel(run)
            writer.line(f"内核：{kernel}")
            env = self._build_env(run, workdir, output_dir, home_dir, tmp_dir)

            nb = nbformat.read(str(workdir / "notebook.ipynb"), as_version=4)
            self._apply_kernel(nb, kernel)

            client_class = _streaming_client_class()
            client = client_class(
                nb,
                timeout=self._settings.cell_timeout_seconds or None,
                kernel_name=kernel,
                log_writer=writer,
            )
            self._active_clients[run_id] = client

            timeout_seconds = int(run.get("timeout_seconds") or 0)
            execution = client.async_execute(cwd=str(workdir), env=env)
            exec_task = asyncio.create_task(execution, name=f"trainhub-exec-{run_id}")
            writer.line("开始执行 notebook")
            try:
                if timeout_seconds > 0:
                    done, _pending = await asyncio.wait({exec_task}, timeout=timeout_seconds)
                    if not done:
                        exec_task.cancel()
                        try:
                            await exec_task
                        except BaseException:
                            pass
                        raise RuntimeError(f"训练超时（超过 {timeout_seconds} 秒）")
                    exec_task.result()
                else:
                    await exec_task
            except asyncio.CancelledError:
                exec_task.cancel()
                try:
                    await exec_task
                except BaseException:
                    pass
                raise
            if getattr(client, "last_error", None):
                raise RuntimeError(f"单元格执行失败：{client.last_error}")

            status = "succeeded"
            exit_code = 0
            message = "训练完成"
            writer.line(f"训练完成（{time.monotonic() - started:.1f}s）")
        except asyncio.CancelledError:
            if run_id in self._cancel_requested:
                status, exit_code, message = "cancelled", None, "训练已取消"
            else:
                status, exit_code, message = "failed", 1, "服务停止，训练中断"
            writer.line(message)
            raise
        except Exception as exc:
            if run_id in self._cancel_requested:
                # 取消会中断内核，nbclient 清理期可能抛 DeadKernelError 之类的异常，
                # 以取消标记为准，而不是异常类型。
                status, exit_code, message = "cancelled", None, "训练已取消"
                writer.line(message)
            else:
                status, exit_code = "failed", 1
                message = _format_exception(exc)
                last_error = getattr(client, "last_error", None)
                if last_error and last_error not in message:
                    message = f"{last_error}｜{message}"
                writer.line(f"训练失败：{message}")
        finally:
            await self._shutdown_client(client)
            if nb is not None and nbformat is not None:
                try:
                    nbformat.write(nb, str(output_dir / "executed_notebook.ipynb"))
                except Exception:
                    logger.warning("保存执行后的 notebook 失败", exc_info=True)
            try:
                writer.line(f"结束状态：{status}")
            finally:
                writer.close()
            message = truncate(message, _MESSAGE_LIMIT)
            self._db.finish_run(
                run_id,
                status=status,
                exit_code=exit_code,
                message=message,
                log_path=str(log_path),
                output_path=str(output_dir),
            )
            if not self._settings.keep_run_workdir:
                shutil.rmtree(workdir, ignore_errors=True)
                shutil.rmtree(home_dir, ignore_errors=True)
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._active_clients.pop(run_id, None)
            self._cancel_requested.discard(run_id)
            elapsed = time.monotonic() - started
            if status == "succeeded":
                logger.info("训练 %s 完成（%.1fs）", run_id, elapsed)
            else:
                logger.warning(
                    "训练 %s 结束：%s（%.1fs）%s", run_id, status, elapsed, message or ""
                )

    # ------------------------------------------------------------------ helpers

    def _link_dataset(self, dataset_id: str | None, target: Path, writer: RunLogWriter) -> None:
        if not dataset_id:
            return
        files = self._db.get_dataset_files(dataset_id)
        linked = 0
        for item in files:
            source = Path(item["storage_path"])
            if not source.is_file():
                writer.line(f"警告：数据集文件缺失 {item['filename']}")
                continue
            destination = target / item["filename"]
            try:
                os.symlink(source, destination)
            except OSError:
                try:
                    shutil.copy2(source, destination)
                except OSError:
                    writer.line(f"警告：无法挂载数据集文件 {item['filename']}")
                    continue
            linked += 1
        writer.line(f"数据集已挂载：{linked}/{len(files)} 个文件 → work/dataset")

    def _resolve_kernel(self, run: dict[str, Any]) -> str:
        kernels = list_kernels()
        names = {item["name"] for item in kernels}
        if not names:
            raise RuntimeError(
                "服务器没有可用的 Jupyter 内核，请在 GPU 节点执行 "
                "`python -m ipykernel install --name <kernel>` 注册训练环境"
            )
        requested = (run.get("kernel") or self._settings.default_kernel or "").strip()
        if requested and requested in names:
            return requested
        if self._settings.default_kernel in names:
            return self._settings.default_kernel
        return sorted(names)[0]

    @staticmethod
    def _apply_kernel(nb: Any, kernel: str) -> None:
        metadata = nb.setdefault("metadata", {})
        spec = metadata.setdefault("kernelspec", {})
        spec["name"] = kernel
        spec["display_name"] = spec.get("display_name") or kernel
        spec["language"] = spec.get("language") or "python"

    def _build_env(
        self, run: dict[str, Any], workdir: Path, output_dir: Path, home_dir: Path, tmp_dir: Path
    ) -> dict[str, str]:
        env = os.environ.copy()
        parameters = run.get("parameters") or "{}"
        env.update(
            {
                "WMG_RUN_ID": run["id"],
                "WMG_RUN_NAME": run["name"],
                "WMG_CUSTOMER_ID": run["customer_id"],
                "WMG_NOTEBOOK_NAME": run.get("notebook_name") or "",
                "WMG_DATASET_NAME": run.get("dataset_name") or "",
                "WMG_DATASET_DIR": str(workdir / "dataset"),
                "DATASET_DIR": str(workdir / "dataset"),
                "WMG_OUTPUT_DIR": str(output_dir),
                "OUTPUT_DIR": str(output_dir),
                "WMG_WORK_DIR": str(workdir),
                "WMG_PARAMS": parameters,
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
        return env

    async def _shutdown_client(self, client: Any) -> None:
        if client is None:
            return
        try:
            from jupyter_core.utils import ensure_async

            manager = getattr(client, "km", None)
            if manager is not None and await ensure_async(manager.is_alive()):
                await ensure_async(manager.shutdown_kernel(now=True))
                await ensure_async(manager.cleanup_resources())
        except Exception:
            logger.debug("清理训练内核失败", exc_info=True)


def scan_outputs(output_dir: Path) -> dict[str, Any]:
    files, truncated = iter_files(output_dir, limit=2000)
    return {
        "files": [{"path": path, "sizeBytes": size} for path, size in files],
        "count": len(files),
        "bytes": sum(size for _path, size in files),
        "truncated": truncated,
    }
