from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ..config import Settings
from ..db import Database
from ..responses import ApiError, ok, page_data
from ..schemas import (
    NotebookCellExecute,
    NotebookDatasetUpdate,
    NotebookExecuteUpto,
    NotebookRunStart,
    NotebookSessionStart,
    NotebookSessionWrite,
    NotebookWrite,
)
from ..serializers import notebook_public, run_public
from ..services import (
    create_manual_notebook,
    create_notebook,
    delete_notebook,
    ensure_cells_allowed,
    notebook_content,
    notebook_download,
    notebook_session_get,
    notebook_session_save,
    update_notebook,
    update_notebook_dataset,
)
from ..storage import Storage
from .deps import client_ip, current_customer, get_db, get_settings, get_storage

router = APIRouter(prefix="/api/notebooks", tags=["notebooks"])


@router.get("")
def list_notebooks(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, max_length=120),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_notebooks(customer["id"], page, size, q)
    return ok(page_data([notebook_public(row) for row in rows], total, page, size))


@router.post("")
def create_notebook_manual(
    request: Request,
    payload: NotebookWrite,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    notebook = create_manual_notebook(
        db=db,
        storage=storage,
        settings=settings,
        customer=customer,
        name=payload.name,
        content=payload.content,
        ip=client_ip(request),
    )
    return ok(notebook_public(notebook), message="Notebook 已创建", code=201)


@router.post("/import")
async def import_notebook(
    request: Request,
    name: str | None = Form(None),
    file: UploadFile = File(...),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    notebook = await create_notebook(
        db=db,
        storage=storage,
        settings=settings,
        customer=customer,
        name=name,
        upload=file,
        ip=client_ip(request),
    )
    return ok(notebook_public(notebook), message="Notebook 已导入", code=201)


def _owned_notebook(db: Database, customer: dict, notebook_id: str) -> dict:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    return notebook


@router.post("/{notebook_id}/sessions")
async def start_notebook_session(
    request: Request,
    notebook_id: str,
    payload: NotebookSessionStart | None = None,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    notebook = _owned_notebook(db, customer, notebook_id)
    manager = request.app.state.kernel_sessions
    info = await manager.ensure_session(
        customer=customer,
        notebook=notebook,
        kernel_name=payload.kernel if payload else None,
    )
    return ok(info, message="Notebook 内核已就绪")


@router.post("/{notebook_id}/sessions/restart")
async def restart_notebook_session(
    request: Request,
    notebook_id: str,
    payload: NotebookSessionStart | None = None,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """重启交互式内核：丢弃当前内核（清空变量与导入状态）并新建干净会话；不运行任何代码。"""
    notebook = _owned_notebook(db, customer, notebook_id)
    manager = request.app.state.kernel_sessions
    info = await manager.restart_session(
        customer=customer,
        notebook=notebook,
        kernel_name=payload.kernel if payload else None,
    )
    return ok(info, message="Notebook 内核已重启")


@router.post("/{notebook_id}/sessions/{session_id}/execute")
async def execute_notebook_cell(
    request: Request,
    notebook_id: str,
    session_id: str,
    payload: NotebookCellExecute,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    _owned_notebook(db, customer, notebook_id)
    # 单任务实例：训练任务排队/执行期间不允许运行单元格（GPU 独占、抢占式调度）
    ensure_cells_allowed(db)
    manager = request.app.state.kernel_sessions
    result = await manager.execute(
        customer_id=customer["id"],
        notebook_id=notebook_id,
        session_id=session_id,
        code=payload.code,
        timeout_seconds=payload.timeoutSeconds,
        cell_id=payload.cellId,
    )
    return ok(result)


MAX_EXECUTE_UPTO_BYTES = 4 * 1024 * 1024


@router.post("/{notebook_id}/sessions/{session_id}/execute-upto")
async def execute_notebook_cells_upto(
    request: Request,
    notebook_id: str,
    session_id: str,
    payload: NotebookExecuteUpto,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """运行到指定单元格：补跑尚未执行（或源码已变）的前置代码单元格，再运行目标单元格。"""
    notebook = _owned_notebook(db, customer, notebook_id)
    ensure_cells_allowed(db)
    if sum(len(cell.source) for cell in payload.cells) > MAX_EXECUTE_UPTO_BYTES:
        raise ApiError("提交的代码总量超过上限（4MB）", 413)
    cells = [{"cellId": cell.cellId, "source": cell.source} for cell in payload.cells]
    if payload.targetCellId is not None:
        ids = [cell["cellId"] for cell in cells]
        if payload.targetCellId not in ids:
            raise ApiError("目标单元格不在提交的代码列表中", 400)
        cells = cells[: ids.index(payload.targetCellId) + 1]
    manager = request.app.state.kernel_sessions
    if payload.restart:
        info = await manager.restart_session(
            customer=customer,
            notebook=notebook,
            kernel_name=payload.kernel,
            session_id=session_id,
        )
        session_id = info["sessionId"]
    result = await manager.execute_sequence(
        customer_id=customer["id"],
        notebook_id=notebook_id,
        session_id=session_id,
        cells=cells,
        timeout_seconds=payload.timeoutSeconds,
        target_cell_id=payload.targetCellId,
    )
    return ok(result, message="已运行到指定单元格")


@router.post("/{notebook_id}/runs")
async def start_notebook_run(
    request: Request,
    notebook_id: str,
    payload: NotebookRunStart,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """启动一次后台执行，立即返回运行态；结果由 GET /runs/current 取回。

    三种模式：cell=单格；upto=运行到此处（补跑缺失/变更的前置格并跳过已执行且未变的格）；
    all=运行全部（全部代码格按顺序执行，不跳过已执行格）。
    刷新页面不会中断执行；新页面查询运行态即可接管（运行中继续轮询，已结束回填输出）。
    """
    notebook = _owned_notebook(db, customer, notebook_id)
    ensure_cells_allowed(db)
    if payload.mode == "cell":
        if not (payload.code or "").strip():
            raise ApiError("单格运行需要非空代码", 400)
        cells = [{"cellId": payload.cellId or "", "source": payload.code or ""}]
        force_cell_id = payload.cellId or None
        force_all = False
    elif payload.mode == "upto":
        if not payload.cells:
            raise ApiError("运行到此处需要至少一个单元格", 400)
        if sum(len(cell.source) for cell in payload.cells) > MAX_EXECUTE_UPTO_BYTES:
            raise ApiError("提交的代码总量超过上限（4MB）", 413)
        cells = [{"cellId": cell.cellId, "source": cell.source} for cell in payload.cells]
        if payload.targetCellId is not None:
            ids = [cell["cellId"] for cell in cells]
            if payload.targetCellId not in ids:
                raise ApiError("目标单元格不在提交的代码列表中", 400)
            cells = cells[: ids.index(payload.targetCellId) + 1]
        force_cell_id = payload.targetCellId
        force_all = False
    else:
        if not payload.cells:
            raise ApiError("运行全部需要至少一个单元格", 400)
        if sum(len(cell.source) for cell in payload.cells) > MAX_EXECUTE_UPTO_BYTES:
            raise ApiError("提交的代码总量超过上限（4MB）", 413)
        cells = [{"cellId": cell.cellId, "source": cell.source} for cell in payload.cells]
        force_cell_id = None
        force_all = True
    manager = request.app.state.kernel_sessions
    if payload.restart:
        info = await manager.restart_session(
            customer=customer, notebook=notebook, kernel_name=payload.kernel
        )
    else:
        info = await manager.ensure_session(
            customer=customer, notebook=notebook, kernel_name=payload.kernel
        )
    result = await manager.start_run(
        customer_id=customer["id"],
        notebook_id=notebook_id,
        session_id=info["sessionId"],
        mode=payload.mode,
        cells=cells,
        force_cell_id=force_cell_id,
        timeout_seconds=payload.timeoutSeconds,
        force_all=force_all,
    )
    return ok(result, message="已开始运行")


@router.get("/{notebook_id}/runs/current")
async def current_notebook_run(
    request: Request,
    notebook_id: str,
    outputs: bool = Query(False),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """最近一次运行态（运行中或已结束）；没有会话/运行记录时返回 null。

    `outputs=0` 只给逐格 hasOutputs；`outputs=1` 带输出（初载/终态取回用）。
    """
    _owned_notebook(db, customer, notebook_id)
    manager = request.app.state.kernel_sessions
    return ok(
        manager.current_run(
            customer_id=customer["id"], notebook_id=notebook_id, include_outputs=outputs
        )
    )


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/{notebook_id}/runs/stream")
async def stream_notebook_run(
    request: Request,
    notebook_id: str,
    runId: str | None = Query(None, max_length=64),
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
):
    """跟随一次运行的 SSE 事件流：先发 state 快照，之后实时推送输出增量与状态。

    事件：state（快照）/ count / cell / stream（新增文本）/ output（新输出项）/
    outputs（整体替换）/ run（终态）/ resync（跟不上，请重取快照）。
    """
    _owned_notebook(db, customer, notebook_id)
    manager = request.app.state.kernel_sessions
    session = manager.get_session(customer_id=customer["id"], notebook_id=notebook_id)
    if session is None or session.latest_run is None:
        raise ApiError("没有可跟随的运行", 404)
    if runId and session.latest_run.get("runId") != runId:
        raise ApiError("运行不存在或已被新的运行取代", 404)
    queue = session.subscribe_run()
    snapshot = manager.current_run(
        customer_id=customer["id"], notebook_id=notebook_id, include_outputs=True
    )
    cut = int((snapshot or {}).get("lastEventSeq") or 0)
    pending: list[dict] = []
    while True:
        try:
            queued = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if int(queued.get("seq") or 0) > cut:
            pending.append(queued)

    async def event_stream():
        try:
            yield _sse_event({"e": "state", "run": snapshot})
            if not snapshot:
                return
            if snapshot.get("status") != "running":
                # 状态快照已经是终态：补一个终态事件，客户端无需等一个可能已经错过的 run 事件。
                yield _sse_event(
                    {
                        "e": "run",
                        "runId": snapshot.get("runId"),
                        "status": snapshot.get("status"),
                        "failedCellId": snapshot.get("failedCellId"),
                        "message": snapshot.get("message"),
                    }
                )
                return
            for item in pending:
                yield _sse_event(item)
                if item.get("e") == "run":
                    return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse_event(event)
                if event.get("e") == "run":
                    break
        finally:
            session.unsubscribe_run(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/{notebook_id}/sessions/{session_id}")
async def close_notebook_session(
    request: Request,
    notebook_id: str,
    session_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    _owned_notebook(db, customer, notebook_id)
    manager = request.app.state.kernel_sessions
    await manager.close(customer_id=customer["id"], notebook_id=notebook_id, session_id=session_id)
    return ok(None, message="Notebook 内核已关闭")


@router.get("/{notebook_id}/content")
def get_notebook_content(
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    return ok(notebook_content(db=db, customer=customer, notebook_id=notebook_id))


@router.get("/{notebook_id}/session")
def get_notebook_session(
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    return ok(notebook_session_get(db=db, customer=customer, notebook_id=notebook_id))


@router.put("/{notebook_id}/session")
def save_notebook_session(
    notebook_id: str,
    payload: NotebookSessionWrite,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    result = notebook_session_save(
        db=db,
        settings=settings,
        customer=customer,
        notebook_id=notebook_id,
        cells=payload.cells,
    )
    return ok(result, message="会话已保存")


@router.put("/{notebook_id}/dataset")
async def set_notebook_dataset(
    request: Request,
    notebook_id: str,
    payload: NotebookDatasetUpdate,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """为 Notebook 选择交互式会话挂载的数据集（null = 不使用）。"""
    _owned_notebook(db, customer, notebook_id)
    manager = request.app.state.kernel_sessions
    if manager.dataset_busy(customer_id=customer["id"], notebook_id=notebook_id):
        raise ApiError("该 Notebook 正在运行，请结束后再切换数据集", 409)
    notebook = update_notebook_dataset(
        db=db,
        customer=customer,
        notebook_id=notebook_id,
        dataset_id=payload.datasetId,
    )
    await manager.refresh_dataset(customer_id=customer["id"], notebook_id=notebook_id)
    return ok(notebook_public(notebook), message="数据集已更新")


@router.put("/{notebook_id}")
def save_notebook(
    request: Request,
    notebook_id: str,
    payload: NotebookWrite,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict:
    notebook = update_notebook(
        db=db,
        storage=storage,
        settings=settings,
        customer=customer,
        notebook_id=notebook_id,
        name=payload.name,
        content=payload.content,
        ip=client_ip(request),
    )
    return ok(notebook_public(notebook), message="Notebook 已保存")


@router.get("/{notebook_id}")
def get_notebook(
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    return ok(notebook_public(notebook))


@router.get("/{notebook_id}/last-run")
def get_notebook_last_run(
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> dict:
    """提交训练时回填该 Notebook 最近一次运行的参数（没有运行则返回 null）。"""
    notebook = db.get_notebook(customer["id"], notebook_id)
    if notebook is None:
        raise ApiError("Notebook 不存在", 404)
    run = db.latest_run_for_notebook(customer["id"], notebook_id)
    return ok(run_public(run) if run else None)


@router.get("/{notebook_id}/download")
async def download_notebook(
    request: Request,
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
) -> Response:
    payload, filename = await run_in_threadpool(
        notebook_download, db=db, customer=customer, notebook_id=notebook_id
    )
    db.add_activity(
        customer["id"],
        "notebook.download",
        target_type="notebook",
        target_id=notebook_id,
        detail=filename,
        ip=client_ip(request),
    )
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "notebook.ipynb"
    disposition = f'attachment; filename="{ascii_name}"; ' f"filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type="application/x-ipynb+json",
        headers={"Content-Disposition": disposition},
    )


@router.delete("/{notebook_id}")
def remove_notebook(
    request: Request,
    notebook_id: str,
    customer: dict = Depends(current_customer),
    db: Database = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> dict:
    delete_notebook(
        db=db,
        storage=storage,
        customer=customer,
        notebook_id=notebook_id,
        ip=client_ip(request),
    )
    return ok(None, message="Notebook 已删除")
