from __future__ import annotations

from helpers import upload_dataset, upload_notebook


async def test_storage_requires_auth(client):
    response = await client.get("/api/storage")
    assert response.status_code == 401


async def test_storage_breakdown_and_details(client, app, db, customer, headers):
    await upload_dataset(client, headers, name="storage-set", content=b"x" * 512)
    notebook = await upload_notebook(client, headers, "print(1)", name="storage-nb")

    # 其他客户共享的数据集属于所有者目录，不计入当前客户占用。
    db.create_customer("Other Co", None, "trh_other_storage_token_1234567890")
    other_headers = {"Authorization": "Bearer trh_other_storage_token_1234567890"}
    await upload_dataset(
        client, other_headers, name="shared-storage", content=b"y" * 4096, shared=True
    )

    notebook_row = db.get_notebook(customer["id"], notebook["id"])
    assert notebook_row is not None
    run = db.create_run(
        customer_id=customer["id"],
        notebook=notebook_row,
        dataset=None,
        name="storage-run",
        kernel=None,
        parameters_json=None,
        timeout_seconds=0,
    )
    run_dir = app.state.storage.run_dir(customer["id"], run["id"])
    (run_dir / "output").mkdir(parents=True, exist_ok=True)
    (run_dir / "output" / "checkpoint.bin").write_bytes(b"z" * 2048)

    response = await client.get("/api/storage", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    categories = {item["key"]: item for item in data["categories"]}
    assert categories["datasets"]["count"] == 1
    assert categories["notebooks"]["count"] == 1
    assert categories["runs"]["count"] == 1
    assert categories["datasets"]["bytes"] >= 512
    assert categories["notebooks"]["bytes"] > 0
    assert categories["runs"]["bytes"] >= 2048
    assert data["customerBytes"] >= sum(
        categories[key]["bytes"] for key in ("datasets", "notebooks", "runs")
    )
    assert data["disk"]["totalBytes"] > 0

    datasets = data["details"]["datasets"]
    assert datasets["total"] == 1
    assert datasets["truncated"] is False
    assert datasets["items"][0]["name"] == "storage-set"
    assert datasets["items"][0]["bytes"] >= 512

    notebooks = data["details"]["notebooks"]
    assert notebooks["items"][0]["name"] == "storage-nb"
    assert notebooks["items"][0]["bytes"] > 0

    runs = data["details"]["runs"]
    assert runs["items"][0]["name"] == "storage-run"
    assert runs["items"][0]["bytes"] >= 2048
    assert runs["items"][0]["status"] == "queued"
