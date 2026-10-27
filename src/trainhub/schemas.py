from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_TIMEOUT_SECONDS = 30 * 24 * 3600
MAX_PARAMS_BYTES = 16 * 1024


class RunCreate(BaseModel):
    notebookId: str = Field(min_length=1, max_length=64)
    datasetId: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    kernel: str | None = Field(default=None, max_length=64)
    parameters: dict[str, Any] | None = None
    timeoutSeconds: int | None = Field(default=None, ge=0, le=MAX_TIMEOUT_SECONDS)

    @field_validator("parameters")
    @classmethod
    def _check_parameters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"parameters 必须是合法 JSON：{exc}") from exc
        if len(encoded.encode("utf-8")) > MAX_PARAMS_BYTES:
            raise ValueError("parameters 不能超过 16KB")
        return value


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("客户名称不能为空")
        return value


class CustomerStatusUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        if value not in ("active", "disabled"):
            raise ValueError("status 只能是 active 或 disabled")
        return value


class DatasetSharingUpdate(BaseModel):
    """数据集共享开关（仅所有者可改）。"""

    shared: bool
