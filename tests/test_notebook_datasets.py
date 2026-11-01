from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from helpers import notebook_bytes, upload_dataset
from trainhub.responses import ApiError


class FakeSession:
    """只实现数据集挂载所需的最小会话接口。"""

    def __init__(self, dataset_dir: Path) -> None:
        self.dataset_dir = dataset_dir
        self.dataset_dir.mkdir(parents=True, exist_ok=True)
        self.dataset_id: str | None = None
        self.dataset_name: str | None = None
        self.dataset_signature: str | None = None
        self.dataset_lock = asyncio.Lock()
        self.run_task = None
        self.lock = asyncio.Lock()

    def is_busy(self) -> bool:
        return self.lock.locked()


async def _create_notebook(client, headers) -> dict:
    content = json.loads(notebook_bytes("print(1)").decode("utf-8"))
    response = await client.post(
        "/api/notebooks", headers=headers, json={"name": "Dataset Notebook", "content": content}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_set_and_clear_notebook_dataset(client, headers):
    notebook = await _create_notebook(client, headers)
    dataset = await upload_dataset(client, headers, name="train-set")

    response = await client.put(
        f"/api/notebooks/{notebook['id']}/dataset",
        headers=headers,
        json={"datasetId": dataset["id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["datasetId"] == dataset["id"]

    detail = await client.get(f"/api/notebooks/{notebook['id']}", headers=headers)
    assert detail.json()["data"]["datasetId"] == dataset["id"]

    cleared = await client.put(
        f"/api/notebooks/{notebook['id']}/dataset",
        headers=headers,
        json={"datasetId": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["datasetId"] is None


async def test_notebook_dataset_visibility_rules(client, db, headers):
    notebook = await _create_notebook(client, headers)
    other_token = "trh_other_customer_token_1234567890"
    other = db.create_customer("Other Co", "other@example.com", other_token)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    private_dataset = await upload_dataset(client, other_headers, name="private-set")
    denied = await client.put(
        f"/api/notebooks/{notebook['id']}/dataset",
        headers=headers,
        json={"datasetId": private_dataset["id"]},
    )
    assert denied.status_code == 404

    shared_dataset = await upload_dataset(client, other_headers, name="shared-set", shared=True)
    allowed = await client.put(
        f"/api/notebooks/{notebook['id']}/dataset",
        headers=headers,
        json={"datasetId": shared_dataset["id"]},
    )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["datasetId"] == shared_dataset["id"]

    # 取消共享后，其他客户 Notebook 上的引用应被清除
    unshare = await client.patch(
        f"/api/datasets/{shared_dataset['id']}/sharing",
        headers=other_headers,
        json={"shared": False},
    )
    assert unshare.status_code == 200
    detail = await client.get(f"/api/notebooks/{notebook['id']}", headers=headers)
    assert detail.json()["data"]["datasetId"] is None


async def test_delete_dataset_clears_notebook_reference(client, headers):
    notebook = await _create_notebook(client, headers)
    dataset = await upload_dataset(client, headers, name="to-delete")
    await client.put(
        f"/api/notebooks/{notebook['id']}/dataset",
        headers=headers,
        json={"datasetId": dataset["id"]},
    )
    deleted = await client.delete(f"/api/datasets/{dataset['id']}", headers=headers)
    assert deleted.status_code == 200
    detail = await client.get(f"/api/notebooks/{notebook['id']}", headers=headers)
    assert detail.json()["data"]["datasetId"] is None


async def test_session_dataset_mount_and_clear(client, app, db, customer, headers, tmp_path):
    notebook = await _create_notebook(client, headers)
    dataset = await upload_dataset(client, headers, name="mount-set")
    db.set_notebook_dataset(customer["id"], notebook["id"], dataset["id"])
    row = db.get_notebook(customer["id"], notebook["id"])
    assert row is not None and row["dataset_id"] == dataset["id"]

    manager = app.state.kernel_sessions
    session = FakeSession(tmp_path / "dataset")
    await manager._apply_dataset(session, customer["id"], row)

    assert session.dataset_id == dataset["id"]
    mount_dir = tmp_path / "dataset"
    assert sorted(item.name for item in mount_dir.iterdir()) == ["a.txt"]
    assert (mount_dir / "a.txt").read_bytes() == b"data-ok"

    db.set_notebook_dataset(customer["id"], notebook["id"], None)
    row = db.get_notebook(customer["id"], notebook["id"])
    await manager._apply_dataset(session, customer["id"], row)
    assert session.dataset_id is None
    assert list(mount_dir.iterdir()) == []


async def test_switch_dataset_rejected_while_running(client, app, db, customer, headers, tmp_path):
    notebook = await _create_notebook(client, headers)
    dataset = await upload_dataset(client, headers, name="busy-set")
    db.set_notebook_dataset(customer["id"], notebook["id"], dataset["id"])
    row = db.get_notebook(customer["id"], notebook["id"])
    assert row is not None

    manager = app.state.kernel_sessions
    session = FakeSession(tmp_path / "dataset")
    await session.lock.acquire()
    try:
        with pytest.raises(ApiError) as exc:
            await manager._apply_dataset(session, customer["id"], row)
        assert exc.value.status_code == 409
    finally:
        session.lock.release()
