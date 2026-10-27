from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import activity, admin, auth, datasets, health, notebooks, overview, runs
from .config import PROJECT_ROOT, Settings
from .db import Database
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
    db = Database(settings.db_path)
    executor = RunExecutor(db, settings, storage)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await executor.start()
        try:
            yield
        finally:
            await executor.stop()
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

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)

    for module in (health, auth, overview, datasets, notebooks, runs, activity, admin):
        app.include_router(module.router)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:  # pragma: no cover - 打包缺失时的显式提示
        logger.warning("未找到静态控制台目录：%s", Path(STATIC_DIR))

    return app
