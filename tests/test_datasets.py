from __future__ import annotations

import io
import zipfile

from helpers import make_settings, notebook_bytes, upload_dataset

from trainhub.main import create_app


def _dataset_payload(name: str = "toy"):
    files = [
        ("files", ("a.txt", b"hello trainhub", "text/plain")),
        ("files", ("nested/../b.csv", b"1,2\n3,4\n", "text/csv")),
    ]
    return {"name": name, "description": "demo dataset"}, files


async def test_upload_list_detail_and_download(client, headers):
    data, files = _dataset_payload()
    response = await client.post("/api/datasets", headers=headers, data=data, files=files)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 201
    dataset = body["data"]
    assert dataset["fileCount"] == 2
    assert dataset["sizeBytes"] == len(b"hello trainhub") + len(b"1,2\n3,4\n")

    listing = await client.get("/api/datasets", headers=headers)
    records = listing.json()["data"]["records"]
    assert len(records) == 1
    assert records[0]["name"] == "toy"

    detail = await client.get(f"/api/datasets/{dataset['id']}", headers=headers)
    filenames = [item["filename"] for item in detail.json()["data"]["files"]]
    assert filenames == ["a.txt", "b.csv"]  # 目录被剥离，文件名安全化

    download = await client.get(f"/api/datasets/{dataset['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert set(archive.namelist()) == {"a.txt", "b.csv"}
        assert archive.read("a.txt") == b"hello trainhub"


async def test_single_file_dataset_downloads_original(client, headers):
    response = await client.post(
        "/api/datasets",
        headers=headers,
        data={"name": "single"},
        files=[("files", ("data.csv", b"x,y\n1,2\n", "text/csv"))],
    )
    dataset = response.json()["data"]
    download = await client.get(f"/api/datasets/{dataset['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.content == b"x,y\n1,2\n"
    assert "data.csv" in download.headers.get("content-disposition", "")


async def test_dataset_isolation_between_customers(client, db, headers):
    response = await client.post(
        "/api/datasets",
        headers=headers,
        data={"name": "private"},
        files=[("files", ("secret.txt", b"top secret", "text/plain"))],
    )
    dataset_id = response.json()["data"]["id"]

    db.create_customer("Other Co", None, "trh_other_customer_token_abcdef")
    other_headers = {"Authorization": "Bearer trh_other_customer_token_abcdef"}

    assert (await client.get("/api/datasets", headers=other_headers)).json()["data"][
        "records"
    ] == []
    assert (
        await client.get(f"/api/datasets/{dataset_id}", headers=other_headers)
    ).status_code == 404
    assert (
        await client.get(f"/api/datasets/{dataset_id}/download", headers=other_headers)
    ).status_code == 404
    assert (
        await client.delete(f"/api/datasets/{dataset_id}", headers=other_headers)
    ).status_code == 404


async def test_delete_dataset(client, headers):
    response = await client.post(
        "/api/datasets",
        headers=headers,
        data={"name": "temp"},
        files=[("files", ("t.txt", b"x", "text/plain"))],
    )
    dataset_id = response.json()["data"]["id"]
    deleted = await client.delete(f"/api/datasets/{dataset_id}", headers=headers)
    assert deleted.status_code == 200
    assert (await client.get(f"/api/datasets/{dataset_id}", headers=headers)).status_code == 404
    assert (await client.get("/api/datasets", headers=headers)).json()["data"]["total"] == 0


async def test_dataset_size_limit(tmp_path):
    from httpx import ASGITransport, AsyncClient

    app = create_app(make_settings(tmp_path, max_dataset_mb=0))
    db = app.state.db
    db.create_customer("Limit Co", None, "trh_limit_customer_token_123")
    headers = {"Authorization": "Bearer trh_limit_customer_token_123"}
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://trainhub.test") as client:
            response = await client.post(
                "/api/datasets",
                headers=headers,
                data={"name": "too big"},
                files=[("files", ("big.bin", b"x" * 2048, "application/octet-stream"))],
            )
            assert response.status_code == 413
            assert "上限" in response.json()["message"]
            listing = await client.get("/api/datasets", headers=headers)
            assert listing.json()["data"]["total"] == 0


async def test_delete_dataset_blocked_while_run_active(client, headers, db):
    dataset = (
        await client.post(
            "/api/datasets",
            headers=headers,
            data={"name": "busy"},
            files=[("files", ("a.txt", b"x", "text/plain"))],
        )
    ).json()["data"]
    notebook = (
        await client.post(
            "/api/notebooks",
            headers=headers,
            data={"name": "sleepy"},
            files=[
                (
                    "file",
                    (
                        "sleepy.ipynb",
                        notebook_bytes("import time\ntime.sleep(30)"),
                        "application/json",
                    ),
                )
            ],
        )
    ).json()["data"]
    run = (
        await client.post(
            "/api/runs",
            headers=headers,
            json={"notebookId": notebook["id"], "datasetId": dataset["id"]},
        )
    ).json()["data"]

    import asyncio

    for _ in range(80):
        detail = (await client.get(f"/api/runs/{run['id']}", headers=headers)).json()["data"]
        if detail["status"] == "running":
            break
        await asyncio.sleep(0.15)

    blocked = await client.delete(f"/api/datasets/{dataset['id']}", headers=headers)
    assert blocked.status_code == 409
    blocked_nb = await client.delete(f"/api/notebooks/{notebook['id']}", headers=headers)
    assert blocked_nb.status_code == 409

    await client.post(f"/api/runs/{run['id']}/cancel", headers=headers)


async def test_search_and_pagination(client, headers):
    for index in range(3):
        await client.post(
            "/api/datasets",
            headers=headers,
            data={"name": f"set-{index}"},
            files=[("files", (f"f{index}.txt", b"x", "text/plain"))],
        )
    page = await client.get("/api/datasets?page=1&size=2", headers=headers)
    data = page.json()["data"]
    assert data["total"] == 3
    assert data["pages"] == 2
    assert len(data["records"]) == 2
    searched = await client.get("/api/datasets?q=set-1", headers=headers)
    assert searched.json()["data"]["total"] == 1


async def test_activity_is_recorded(client, headers):
    await client.post(
        "/api/datasets",
        headers=headers,
        data={"name": "audit"},
        files=[("files", ("a.txt", b"x", "text/plain"))],
    )
    activity = await client.get("/api/activity", headers=headers)
    actions = [item["action"] for item in activity.json()["data"]["records"]]
    assert "dataset.upload" in actions


async def test_shared_dataset_visible_to_other_customer(client, db, headers):
    dataset = await upload_dataset(
        client, headers, name="shared data", content=b"shared-data", shared=True
    )
    assert dataset["shared"] is True
    assert dataset["isOwner"] is True

    db.create_customer("Other Co", None, "trh_other_share_token_abcdef")
    other = {"Authorization": "Bearer trh_other_share_token_abcdef"}

    listing = (await client.get("/api/datasets", headers=other)).json()["data"]
    record = next(item for item in listing["records"] if item["id"] == dataset["id"])
    assert record["shared"] is True
    assert record["ownerName"] == "Test Co"
    assert record["isOwner"] is False

    detail = await client.get(f"/api/datasets/{dataset['id']}", headers=other)
    assert detail.status_code == 200
    assert detail.json()["data"]["ownerName"] == "Test Co"

    download = await client.get(f"/api/datasets/{dataset['id']}/download", headers=other)
    assert download.status_code == 200
    assert download.content == b"shared-data"

    # 非所有者不能取消共享，也不能删除
    assert (
        await client.patch(
            f"/api/datasets/{dataset['id']}/sharing",
            headers=other,
            json={"shared": False},
        )
    ).status_code == 404
    assert (await client.delete(f"/api/datasets/{dataset['id']}", headers=other)).status_code == 404


async def test_shared_dataset_usable_by_other_customer_run(client, db, headers):
    dataset = await upload_dataset(
        client, headers, name="shared for run", content=b"shared-run-data", shared=True
    )
    db.create_customer("Run Co", None, "trh_other_run_share_token_abcdef")
    other = {"Authorization": "Bearer trh_other_run_share_token_abcdef"}

    notebook = (
        await client.post(
            "/api/notebooks",
            headers=other,
            data={"name": "use shared"},
            files=[
                (
                    "file",
                    (
                        "shared.ipynb",
                        notebook_bytes(
                            "import os\n"
                            'print(open(os.path.join(os.environ["WMG_DATASET_DIR"], "a.txt")).read())'
                        ),
                        "application/json",
                    ),
                )
            ],
        )
    ).json()["data"]
    run = (
        await client.post(
            "/api/runs",
            headers=other,
            json={"notebookId": notebook["id"], "datasetId": dataset["id"]},
        )
    ).json()["data"]
    assert run["datasetOwnerName"] == "Test Co"
    assert run["datasetShared"] is True

    from helpers import wait_for_status

    final = await wait_for_status(client, other, run["id"], ("succeeded", "failed"))
    assert final["status"] == "succeeded", final.get("message")
    logs = (await client.get(f"/api/runs/{run['id']}/logs", headers=other)).json()["data"]
    assert "shared-run-data" in logs["content"]

    # 所有者取消共享后，其他客户不再能看到，也不能再基于它提交训练
    unshared = await client.patch(
        f"/api/datasets/{dataset['id']}/sharing",
        headers=headers,
        json={"shared": False},
    )
    assert unshared.status_code == 200
    assert unshared.json()["data"]["shared"] is False

    listing = (await client.get("/api/datasets", headers=other)).json()["data"]
    assert all(item["id"] != dataset["id"] for item in listing["records"])
    blocked = await client.post(
        "/api/runs",
        headers=other,
        json={"notebookId": notebook["id"], "datasetId": dataset["id"]},
    )
    assert blocked.status_code == 404

    activity = (await client.get("/api/activity", headers=headers)).json()["data"]["records"]
    actions = [item["action"] for item in activity]
    assert "dataset.share" in actions
    assert "dataset.unshare" in actions


async def test_overview_counts_only_own_datasets(client, db, headers):
    await upload_dataset(client, headers, name="shared stat", shared=True)
    db.create_customer("Stat Co", None, "trh_other_stat_token_abcdef")
    other = {"Authorization": "Bearer trh_other_stat_token_abcdef"}

    assert (await client.get("/api/datasets", headers=other)).json()["data"]["total"] == 1
    overview = (await client.get("/api/overview", headers=other)).json()["data"]
    assert overview["stats"]["datasets"] == 0

    owner_overview = (await client.get("/api/overview", headers=headers)).json()["data"]
    assert owner_overview["stats"]["datasets"] == 1


async def test_delete_shared_dataset_blocked_by_other_active_run(client, db, headers):
    dataset = await upload_dataset(client, headers, name="shared busy", shared=True)
    db.create_customer("Busy Co", None, "trh_other_busy_token_abcdef")
    other = {"Authorization": "Bearer trh_other_busy_token_abcdef"}
    notebook = (
        await client.post(
            "/api/notebooks",
            headers=other,
            data={"name": "busy"},
            files=[
                (
                    "file",
                    (
                        "busy.ipynb",
                        notebook_bytes("import time\ntime.sleep(30)"),
                        "application/json",
                    ),
                )
            ],
        )
    ).json()["data"]
    run = (
        await client.post(
            "/api/runs",
            headers=other,
            json={"notebookId": notebook["id"], "datasetId": dataset["id"]},
        )
    ).json()["data"]

    from helpers import wait_for_status

    await wait_for_status(client, other, run["id"], ("running",), timeout=30)
    blocked = await client.delete(f"/api/datasets/{dataset['id']}", headers=headers)
    assert blocked.status_code == 409
    await client.post(f"/api/runs/{run['id']}/cancel", headers=other)
    await wait_for_status(client, other, run["id"], ("cancelled", "failed"), timeout=45)


async def test_shared_multi_file_dataset_download_uses_owner_storage(client, db, headers):
    response = await client.post(
        "/api/datasets",
        headers=headers,
        data={"name": "shared multi", "shared": "true"},
        files=[
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbb", "text/plain")),
        ],
    )
    dataset = response.json()["data"]
    db.create_customer("Zip Co", None, "trh_other_zip_token_abcdef")
    other = {"Authorization": "Bearer trh_other_zip_token_abcdef"}

    download = await client.get(f"/api/datasets/{dataset['id']}/download", headers=other)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert set(archive.namelist()) == {"a.txt", "b.txt"}
        assert archive.read("a.txt") == b"aaa"
        assert archive.read("b.txt") == b"bbb"
