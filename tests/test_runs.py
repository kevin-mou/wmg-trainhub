from __future__ import annotations

import io
import json
import zipfile

from helpers import upload_dataset, upload_notebook, wait_for_status


async def _create_run(client, headers, notebook_id, **overrides):
    payload = {"notebookId": notebook_id, **overrides}
    response = await client.post("/api/runs", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_successful_run_logs_outputs_and_parameters(client, headers):
    dataset = await upload_dataset(client, headers, name="toy data", content=b"data-ok")
    notebook = await upload_notebook(
        client,
        headers,
        'import os\nprint("hello-trainhub")\n'
        'print(open(os.path.join(os.environ["WMG_DATASET_DIR"], "a.txt")).read())',
        "import os\n"
        'params = os.environ["WMG_PARAMS"]\n'
        'print("params", params)\n'
        'open(os.path.join(os.environ["WMG_OUTPUT_DIR"], "result.json"), "w").write(params)',
        name="train demo",
    )
    run = await _create_run(
        client,
        headers,
        notebook["id"],
        datasetId=dataset["id"],
        name="first run",
        parameters={"epochs": 2, "lr": 0.01},
    )
    assert run["status"] == "queued"
    assert run["datasetName"] == "toy data"

    final = await wait_for_status(client, headers, run["id"], ("succeeded", "failed", "cancelled"))
    assert final["status"] == "succeeded", final.get("message")
    assert final["exitCode"] == 0
    assert final["durationSeconds"] is not None
    assert final["parameters"] == {"epochs": 2, "lr": 0.01}

    logs = (await client.get(f"/api/runs/{run['id']}/logs", headers=headers)).json()["data"]
    assert "hello-trainhub" in logs["content"]
    assert "data-ok" in logs["content"]
    assert "epochs" in logs["content"]
    assert logs["completed"] is True
    assert logs["nextOffset"] == logs["totalBytes"]

    tail = (
        await client.get(f"/api/runs/{run['id']}/logs?tail=true&limit=160", headers=headers)
    ).json()["data"]
    assert tail["nextOffset"] == logs["totalBytes"]
    assert logs["content"].endswith(tail["content"].lstrip("�"))

    log_download = await client.get(f"/api/runs/{run['id']}/logs/download", headers=headers)
    assert log_download.status_code == 200
    assert "hello-trainhub" in log_download.text
    assert ".log" in log_download.headers.get("content-disposition", "")

    outputs = (await client.get(f"/api/runs/{run['id']}/outputs", headers=headers)).json()["data"]
    paths = [item["path"] for item in outputs["files"]]
    assert "result.json" in paths
    assert "executed_notebook.ipynb" in paths

    single = await client.get(
        f"/api/runs/{run['id']}/outputs/file?path=result.json", headers=headers
    )
    assert single.status_code == 200
    assert json.loads(single.content) == {"epochs": 2, "lr": 0.01}

    archive = await client.get(f"/api/runs/{run['id']}/outputs/download", headers=headers)
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        assert "result.json" in zf.namelist()

    traversal = await client.get(
        f"/api/runs/{run['id']}/outputs/file",
        headers=headers,
        params={"path": "../../../../etc/passwd"},
    )
    assert traversal.status_code == 400

    activity = (await client.get("/api/activity", headers=headers)).json()["data"]["records"]
    assert any(item["action"] == "run.create" for item in activity)

    deleted = await client.delete(f"/api/runs/{run['id']}", headers=headers)
    assert deleted.status_code == 200
    assert (await client.get(f"/api/runs/{run['id']}", headers=headers)).status_code == 404


async def test_failed_run_exposes_error_and_logs(client, headers):
    notebook = await upload_notebook(
        client,
        headers,
        'print("before crash")\n1 / 0',
        name="crash",
    )
    run = await _create_run(client, headers, notebook["id"])
    final = await wait_for_status(client, headers, run["id"], ("succeeded", "failed", "cancelled"))
    assert final["status"] == "failed"
    assert final["exitCode"] == 1
    assert final["message"]
    assert "ZeroDivisionError" in final["message"]

    logs = (await client.get(f"/api/runs/{run['id']}/logs", headers=headers)).json()["data"]
    assert "before crash" in logs["content"]
    assert "ZeroDivisionError" in logs["content"]

    outputs = (await client.get(f"/api/runs/{run['id']}/outputs", headers=headers)).json()["data"]
    assert any(item["path"] == "executed_notebook.ipynb" for item in outputs["files"])


async def test_cancel_running_run(client, headers):
    notebook = await upload_notebook(
        client,
        headers,
        'import time\nprint("sleeping")\ntime.sleep(60)',
        name="sleepy",
    )
    run = await _create_run(client, headers, notebook["id"])
    await wait_for_status(client, headers, run["id"], ("running",), timeout=30)

    import asyncio

    deadline = 30
    for _ in range(deadline * 5):
        logs = (await client.get(f"/api/runs/{run['id']}/logs", headers=headers)).json()["data"]
        if "sleeping" in logs["content"]:
            break
        await asyncio.sleep(0.2)
    else:
        raise AssertionError("kernel output did not stream into the log before cancel")

    response = await client.post(f"/api/runs/{run['id']}/cancel", headers=headers)
    assert response.status_code == 200

    final = await wait_for_status(client, headers, run["id"], ("cancelled", "failed"), timeout=45)
    assert final["status"] == "cancelled"
    assert final["message"] == "训练已取消"

    logs = (await client.get(f"/api/runs/{run['id']}/logs", headers=headers)).json()["data"]
    assert "sleeping" in logs["content"]
    assert "训练已取消" in logs["content"]


async def test_run_timeout(client, headers):
    notebook = await upload_notebook(
        client,
        headers,
        'import time\nprint("start")\ntime.sleep(30)',
        name="slow",
    )
    run = await _create_run(client, headers, notebook["id"], timeoutSeconds=1)
    final = await wait_for_status(client, headers, run["id"], ("succeeded", "failed"), timeout=60)
    assert final["status"] == "failed"
    assert "超时" in (final["message"] or "")


async def test_unknown_kernel_rejected(client, headers):
    notebook = await upload_notebook(client, headers, "print('x')")
    response = await client.post(
        "/api/runs",
        headers=headers,
        json={"notebookId": notebook["id"], "kernel": "no-such-kernel"},
    )
    assert response.status_code == 400
    assert "内核不可用" in response.json()["message"]


async def test_run_isolation(client, db, headers):
    notebook = await upload_notebook(client, headers, "print('secret')")
    run = await _create_run(client, headers, notebook["id"])
    await wait_for_status(client, headers, run["id"], ("succeeded", "failed"))

    db.create_customer("Other Co", None, "trh_other_run_token_abcdef")
    other = {"Authorization": "Bearer trh_other_run_token_abcdef"}
    assert (await client.get(f"/api/runs/{run['id']}", headers=other)).status_code == 404
    assert (await client.get(f"/api/runs/{run['id']}/logs", headers=other)).status_code == 404
    assert (await client.get(f"/api/runs/{run['id']}/outputs", headers=other)).status_code == 404
    assert (await client.get("/api/runs", headers=other)).json()["data"]["total"] == 0
    assert (await client.post(f"/api/runs/{run['id']}/cancel", headers=other)).status_code == 404


async def test_run_requires_owned_notebook_and_dataset(client, db, headers):
    notebook = await upload_notebook(client, headers, "print('x')")
    db.create_customer("Other Co", None, "trh_other_ownership_token_abcdef")
    other = {"Authorization": "Bearer trh_other_ownership_token_abcdef"}

    missing = await client.post("/api/runs", headers=headers, json={"notebookId": "nb_missing"})
    assert missing.status_code == 404

    foreign = await client.post("/api/runs", headers=other, json={"notebookId": notebook["id"]})
    assert foreign.status_code == 404


async def test_admin_cannot_delete_customer_with_active_run(client, headers, customer):
    notebook = await upload_notebook(
        client, headers, 'import time\nprint("busy")\ntime.sleep(30)', name="busy"
    )
    run = await _create_run(client, headers, notebook["id"])
    await wait_for_status(client, headers, run["id"], ("running",), timeout=30)

    response = await client.delete(
        f"/api/admin/customers/{customer['id']}",
        headers={"X-Admin-Token": "admin-test-token"},
    )
    assert response.status_code == 409
    assert "训练任务" in response.json()["message"]

    await client.post(f"/api/runs/{run['id']}/cancel", headers=headers)


async def test_shipped_example_notebook_runs(client, headers):
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "examples" / "hello_trainhub.ipynb"
    uploaded = await client.post(
        "/api/notebooks",
        headers=headers,
        data={"name": "hello example"},
        files=[("file", ("hello_trainhub.ipynb", example.read_bytes(), "application/json"))],
    )
    assert uploaded.status_code == 200
    notebook = uploaded.json()["data"]
    dataset = await upload_dataset(client, headers, name="example data", content=b"sample")

    run = await _create_run(
        client,
        headers,
        notebook["id"],
        datasetId=dataset["id"],
        parameters={"epochs": 2},
    )
    final = await wait_for_status(client, headers, run["id"], ("succeeded", "failed"))
    assert final["status"] == "succeeded", final.get("message")

    logs = (await client.get(f"/api/runs/{run['id']}/logs", headers=headers)).json()["data"]
    assert "epoch 1/2" in logs["content"]
    assert "dataset files:" in logs["content"]

    artifact = await client.get(
        f"/api/runs/{run['id']}/outputs/file?path=result.json", headers=headers
    )
    assert artifact.status_code == 200
    payload = json.loads(artifact.content)
    assert payload["epochs"] == 2
    assert payload["datasetFiles"] == ["a.txt"]
