from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import (
    activity,
    admin,
    auth,
    datasets,
    health,
    notebooks,
    overview,
    runs,
    storage as storage_api,
)
from .config import PROJECT_ROOT, Settings
from .db import Database
from .kernel_sessions import KernelSessionManager
from .runtime_env import ensure_model_dirs
from .sandbox import DockerSandbox
from .responses import (
    ApiError,
    api_error_handler,
    http_error_handler,
    validation_error_handler,
)
from .runner import RunExecutor
from .storage import Storage

STATIC_DIR = PROJECT_ROOT / "static"

logger = logging.getLogger("trainhub")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    storage = Storage(settings.data_dir)
    storage.ensure_base_dirs()
    ensure_model_dirs(settings)
    db = Database(settings.db_path)
    sandbox = DockerSandbox(settings)
    executor = RunExecutor(db, settings, storage, sandbox=sandbox)
    kernel_sessions = KernelSessionManager(db, settings, storage, sandbox=sandbox)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if sandbox.enabled:
            status = await asyncio.to_thread(sandbox.preflight)
            if not status.get("ok"):
                logger.error("Docker 沙箱不可用：%s", status.get("message"))
            else:
                removed = await asyncio.to_thread(sandbox.reap_orphans, set())
                stale_ipc = await asyncio.to_thread(sandbox.cleanup_ipc_root)
                if removed or stale_ipc:
                    logger.info("已清理 %s 个遗留沙箱容器、%s 个遗留 IPC 目录", removed, stale_ipc)
        await executor.start()
        await kernel_sessions.start()
        try:
            yield
        finally:
            await kernel_sessions.stop()
            await executor.stop()
            if sandbox.enabled:
                await asyncio.to_thread(sandbox.reap_orphans, set())
                await asyncio.to_thread(sandbox.cleanup_ipc_root)
            db.close()

    app = FastAPI(
        title="TrainHub",
        description="Kaggle 风格的单节点模型训练管理台",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.storage = storage
    app.state.db = db
    app.state.executor = executor
    app.state.kernel_sessions = kernel_sessions
    app.state.sandbox = sandbox

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)

    for module in (health, auth, overview, datasets, notebooks, runs, storage_api, activity, admin):
        app.include_router(module.router)

    # 零构建 SPA 没有构建哈希：HTML/JS/CSS 必须每次重新验证，否则浏览器会
    # 在部署后继续使用旧模块。字体文件很少变，保留条件缓存即可。
    @app.middleware("http")
    async def static_cache_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path == "/index.html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        elif path.startswith("/assets/"):
            if path.lower().endswith((".woff2", ".woff", ".ttf", ".otf")):
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
        return response

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:  # pragma: no cover - 打包缺失时的显式提示
        logger.warning("未找到静态控制台目录：%s", Path(STATIC_DIR))

    return app
