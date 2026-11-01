from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile

from helpers import notebook_bytes

ADMIN = {"X-Admin-Token": "admin-test-token"}


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _code_bundle(notebook_source: str, extra_files: dict[str, bytes] | None = None) -> bytes:
    files = {"train.ipynb": notebook_bytes(notebook_source)}
    files.update(extra_files or {})
    return _zip(files)


async def _wait_admin(
    client, run_id: str, statuses=("succeeded", "failed", "cancelled"), timeout=90.0
):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = await client.get(f"/api/admin/runs/{run_id}", headers=ADMIN)
        assert response.status_code == 200, response.text
        last = response.json()["data"]
        if last["status"] in statuses:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"run {run_id} did not reach {statuses}; last={last}")


async def _submit(
    client, code: bytes, dataset: bytes | None = None, assets: bytes | None = None, **fields
):
    data = {
        "name": fields.pop("name", "platform run"),
        "entry": fields.pop("entry", "train.ipynb"),
        "parameters": json.dumps(fields.pop("parameters", {"epochs": 1})),
        "timeoutSeconds": str(fields.pop("timeoutSeconds", 60)),
        "baseModel": fields.pop("baseModel", "Qwen3.5-0.8B"),
        "controlJobId": fields.pop("controlJobId", "job-test-1"),
        **{key: str(value) for key, value in fields.items()},
    }
    files = {"code": ("code.zip", code, "application/zip")}
    if dataset is not None:
        files["dataset"] = ("dataset.zip", dataset, "application/zip")
    if assets is not None:
        files["assets"] = ("assets.zip", assets, "application/zip")
    response = await client.post("/api/admin/executions", headers=ADMIN, data=data, files=files)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_platform_execution_passes_dataset_name_to_kernel(client):
    source = "import os\nprint('DSNAME=' + os.environ.get('WMG_DATASET_NAME', ''))\n"
    run = await _submit(client, _code_bundle(source), datasetName="demo", datasetVersion=3)
    assert run["datasetName"] == "demo"
    final = await _wait_admin(client, run["id"])
    assert final["status"] == "succeeded", final.get("message")
    logs = await client.get(f"/api/admin/runs/{run['id']}/logs", headers=ADMIN)
    assert "DSNAME=demo" in logs.text


async def test_platform_execution_bundle_runs_and_reports(client):
    source = (
        "import os\n"
        "import helper\n"
        "print('hello-platform', helper.VALUE)\n"
        "dataset = os.environ['WMG_DATASET_DIR']\n"
        "print('dataset', open(os.path.join(dataset, 'a.txt')).read())\n"
        "print('code-sha', os.environ['WMG_CODE_SHA256'][:8])\n"
        "open(os.path.join(os.environ['WMG_OUTPUT_DIR'], 'checkpoint.txt'), 'w').write('ckpt')\n"
        'json_text = \'{"modelName": "demo-model", "baseModel": "Qwen3.5-0.8B"}\'\n'
        "open(os.path.join(os.environ['WMG_OUTPUT_DIR'], 'trainhub_result.json'), 'w').write(json_text)\n"
    )
    code = _code_bundle(source, {"helper.py": b"VALUE = 42\n"})
    dataset = _zip({"a.txt": b"data-ok"})
    run = await _submit(client, code, dataset)
    assert run["origin"] == "platform"
    assert run["controlJobId"] == "job-test-1"
    assert run["codeSha256"]
    assert run["datasetSha256"]

    final = await _wait_admin(client, run["id"])
    assert final["status"] == "succeeded", final.get("message")
    logs = await client.get(f"/api/admin/runs/{run['id']}/logs", headers=ADMIN)
    assert logs.status_code == 200
    assert "hello-platform 42" in logs.text
    assert "dataset data-ok" in logs.text

    outputs = await client.get(f"/api/admin/runs/{run['id']}", headers=ADMIN)
    paths = [item["path"] for item in outputs.json()["data"]["outputs"]["files"]]
    assert "checkpoint.txt" in paths
    assert "trainhub_result.json" in paths


