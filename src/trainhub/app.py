"""Uvicorn 入口：`uvicorn trainhub.app:app`。"""

from .main import create_app

app = create_app()
