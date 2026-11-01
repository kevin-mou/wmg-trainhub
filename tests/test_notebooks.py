from __future__ import annotations

import json
from pathlib import Path

from helpers import upload_notebook


async def test_rejects_non_notebook_file(client, headers):
    response = await client.post(
        "/api/notebooks/import",
        headers=headers,
        data={"name": "bad"},
        files={"file": ("train.py", b"print(1)", "text/x-python")},
    )
    assert response.status_code == 400
    assert ".ipynb" in response.json()["message"]


async def test_rejects_invalid_notebook_json(client, headers):
    response = await client.post(
        "/api/notebooks/import",
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

    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    assert content["name"] == "two cells"
    assert [cell["source"] for cell in content["content"]["cells"]] == [
        "print('hi')",
        "print('bye')",
    ]

    download = await client.get(f"/api/notebooks/{notebook['id']}/download", headers=headers)
    assert download.status_code == 200
    downloaded = json.loads(download.content)
    assert [_cell_source(cell) for cell in downloaded["cells"]] == [
        "print('hi')",
        "print('bye')",
    ]
    assert all(cell.get("outputs") == [] for cell in downloaded["cells"])
    assert "train.ipynb" in download.headers.get("content-disposition", "")


async def test_import_migrates_outputs_into_session_snapshot(client, db, customer, headers):
    payload = {
        "cells": [
            {
                "cell_type": "code",
                "id": "cell-import-output",
                "metadata": {},
                "execution_count": 2,
                "source": "print('hello')",
                "outputs": [{"output_type": "stream", "name": "stdout", "text": "hello\n"}],
            }
        ],
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    response = await client.post(
        "/api/notebooks/import",
        headers=headers,
        files={"file": ("with-output.ipynb", json.dumps(payload).encode(), "application/json")},
    )
    assert response.status_code == 200, response.text
    notebook = response.json()["data"]

    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    assert content["content"]["cells"][0]["outputs"] == []
    assert content["content"]["cells"][0]["execution_count"] is None

    session = (
        await client.get(f"/api/notebooks/{notebook['id']}/session", headers=headers)
    ).json()["data"]
    assert session["origin"] == "import"
    assert session["cells"][0]["cellId"] == "cell-import-output"
    assert session["cells"][0]["matchesScript"] is True
    assert session["cells"][0]["executionCount"] == 2
    assert session["cells"][0]["outputs"][0]["text"] == "hello\n"

    row = db.get_notebook(customer["id"], notebook["id"])
    stored = json.loads(Path(row["storage_path"]).read_text(encoding="utf-8"))
    assert stored["cells"][0]["outputs"] == []
    assert stored["cells"][0]["execution_count"] is None

    download = await client.get(f"/api/notebooks/{notebook['id']}/download", headers=headers)
    assert download.status_code == 200, download.text
    downloaded = json.loads(download.content)
    assert downloaded["cells"][0]["execution_count"] == 2
    assert _output_text(downloaded["cells"][0]["outputs"][0]) == "hello\n"


async def test_legacy_notebook_outputs_are_migrated_on_first_read(client, db, customer, headers):
    created = await client.post(
        "/api/notebooks",
        headers=headers,
        json={"name": "legacy", "content": _manual_content("print('legacy')")},
    )
    notebook = created.json()["data"]
    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    cell_id = content["content"]["cells"][0]["id"]

    row = db.get_notebook(customer["id"], notebook["id"])
    path = Path(row["storage_path"])
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["cells"][0]["id"] = cell_id
    legacy["cells"][0]["execution_count"] = 7
    legacy["cells"][0]["outputs"] = [
        {"output_type": "stream", "name": "stdout", "text": "legacy-output\n"}
    ]
    path.write_text(json.dumps(legacy), encoding="utf-8")

    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    assert content["content"]["cells"][0]["outputs"] == []
    assert content["content"]["cells"][0]["execution_count"] is None

    session = (
        await client.get(f"/api/notebooks/{notebook['id']}/session", headers=headers)
    ).json()["data"]
    assert session["origin"] == "legacy"
    assert session["cells"][0]["cellId"] == cell_id
    assert session["cells"][0]["executionCount"] == 7
    assert _output_text(session["cells"][0]["outputs"][0]) == "legacy-output\n"

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["cells"][0]["outputs"] == []
    assert stored["cells"][0]["execution_count"] is None


async def test_save_session_is_separate_and_download_merges_matching_outputs(
    client, db, customer, headers
):
    created = await client.post(
        "/api/notebooks",
        headers=headers,
        json={"name": "session", "content": _manual_content("print('one')")},
    )
    notebook = created.json()["data"]
    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    cell = content["content"]["cells"][0]
    cell_id = cell["id"]
    saved = await client.put(
        f"/api/notebooks/{notebook['id']}/session",
        headers=headers,
        json={
            "cells": [
                {
                    "cellId": cell_id,
                    "source": "print('one')",
                    "executionCount": 1,
                    "outputs": [{"output_type": "stream", "name": "stdout", "text": "one\n"}],
                }
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["origin"] == "session"
    assert saved.json()["data"]["cells"][0]["matchesScript"] is True

    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    assert content["content"]["cells"][0]["outputs"] == []

    row = db.get_notebook(customer["id"], notebook["id"])
    stored = json.loads(Path(row["storage_path"]).read_text(encoding="utf-8"))
    assert stored["cells"][0]["outputs"] == []

    download = await client.get(f"/api/notebooks/{notebook['id']}/download", headers=headers)
    assert download.status_code == 200, download.text
    downloaded = json.loads(download.content)
    assert _output_text(downloaded["cells"][0]["outputs"][0]) == "one\n"

    saved_script = await client.put(
        f"/api/notebooks/{notebook['id']}",
        headers=headers,
        json={"name": "session", "content": content["content"]},
    )
    assert saved_script.status_code == 200, saved_script.text
    download = await client.get(f"/api/notebooks/{notebook['id']}/download", headers=headers)
    assert download.status_code == 200, download.text
    assert _output_text(json.loads(download.content)["cells"][0]["outputs"][0]) == "one\n"

    changed = content["content"]
    changed["cells"][0]["source"] = "print('two')"
    updated = await client.put(
        f"/api/notebooks/{notebook['id']}",
        headers=headers,
        json={"name": "session", "content": changed},
    )
    assert updated.status_code == 200, updated.text

    session = (
        await client.get(f"/api/notebooks/{notebook['id']}/session", headers=headers)
    ).json()["data"]
    assert session["cells"][0]["matchesScript"] is False
    download = await client.get(f"/api/notebooks/{notebook['id']}/download", headers=headers)
    downloaded = json.loads(download.content)
    assert downloaded["cells"][0]["outputs"] == []

    mismatch = await client.put(
        f"/api/notebooks/{notebook['id']}/session",
        headers=headers,
        json={
            "cells": [
                {
                    "cellId": cell_id,
                    "source": "print('one')",
                    "executionCount": 1,
                    "outputs": [],
                }
            ]
        },
    )
    assert mismatch.status_code == 409
    assert "未保存" in mismatch.json()["message"]


async def test_notebook_isolation(client, db, headers):
    notebook = await upload_notebook(client, headers, "print('private')")
    db.create_customer("Other Co", None, "trh_other_notebook_token_abcdef")
    other = {"Authorization": "Bearer trh_other_notebook_token_abcdef"}
    assert (await client.get("/api/notebooks", headers=other)).json()["data"]["records"] == []
    assert (await client.get(f"/api/notebooks/{notebook['id']}", headers=other)).status_code == 404
    assert (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=other)
    ).status_code == 404
    assert (
        await client.get(f"/api/notebooks/{notebook['id']}/session", headers=other)
    ).status_code == 404
    assert (
        await client.put(
            f"/api/notebooks/{notebook['id']}/session",
            headers=other,
            json={"cells": []},
        )
    ).status_code == 404
    assert (
        await client.put(
            f"/api/notebooks/{notebook['id']}",
            headers=other,
            json={"name": "other", "content": _manual_content()},
        )
    ).status_code == 404
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


async def test_last_run_returns_latest_parameters_and_is_scoped(client, db, customer, headers):
    notebook = await upload_notebook(client, headers, "print('params')", name="prefill")
    empty = await client.get(f"/api/notebooks/{notebook['id']}/last-run", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["data"] is None

    notebook_row = db.get_notebook(customer["id"], notebook["id"])
    assert notebook_row is not None
    first = db.create_run(
        customer_id=customer["id"],
        notebook=notebook_row,
        dataset=None,
        name="first",
        kernel=None,
        parameters_json=json.dumps({"epochs": 1, "lr": 0.01}),
        timeout_seconds=0,
    )
    second = db.create_run(
        customer_id=customer["id"],
        notebook=notebook_row,
        dataset=None,
        name="second",
        kernel=None,
        parameters_json=json.dumps({"epochs": 2, "lr": 0.02}),
        timeout_seconds=0,
    )

    last = await client.get(f"/api/notebooks/{notebook['id']}/last-run", headers=headers)
    assert last.status_code == 200
    payload = last.json()["data"]
    assert payload["id"] == second["id"]
    assert payload["id"] != first["id"]
    assert payload["parameters"] == {"epochs": 2, "lr": 0.02}

    db.create_customer("Other Co", None, "trh_other_prefill_token_abcdef")
    other = {"Authorization": "Bearer trh_other_prefill_token_abcdef"}
    denied = await client.get(f"/api/notebooks/{notebook['id']}/last-run", headers=other)
    assert denied.status_code == 404


def _cell_source(cell: dict) -> str:
    source = cell.get("source")
    return "".join(source) if isinstance(source, list) else str(source or "")


def _output_text(output: dict) -> str:
    text = output.get("text")
    return "".join(text) if isinstance(text, list) else str(text or "")


def _manual_content(source: str = "print('manual')") -> dict:
    return {
        "cells": [
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": source,
            }
        ],
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


async def test_manual_notebook_create_edit_and_download(client, headers):
    created = await client.post(
        "/api/notebooks",
        headers=headers,
        json={"name": "manual", "content": _manual_content()},
    )
    assert created.status_code == 200, created.text
    assert created.json()["code"] == 201
    notebook = created.json()["data"]
    assert notebook["name"] == "manual"
    assert notebook["filename"] == "manual.ipynb"
    assert notebook["cellCount"] == 1

    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    assert content["name"] == "manual"
    assert content["content"]["cells"][0]["source"] == "print('manual')"

    updated_payload = _manual_content("print('updated')")
    updated_payload["cells"][0]["outputs"] = [
        {"output_type": "stream", "name": "stdout", "text": "last-output"}
    ]
    updated_payload["cells"].append(
        {"cell_type": "markdown", "metadata": {}, "source": "# heading"}
    )
    updated = await client.put(
        f"/api/notebooks/{notebook['id']}",
        headers=headers,
        json={"name": "renamed", "content": updated_payload},
    )
    assert updated.status_code == 200, updated.text
    saved = updated.json()["data"]
    assert saved["name"] == "renamed"
    assert saved["filename"] == "renamed.ipynb"
    assert saved["cellCount"] == 1

    content = (
        await client.get(f"/api/notebooks/{notebook['id']}/content", headers=headers)
    ).json()["data"]
    assert len(content["content"]["cells"]) == 2
    assert content["content"]["cells"][0]["source"] == "print('updated')"
    assert content["content"]["cells"][1]["cell_type"] == "markdown"
    assert content["content"]["cells"][0]["outputs"] == []
    assert content["content"]["cells"][0]["execution_count"] is None
    session = (
        await client.get(f"/api/notebooks/{notebook['id']}/session", headers=headers)
    ).json()["data"]
    assert session["cells"] == []

    download = await client.get(f"/api/notebooks/{notebook['id']}/download", headers=headers)
    assert download.status_code == 200
    downloaded = json.loads(download.content)
    assert len(downloaded["cells"]) == 2
    assert _cell_source(downloaded["cells"][0]) == "print('updated')"
    assert downloaded["cells"][0]["outputs"] == []

    activity = (await client.get("/api/activity", headers=headers)).json()["data"]["records"]
    actions = [item["action"] for item in activity]
    assert "notebook.create" in actions
    assert "notebook.update" in actions


async def test_manual_notebook_requires_cells(client, headers):
    response = await client.post(
        "/api/notebooks",
        headers=headers,
        json={
            "name": "empty",
            "content": {
                "cells": [],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            },
        },
    )
    assert response.status_code == 400
    assert "至少" in response.json()["message"]


async def test_edit_notebook_blocked_while_run_active(client, db, customer, headers):
    created = await client.post(
        "/api/notebooks",
        headers=headers,
        json={"name": "busy edit", "content": _manual_content()},
    )
    notebook = created.json()["data"]
    row = db.get_notebook(customer["id"], notebook["id"])
    assert row is not None
    db.create_run(
        customer_id=customer["id"],
        notebook=row,
        dataset=None,
        name="active",
        kernel=None,
        parameters_json=None,
        timeout_seconds=0,
    )
    response = await client.put(
        f"/api/notebooks/{notebook['id']}",
        headers=headers,
        json={"name": "busy edit", "content": _manual_content("print('next')")},
    )
    assert response.status_code == 409
    assert "无法编辑" in response.json()["message"]


async def test_execute_cell_blocked_while_node_busy(client, db, customer, headers):
    notebook = await upload_notebook(client, headers, "print('placeholder')", name="busy execute")
    started = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions",
        headers=headers,
        json={"kernel": "python3"},
    )
    assert started.status_code == 200, started.text
    session = started.json()["data"]

    row = db.get_notebook(customer["id"], notebook["id"])
    run = db.create_run(
        customer_id=customer["id"],
        notebook=row,
        dataset=None,
        name="占用中的训练",
        kernel=None,
        parameters_json=None,
        timeout_seconds=0,
    )
    blocked = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
        headers=headers,
        json={"code": "print('should-not-run')", "cellId": "c1"},
    )
    assert blocked.status_code == 409, blocked.text
    message = blocked.json()["message"]
    assert "占用" in message and "占用中的训练" in message

    # 任务结束后自动恢复
    assert db.cancel_queued_run(run["id"])
    ok = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
        headers=headers,
        json={"code": "print('resumed')", "cellId": "c1"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["status"] == "ok"

    await client.delete(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}", headers=headers
    )


async def test_notebook_kernel_session_executes_cells_and_keeps_state(client, headers):
    notebook = await upload_notebook(client, headers, "print('placeholder')", name="session")

    started = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions",
        headers=headers,
        json={"kernel": "python3"},
    )
    assert started.status_code == 200, started.text
    session = started.json()["data"]
    assert session["sessionId"].startswith("ks_")
    assert session["kernel"] == "python3"

    reused = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions",
        headers=headers,
        json={"kernel": "python3"},
    )
    assert reused.status_code == 200
    assert reused.json()["data"]["sessionId"] == session["sessionId"]

    first = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
        headers=headers,
        json={"code": "x = 41\nprint('hello-cell')", "cellId": "c1"},
    )
    assert first.status_code == 200, first.text
    result = first.json()["data"]
    assert result["status"] == "ok"
    assert result["executionCount"] == 1
    stream_text = "".join(
        item.get("text", "") for item in result["outputs"] if item.get("output_type") == "stream"
    )
    assert "hello-cell" in stream_text

    second = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
        headers=headers,
        json={"code": "print(x + 1)", "cellId": "c2"},
    )
    assert second.status_code == 200, second.text
    result = second.json()["data"]
    stream_text = "".join(
        item.get("text", "") for item in result["outputs"] if item.get("output_type") == "stream"
    )
    assert "42" in stream_text

    error = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
        headers=headers,
        json={"code": "1 / 0", "cellId": "c3"},
    )
    assert error.status_code == 200, error.text
    result = error.json()["data"]
    assert result["status"] == "error"
    assert any(
        item.get("output_type") == "error" and item.get("ename") == "ZeroDivisionError"
        for item in result["outputs"]
    )

    closed = await client.delete(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}",
        headers=headers,
    )
    assert closed.status_code == 200

    missing = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
        headers=headers,
        json={"code": "print('gone')"},
    )
    assert missing.status_code == 404