async def test_admin_customer_can_use_explicit_token(client):
    token = "trh_explicit_identity_token_0001"
    created = await client.post(
        "/api/admin/customers",
        headers=ADMIN,
        json={"name": "指定令牌客户", "token": token},
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["token"] == token

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["data"]["name"] == "指定令牌客户"

    duplicated = await client.post(
        "/api/admin/customers",
        headers=ADMIN,
        json={"name": "重复令牌", "token": token},
    )
    assert duplicated.status_code == 409, duplicated.text


async def test_platform_execution_attributed_to_identity_customer(client):
    created = await client.post(
        "/api/admin/customers", headers=ADMIN, json={"name": "WMG 平台身份"}
    )
    assert created.status_code == 200, created.text
    customer = created.json()["data"]
    token = customer["token"]
    customer_headers = {"Authorization": f"Bearer {token}"}

    run = await _submit(
        client,
        _code_bundle("print('identity ok')\n"),
        customerId=customer["id"],
    )
    assert run["customerId"] == customer["id"]

    # 平台身份用客户令牌登录控制台（客户 API）应能看到这次通过 API 发起的执行
    listing = await client.get("/api/runs", headers=customer_headers)
    assert listing.status_code == 200, listing.text
    ids = [item["id"] for item in listing.json()["data"]["records"]]
    assert run["id"] in ids
    detail = await client.get(f"/api/runs/{run['id']}", headers=customer_headers)
    assert detail.status_code == 200

    final = await _wait_admin(client, run["id"])
    assert final["status"] == "succeeded", final.get("message")
    logs = await client.get(f"/api/runs/{run['id']}/logs", headers=customer_headers)
    assert "identity ok" in logs.text


async def test_platform_execution_rejects_unknown_identity_customer(client):
    data = {
        "name": "bad identity",
        "entry": "train.ipynb",
        "parameters": "{}",
        "timeoutSeconds": "60",
        "customerId": "c_missing",
    }
    files = {"code": ("code.zip", _code_bundle("print('x')\n"), "application/zip")}
    response = await client.post("/api/admin/executions", headers=ADMIN, data=data, files=files)
    assert response.status_code == 400, response.text
    assert "执行身份客户不存在" in response.text


async def test_executor_status_and_system_customer_hidden(client):
    status = await client.get("/api/admin/executor/status", headers=ADMIN)
    assert status.status_code == 200, status.text
    data = status.json()["data"]
    assert data["maxConcurrentRuns"] >= 1
    assert "queuedRuns" in data and "runningRuns" in data
    assert isinstance(data["gpus"], list)
    assert isinstance(data["cpu"], dict)
    assert int(data["cpu"].get("cores") or 0) >= 1
    assert "disk" in data
    assert isinstance(data["kernels"], list)
    assert all(isinstance(item, str) for item in data["kernels"])

    customers = await client.get("/api/admin/customers", headers=ADMIN)
    assert customers.status_code == 200
    ids = [item["id"] for item in customers.json()["data"]["records"]]
    assert "sys_platform" not in ids


async def test_admin_cancel_running_execution(client):
    source = "import time\nprint('sleeping')\ntime.sleep(30)\nprint('done')\n"
    run = await _submit(client, _code_bundle(source), timeoutSeconds=60)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = await client.get(f"/api/admin/runs/{run['id']}", headers=ADMIN)
        if detail.json()["data"]["status"] == "running":
            break
        await asyncio.sleep(0.2)
    else:
        raise AssertionError("run did not start")

    cancel = await client.post(f"/api/admin/runs/{run['id']}/cancel", headers=ADMIN)
    assert cancel.status_code == 200, cancel.text
    final = await _wait_admin(client, run["id"], timeout=20)
    assert final["status"] == "cancelled"


async def test_platform_execution_assets_mount(client):
    source = (
        "import os\n"
        "model_dir = os.environ['WMG_MODEL_DIR']\n"
        "baseline_dir = os.environ['WMG_BASELINE_MODEL_DIR']\n"
        "print('model', open(os.path.join(model_dir, 'who.txt')).read())\n"
        "print('baseline', open(os.path.join(baseline_dir, 'who.txt')).read())\n"
        "open(os.path.join(os.environ['WMG_OUTPUT_DIR'], 'done.txt'), 'w').write('ok')\n"
    )
    assets = _zip({"model/who.txt": b"candidate", "baseline/who.txt": b"baseline-model"})
    run = await _submit(client, _code_bundle(source), assets=assets)
    assert run["assetsSha256"]
    final = await _wait_admin(client, run["id"])
    assert final["status"] == "succeeded", final.get("message")
    logs = await client.get(f"/api/admin/runs/{run['id']}/logs", headers=ADMIN)
    assert "model candidate" in logs.text
    assert "baseline baseline-model" in logs.text


async def test_admin_sse_log_stream_and_terminal_rejection(client):
    source = "import time\nprint('sse-start')\ntime.sleep(0.5)\nprint('sse-done')\n"
    run = await _submit(client, _code_bundle(source), timeoutSeconds=30)
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "GET", f"/api/admin/runs/{run['id']}/logs/stream", headers=ADMIN
    ) as response:
        assert response.status_code == 200, await response.aread()
        assert "text/event-stream" in response.headers.get("content-type", "")
        event_name = ""
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((event_name, json.loads(line[len("data: ") :])))
    assert any(name == "status" for name, _ in events)
    assert any(name == "log" and "sse-start" in data.get("text", "") for name, data in events)
    assert any(name == "end" and data.get("status") == "succeeded" for name, data in events)

    # 已结束的任务不允许再开日志流
    after = await client.get(f"/api/admin/runs/{run['id']}/logs/stream", headers=ADMIN)
    assert after.status_code == 409, after.text


async def test_single_task_instance_rejects_second_submission(client):
    source = "import time\nprint('busy')\ntime.sleep(30)\n"
    first = await _submit(client, _code_bundle(source), timeoutSeconds=60)
    response = await client.post(
        "/api/admin/executions",
        headers=ADMIN,
        data={"name": "second", "entry": "train.ipynb", "parameters": "{}", "timeoutSeconds": "5"},
        files={"code": ("code.zip", _code_bundle("print('x')\n"), "application/zip")},
    )
    assert response.status_code == 409, response.text
    assert "单任务" in response.text
    await client.post(f"/api/admin/runs/{first['id']}/cancel", headers=ADMIN)


async def test_customer_overview_reports_platform_busy(client, headers):
    """平台任务占用单例执行器时，客户概览必须显示繁忙（不能只看自己的队列）。"""
    run = await _submit(
        client,
        _code_bundle("import time\ntime.sleep(5)\n"),
        name="busy-check",
    )
    try:
        overview = (await client.get("/api/overview", headers=headers)).json()["data"]
        assert overview["stats"]["activeRuns"] == 0
        assert overview["executor"]["busy"] is True
        assert overview["executor"]["platformActiveRuns"] >= 1
    finally:
        await client.post(f"/api/admin/runs/{run['id']}/cancel", headers=ADMIN)
