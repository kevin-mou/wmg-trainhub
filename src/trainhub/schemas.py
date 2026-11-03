from __future__ import annotations

import json
from typing import Any, Literal

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
    # 可选：指定访问令牌（导入既有身份/多实例统一身份时使用）；缺省由服务端生成
    token: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("客户名称不能为空")
        return value

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value.startswith("trh_"):
            raise ValueError("访问令牌必须以 trh_ 开头")
        if any(char.isspace() for char in value):
            raise ValueError("访问令牌不能包含空白字符")
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


class NotebookWrite(BaseModel):
    """手动新建/编辑 Notebook；content 是合法的 .ipynb JSON 对象。"""

    name: str | None = Field(default=None, max_length=120)
    content: dict[str, Any]


class NotebookSessionStart(BaseModel):
    """启动/复用 Notebook 交互式内核；kernel 为空时使用节点默认内核。"""

    kernel: str | None = Field(default=None, max_length=64)


class NotebookCellExecute(BaseModel):
    """在 Notebook 内核中执行一个代码单元。"""

    code: str = Field(min_length=1, max_length=1_000_000)
    cellId: str | None = Field(default=None, max_length=64)
    timeoutSeconds: int | None = Field(default=None, ge=0, le=3600)


class NotebookExecuteCell(BaseModel):
    """「运行到此处」提交的单个代码单元（按 notebook 顺序）。"""

    cellId: str = Field(min_length=1, max_length=64)
    source: str = Field(default="", max_length=1_000_000)


class NotebookRunStart(BaseModel):
    """启动一次后台执行：cell=单格，upto=运行到此处（自动跳过未变的前置格），all=全部代码单元按顺序运行。"""

    mode: Literal["cell", "upto", "all"]
    code: str | None = Field(default=None, max_length=1_000_000)
    cellId: str | None = Field(default=None, max_length=64)
    cells: list[NotebookExecuteCell] = Field(default_factory=list, max_length=500)
    targetCellId: str | None = Field(default=None, max_length=64)
    timeoutSeconds: int | None = Field(default=None, ge=0, le=3600)
    restart: bool = False
    kernel: str | None = Field(default=None, max_length=64)


class NotebookExecuteUpto(BaseModel):
    """运行到指定单元格：先按顺序补跑未执行（或源码已变化）的前置代码单元格。"""

    cells: list[NotebookExecuteCell] = Field(min_length=1, max_length=500)
    targetCellId: str | None = Field(default=None, max_length=64)
    timeoutSeconds: int | None = Field(default=None, ge=0, le=3600)
    restart: bool = False
    kernel: str | None = Field(default=None, max_length=64)


class NotebookSessionCellWrite(BaseModel):
    """Save Session 的单个 cell 快照；source 用于校验代码未被偷偷改动。"""

    cellId: str = Field(min_length=1, max_length=64)
    source: str = Field(max_length=1_000_000)
    executionCount: int | None = Field(default=None, ge=0)
    outputs: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class NotebookSessionWrite(BaseModel):
    """一次保存整个 Notebook 的运行会话快照。"""

    cells: list[NotebookSessionCellWrite] = Field(default_factory=list, max_length=1000)
