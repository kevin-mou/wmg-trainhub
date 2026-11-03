from __future__ import annotations

import json

from helpers import notebook_bytes, upload_dataset


async def _create_notebook(client, headers) -> dict:
    content = json.loads(notebook_bytes("print(1)").decode("utf-8"))
    response = await client.post(
        "/api/notebooks", headers=headers, json={"name": "Dataset Notebook", "content": content}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_session_runtime_dataset_helper_resolves_own_and_shared(
    client, app, db, customer, headers
):
    own = await upload_dataset(client, headers, name="own-runtime")
    other = db.create_customer("Other Co", None, "trh_other_runtime_token_1234567890")
    other_headers = {"Authorization": "Bearer trh_other_runtime_token_1234567890"}
    shared = await upload_dataset(
        client,
        other_headers,
        name="shared-runtime",
        content=b"shared-runtime-ok",
        shared=True,
    )
    notebook = await _create_notebook(client, headers)

    started = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions",
        headers=headers,
        json={"kernel": "python3"},
    )
    assert started.status_code == 200, started.text
    session = started.json()["data"]
    shared_ref = f"{other['id']}/shared-runtime"
    code = (
        "import os\n"
        "from trainhub_runtime import dataset_path, list_datasets\n"
        "print('OWN=' + dataset_path('own-runtime'))\n"
        f"print('SHARED=' + dataset_path('{shared_ref}'))\n"
        "print('SHARED_DATA=' + open(os.path.join("
        f"dataset_path('{shared_ref}'), 'a.txt')).read())\n"
        "print('NAMES=' + ','.join(sorted(item['name'] for item in list_datasets())))\n"
        "try:\n"
        "    dataset_path()\n"
        "except Exception as exc:\n"
        "    print('NO_SELECTED=' + ('没有选中' in str(exc) and 'OK' or str(exc)))\n"
    )
    try:
        executed = await client.post(
            f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
            headers=headers,
            json={"code": code, "cellId": "c1"},
        )
        assert executed.status_code == 200, executed.text
        result = executed.json()["data"]
        assert result["status"] == "ok", result.get("message")
        text = "".join(
            item.get("text", "")
            for item in result["outputs"]
            if item.get("output_type") == "stream"
        )
    finally:
        await client.delete(
            f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}",
            headers=headers,
        )

    storage = app.state.storage
    own_dir = storage.dataset_files_dir(customer["id"], own["id"])
    shared_dir = storage.dataset_files_dir(other["id"], shared["id"])
    assert f"OWN={own_dir}" in text
    assert f"SHARED={shared_dir}" in text
    assert "SHARED_DATA=shared-runtime-ok" in text
    assert "NAMES=own-runtime,shared-runtime" in text
    assert "NO_SELECTED=OK" in text


async def test_session_load_dataset_reads_shared_records(client, app, db, customer, headers):
    other = db.create_customer("Data Co", None, "trh_other_load_token_1234567890")
    other_headers = {"Authorization": "Bearer trh_other_load_token_1234567890"}
    manifest = {
        "name": "wmg-shared-records",
        "artifacts": {
            "train.jsonl": {"records": 2},
            "test.jsonl": {"records": 1},
        },
    }
    train_rows = (
        '{"fields": {"subject": "a"}, "answers": {"email_type": "Ham", "motivation": "Reply"}}\n'
        '{"fields": {"subject": "b"}, "answers": {"email_type": "Spam", "motivation": "Unknown"}}\n'
    )
    test_rows = '{"fields": {"subject": "c"}, "answers": {"email_type": "Phishing", "motivation": "Open attachment"}}\n'
    uploaded = await client.post(
        "/api/datasets",
        headers=other_headers,
        data={"name": "shared-records", "shared": "true"},
        files=[
            (
                "files",
                (
                    "dataset_manifest.json",
                    json.dumps(manifest).encode("utf-8"),
                    "application/json",
                ),
            ),
            (
                "files",
                (
                    "groups.json",
                    json.dumps({"train": ["g1", "g2"], "test": ["g3"]}).encode("utf-8"),
                    "application/json",
                ),
            ),
            (
                "files",
                ("train.jsonl", train_rows.encode("utf-8"), "application/x-ndjson"),
            ),
            (
                "files",
                ("test.jsonl", test_rows.encode("utf-8"), "application/x-ndjson"),
            ),
        ],
    )
    assert uploaded.status_code == 200, uploaded.text

    notebook = await _create_notebook(client, headers)
    started = await client.post(
        f"/api/notebooks/{notebook['id']}/sessions",
        headers=headers,
        json={"kernel": "python3"},
    )
    assert started.status_code == 200, started.text
    session = started.json()["data"]
    shared_ref = f"{other['id']}/shared-records"
    code = (
        "from trainhub_runtime import load_dataset\n"
        f"ds = load_dataset('{shared_ref}')\n"
        "print('TRAIN=' + str(len(ds.train)))\n"
        "print('TEST=' + str(len(ds.test)))\n"
        "print('TYPE=' + ds.train[1]['answers']['email_type'])\n"
        "print('GROUPS=' + ','.join(ds.groups['train']))\n"
        "print('REF=' + ds.reference)\n"
        "print('MANIFEST=' + ds.manifest['name'])\n"
        "print('TRAIN_FILE=' + ds.files['train'].rsplit('/', 1)[-1])\n"
    )
    try:
        executed = await client.post(
            f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}/execute",
            headers=headers,
            json={"code": code, "cellId": "c1"},
        )
        assert executed.status_code == 200, executed.text
        result = executed.json()["data"]
        assert result["status"] == "ok", result.get("message")
        text = "".join(
            item.get("text", "")
            for item in result["outputs"]
            if item.get("output_type") == "stream"
        )
    finally:
        await client.delete(
            f"/api/notebooks/{notebook['id']}/sessions/{session['sessionId']}",
            headers=headers,
        )

    assert "TRAIN=2" in text
    assert "TEST=1" in text
    assert "TYPE=Spam" in text
    assert "GROUPS=g1,g2" in text
    assert f"REF={shared_ref}" in text
    assert "MANIFEST=wmg-shared-records" in text
    assert "TRAIN_FILE=train.jsonl" in text
