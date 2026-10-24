from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """业务错误：统一转换为 {code, message, data} 信封。"""

    def __init__(self, message: str, status_code: int = 400, code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code
        self.code = code if code is not None else status_code
        super().__init__(message)


def ok(data: Any = None, message: str = "ok", code: int = 200) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}


def page_data(records: list[Any], total: int, current: int, size: int) -> dict[str, Any]:
    pages = (total + size - 1) // size if size else 0
    return {
        "records": records,
        "total": total,
        "size": size,
        "current": current,
        "pages": pages,
    }


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "data": None},
    )


def _validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "请求参数不合法"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    detail = first.get("msg", "参数不合法")
    return f"请求参数不合法：{location} {detail}".strip()


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": _validation_message(exc), "data": None},
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": detail, "data": None},
    )
