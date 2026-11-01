from __future__ import annotations

import io
import zipfile
from pathlib import Path

from trainhub.dataset_mount import sync_dataset_mount
from trainhub.storage import safe_relative_path, strip_common_root


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_safe_relative_path_keeps_directories_and_normalizes_traversal():
    assert safe_relative_path("Folder/sub/a.txt") == "Folder/sub/a.txt"
    assert safe_relative_path("nested/../b.csv") == "b.csv"
    assert safe_relative_path("../a.txt") == "a.txt"
    assert strip_common_root(["Dataset/train.jsonl", "Dataset/test.jsonl"]) == (
        ["train.jsonl", "test.jsonl"],
        "Dataset",
    )
    assert strip_common_root(["train/a.jsonl", "test/b.jsonl"]) == (
        ["train/a.jsonl", "test/b.jsonl"],
        None,
    )


async def test_folder_upload_preserves_relative_paths(client, app, db, customer, headers, tmp_path):
    files = [
        ("files", ("WMGDataset/dataset_manifest.json", b'{"name": "wmg"}', "application/json")),
        ("files", ("WMGDataset/groups.json", b"{}", "application/json")),
        ("files", ("WMGDataset/train.jsonl", b'{"train": 1}\n', "application/json")),
        ("files", ("WMGDataset/sub/valid.jsonl", b'{"valid": 1}\n', "application/json")),
    ]
    response = await client.post("/api/datasets", headers=headers, files=files)
    assert response.status_code == 200, response.text
    dataset = response.json()["data"]
    assert dataset["name"] == "WMGDataset"
    assert dataset["fileCount"] == 4

    detail = await client.get(f"/api/datasets/{dataset['id']}", headers=headers)
    filenames = sorted(item["filename"] for item in detail.json()["data"]["files"])
    assert filenames == [
        "dataset_manifest.json",
        "groups.json",
        "sub/valid.jsonl",
        "train.jsonl",
    ]

    rows = db.get_dataset_files(dataset["id"])
    for item in rows:
        assert Path(item["storage_path"]).is_file()

    mount = tmp_path / "dataset"
    linked, missing = sync_dataset_mount(rows, mount)
    assert linked == 4 and missing == []
    assert (mount / "sub" / "valid.jsonl").read_bytes() == b'{"valid": 1}\n'
    assert (mount / "dataset_manifest.json").read_bytes() == b'{"name": "wmg"}'

    download = await client.get(f"/api/datasets/{dataset['id']}/download", headers=headers)
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert sorted(archive.namelist()) == filenames


async def test_extract_zip_dataset_into_directory(client, app, db, headers):
    payload = _zip_bytes(
        {
            "dataset_manifest.json": b'{"name": "wmg"}',
            "groups.json": b"{}",
            "train.jsonl": b'{"train": 1}\n',
            "test.jsonl": b'{"test": 1}\n',
        }
    )
    response = await client.post(
        "/api/datasets",
        headers=headers,
        data={"name": "Zipped"},
        files=[("files", ("dataset.zip", payload, "application/zip"))],
    )
    assert response.status_code == 200, response.text
    dataset = response.json()["data"]
    assert dataset["hasZip"] is True
    assert dataset["fileCount"] == 1
    original = db.get_dataset_files(dataset["id"])[0]
    original_path = Path(original["storage_path"])
    assert original_path.is_file()

    extracted = await client.post(f"/api/datasets/{dataset['id']}/extract", headers=headers)
    assert extracted.status_code == 200, extracted.text
    updated = extracted.json()["data"]
    assert updated["hasZip"] is False
    assert updated["fileCount"] == 4
    assert not original_path.exists()

    detail = await client.get(f"/api/datasets/{dataset['id']}", headers=headers)
    filenames = sorted(item["filename"] for item in detail.json()["data"]["files"])
    assert filenames == sorted(
        ["dataset_manifest.json", "groups.json", "train.jsonl", "test.jsonl"]
    )

    download = await client.get(f"/api/datasets/{dataset['id']}/download", headers=headers)
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert sorted(archive.namelist()) == filenames
        assert archive.read("train.jsonl") == b'{"train": 1}\n'


async def test_extract_requires_zip_and_owner(client, db, headers):
    response = await client.post(
        "/api/datasets",
        headers=headers,
        data={"name": "plain"},
        files=[("files", ("a.txt", b"hello", "text/plain"))],
    )
    dataset = response.json()["data"]
    rejected = await client.post(f"/api/datasets/{dataset['id']}/extract", headers=headers)
    assert rejected.status_code == 400

    other_token = "trh_other_extract_token_1234567890"
    other = db.create_customer("Other Extract", "other2@example.com", other_token)
    other_headers = {"Authorization": f"Bearer {other_token}"}
    zip_payload = _zip_bytes({"train.jsonl": b"x\n"})
    created = await client.post(
        "/api/datasets",
        headers=other_headers,
        data={"name": "other-zip", "shared": "true"},
        files=[("files", ("data.zip", zip_payload, "application/zip"))],
    )
    other_dataset = created.json()["data"]
    denied = await client.post(f"/api/datasets/{other_dataset['id']}/extract", headers=headers)
    assert denied.status_code == 404
