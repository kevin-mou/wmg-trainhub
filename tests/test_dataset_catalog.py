from __future__ import annotations

import json

from helpers import upload_dataset
from trainhub.dataset_catalog import (
    accessible_dataset_entries,
    dataset_catalog_path,
    write_dataset_catalog,
)


async def test_accessible_entries_resolve_owner_paths(client, app, db, customer, headers):
    own = await upload_dataset(client, headers, name="own-set")
    other = db.create_customer("Other Co", None, "trh_other_catalog_token_1234567890")
    other_headers = {"Authorization": "Bearer trh_other_catalog_token_1234567890"}
    shared = await upload_dataset(
        client,
        other_headers,
        name="shared-set",
        content=b"shared-catalog",
        shared=True,
    )

    rows = db.list_accessible_datasets(customer["id"])
    assert [row["name"] for row in rows] == ["own-set", "shared-set"]

    storage = app.state.storage
    entries = accessible_dataset_entries(db, storage, customer["id"], selected_id=shared["id"])
    by_name = {entry["name"]: entry for entry in entries}
    assert by_name["own-set"]["dir"] == str(storage.dataset_files_dir(customer["id"], own["id"]))
    assert by_name["own-set"]["owner_id"] == customer["id"]
    assert by_name["own-set"]["shared"] is False
    assert by_name["own-set"]["selected"] is False

    assert by_name["shared-set"]["dir"] == str(storage.dataset_files_dir(other["id"], shared["id"]))
    assert by_name["shared-set"]["owner_id"] == other["id"]
    assert by_name["shared-set"]["owner_name"] == "Other Co"
    assert by_name["shared-set"]["shared"] is True
    assert by_name["shared-set"]["selected"] is True

    # 取消共享后从可见清单中消失（运行中的任务仍按提交时快照解析，由 runner 补条目）。
    db.set_dataset_visibility(other["id"], shared["id"], "private")
    rows = db.list_accessible_datasets(customer["id"])
    assert [row["name"] for row in rows] == ["own-set"]


def test_write_dataset_catalog_shape(tmp_path):
    path = write_dataset_catalog(
        tmp_path / "work",
        customer_id="T0001",
        entries=[
            {
                "name": "demo",
                "dataset_id": "ds_1",
                "owner_id": "T0001",
                "owner_name": "Test Co",
                "shared": False,
                "selected": True,
                "file_count": 2,
                "size_bytes": 10,
                "dir": "/data/customers/T0001/datasets/ds_1/files",
            }
        ],
    )
    assert path == dataset_catalog_path(tmp_path / "work")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["customer_id"] == "T0001"
    assert payload["datasets"][0]["dir"].endswith("/datasets/ds_1/files")

    # 内容未变时不重写（保持文件 inode，避免每次单元格执行都刷新清单）。
    before = path.stat().st_ino
    write_dataset_catalog(tmp_path / "work", customer_id="T0001", entries=payload["datasets"])
    assert path.stat().st_ino == before
