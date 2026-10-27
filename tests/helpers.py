from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from trainhub.config import Settings


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "trainhub.db",
        admin_token="admin-test-token",
        host="127.0.0.1",
        port=0,
        max_concurrent_runs=1,
        default_timeout_seconds=0,
        cell_timeout_seconds=0,
        max_dataset_mb=5,
        max_notebook_mb=2,
        max_log_mb=5,
        keep_run_workdir=False,
        default_kernel="python3",
        log_level="warning",
    )
    base.update(overrides)
    return Settings(**base)


def notebook_bytes(*sources: str) -> bytes:
    cells = [
        {
            "cell_type": "code",
            "id": f"cell-{index}",
            "metadata": {},
            "execution_count": None,
            "source": source,
            "outputs": [],
        }
        for index, source in enumerate(sources)
    ]
    payload = {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "name": "python3",
                "display_name": "Python 3",
                "language": "python",
            },
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(payload).encode("utf-8")


async def upload_dataset(
    client,
    headers: dict[str, str],
    *,
    name: str = "dataset",
    filename: str = "a.txt",
    content: bytes = b"data-ok",
    shared: bool = False,
) -> dict[str, Any]:
    data: dict[str, str] = {"name": name}
    if shared:
        data["shared"] = "true"
    response = await client.post(
        "/api/datasets",
        headers=headers,
        data=data,
        files=[("files", (filename, content, "application/octet-stream"))],
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def upload_notebook(
    client,
    headers: dict[str, str],
    *sources: str,
    name: str = "train",
) -> dict[str, Any]:
    response = await client.post(
        "/api/notebooks",
        headers=headers,
        data={"name": name},
        files=[("file", ("train.ipynb", notebook_bytes(*sources), "application/json"))],
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def wait_for_status(
    client,
    headers: dict[str, str],
    run_id: str,
    statuses: set[str] | tuple[str, ...],
    timeout: float = 90.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = await client.get(f"/api/runs/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()["data"]
        if last["status"] in statuses:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"run {run_id} did not reach {statuses}; last={last}")
