from __future__ import annotations

from pathlib import Path

from trainhub.runner import RunExecutor


def _executor(app) -> RunExecutor:
    return RunExecutor(app.state.db, app.state.settings, app.state.storage)


def test_bundle_run_without_dataset_archive_exposes_visible_datasets(app, customer, tmp_path: Path):
    """训练任务只提交脚本：节点清单暴露该客户可见的数据集，脚本按 hub 名称加载。"""
    db = app.state.db
    db.create_dataset_with_files(
        customer_id=customer["id"],
        name="annotations-20261027@v1",
        description=None,
        files=[],
        size_bytes=0,
        file_count=0,
    )
    run = {
        "customer_id": customer["id"],
        "dataset_id": None,
        "dataset_name": "annotations-20261027@v1",
        "code_archive_path": "code.zip",
        "dataset_archive_path": None,
    }
    entries = _executor(app)._dataset_catalog_entries(run, tmp_path)
    assert [entry["name"] for entry in entries] == ["annotations-20261027@v1"]
    assert entries[0]["selected"] is False
    assert Path(entries[0]["dir"]).is_absolute()


def test_bundle_run_with_dataset_archive_exposes_only_that_dataset(app, customer, tmp_path: Path):
    """随任务上传了数据集归档（评估链路）：清单只暴露这一份，目录固定 work/dataset。"""
    run = {
        "customer_id": customer["id"],
        "dataset_id": None,
        "dataset_name": "demo",
        "code_archive_path": "code.zip",
        "dataset_archive_path": "dataset.zip",
    }
    entries = _executor(app)._dataset_catalog_entries(run, tmp_path)
    assert [(entry["name"], entry["selected"], entry["dir"]) for entry in entries] == [
        ("demo", True, str(tmp_path / "dataset"))
    ]
