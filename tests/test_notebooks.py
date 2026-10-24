from __future__ import annotations

from helpers import notebook_bytes, upload_notebook


async def test_rejects_non_notebook_file(client, headers):
    response = await client.post(
        "/api/notebooks",
        headers=headers,
        data={"name": "bad"},
        files={"file": ("train.py", b"print(1)", "text/x-python")},
    )
    assert response.status_code == 400
    assert ".ipynb" in response.json()["message"]


async def test_rejects_invalid_notebook_json(client, headers):
    response = await client.post(
        "/api/notebooks",
        headers=headers,
        data={"name": "bad"},
        files={"file": ("train.ipynb", b"not-a-notebook", "application/json")},
    )
    assert response.status_code == 400
    assert "不合法" in response.json()["message"]


async def test_upload_list_and_download(client, headers):
    notebook = await upload_notebook(
        client, headers, "print('hi')", "print('bye')", name="two cells"
    )
    assert notebook["cellCount"] == 2
    assert notebook["filename"] == "train.ipynb"

    listing = await client.get("/api/notebooks", headers=headers)
    records = listing.json()["data"]["records"]
    assert len(records) == 1
    assert records[0]["name"] == "two cells"

    download = await client.get(f"/api/notebooks/{notebook['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.content == notebook_bytes("print('hi')", "print('bye')")
    assert "train.ipynb" in download.headers.get("content-disposition", "")


async def test_notebook_isolation(client, db, headers):
    notebook = await upload_notebook(client, headers, "print('private')")
    db.create_customer("Other Co", None, "trh_other_notebook_token_abcdef")
    other = {"Authorization": "Bearer trh_other_notebook_token_abcdef"}
    assert (await client.get("/api/notebooks", headers=other)).json()["data"]["records"] == []
    assert (await client.get(f"/api/notebooks/{notebook['id']}", headers=other)).status_code == 404
    assert (
        await client.get(f"/api/notebooks/{notebook['id']}/download", headers=other)
    ).status_code == 404
    assert (
        await client.delete(f"/api/notebooks/{notebook['id']}", headers=other)
    ).status_code == 404


async def test_delete_notebook(client, headers):
    notebook = await upload_notebook(client, headers, "print('x')")
    response = await client.delete(f"/api/notebooks/{notebook['id']}", headers=headers)
    assert response.status_code == 200
    assert (await client.get("/api/notebooks", headers=headers)).json()["data"]["total"] == 0
