from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from trainhub.dataset_catalog import dataset_entry, write_dataset_catalog
from trainhub_runtime import (
    Dataset,
    DatasetError,
    dataset_info,
    dataset_path,
    list_datasets,
    load_dataset,
)


def _dataset_dir(root: Path, owner: str, dataset_id: str) -> Path:
    directory = root / "customers" / owner / "datasets" / dataset_id / "files"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "data.txt").write_text(f"{owner}:{dataset_id}", encoding="utf-8")
    return directory


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_dataset(root: Path, *, name: str = "demo", artifact_prefix: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    train = [
        {
            "fields": {"sender": "a@x", "subject": "hello", "body": "one"},
            "answers": {"email_type": "Ham", "motivation": "Reply"},
        },
        {
            "fields": {"sender": "b@x", "subject": "buy", "body": "two"},
            "answers": {"email_type": "Spam", "motivation": "Follow the link"},
        },
    ]
    valid = [
        {
            "fields": {"sender": "c@x", "subject": "v", "body": "three"},
            "answers": {"email_type": "Ham", "motivation": "Unknown"},
        }
    ]
    test = [
        {
            "fields": {"sender": "d@x", "subject": "t", "body": "four"},
            "answers": {"email_type": "Phishing", "motivation": "Open attachment"},
        }
    ]
    files = {
        "train": f"{artifact_prefix}train.jsonl",
        "valid": f"{artifact_prefix}valid.jsonl",
        "test": f"{artifact_prefix}test.jsonl",
    }
    _jsonl(root / files["train"], train)
    _jsonl(root / files["valid"], valid)
    _jsonl(root / files["test"], test)
    (root / "groups.json").write_text(
        json.dumps({"train": ["g1", "g2"], "valid": ["g3"], "test": ["g4"]}),
        encoding="utf-8",
    )
    manifest = {
        "name": name,
        "artifacts": {
            files[split]: {"records": count}
            for split, count in (("train", 2), ("valid", 1), ("test", 1))
        },
    }
    (root / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _write_catalog(tmp_path: Path, entries: list[dict], customer_id: str = "T1") -> Path:
    return write_dataset_catalog(tmp_path / "work", customer_id=customer_id, entries=entries)


def _entry(
    name: str,
    directory: Path,
    *,
    owner_id: str,
    dataset_id: str = "",
    owner_name: str = "",
    shared: bool = False,
    selected: bool = False,
) -> dict:
    return dataset_entry(
        name=name,
        directory=directory,
        dataset_id=dataset_id or None,
        owner_id=owner_id,
        owner_name=owner_name,
        shared=shared,
        selected=selected,
    )


def test_resolves_own_and_shared_datasets_by_name(tmp_path, monkeypatch):
    own_dir = _dataset_dir(tmp_path, "T1", "ds_own")
    shared_dir = _dataset_dir(tmp_path, "T2", "ds_shared")
    catalog = _write_catalog(
        tmp_path,
        [
            _entry("own-set", own_dir, owner_id="T1", dataset_id="ds_own", selected=True),
            _entry(
                "shared-set",
                shared_dir,
                owner_id="T2",
                owner_name="Other Co",
                dataset_id="ds_shared",
                shared=True,
            ),
        ],
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    assert dataset_path("own-set") == str(own_dir)
    assert dataset_path() == str(own_dir)
    # 共享数据集必须带 customer_id 前缀
    assert dataset_path("T2/shared-set") == str(shared_dir)
    with pytest.raises(DatasetError, match="customer_id/数据集名称"):
        dataset_path("shared-set")

    info = dataset_info("T2/shared-set")
    assert info["owner_id"] == "T2"
    assert info["owner_name"] == "Other Co"
    assert info["shared"] is True
    assert info["reference"] == "T2/shared-set"
    assert info["path"] == str(shared_dir)

    listing = list_datasets()
    assert [item["name"] for item in listing] == ["own-set", "shared-set"]
    assert {item["reference"] for item in listing} == {"own-set", "T2/shared-set"}
    assert {item["path"] for item in listing} == {str(own_dir), str(shared_dir)}


def test_plain_name_only_matches_own_datasets(tmp_path, monkeypatch):
    own_dir = _dataset_dir(tmp_path, "T1", "ds_own")
    shared_dir = _dataset_dir(tmp_path, "T2", "ds_shared")
    catalog = _write_catalog(
        tmp_path,
        [
            _entry("data", own_dir, owner_id="T1", dataset_id="ds_own"),
            _entry(
                "data",
                shared_dir,
                owner_id="T2",
                dataset_id="ds_shared",
                shared=True,
                selected=True,
            ),
        ],
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    assert dataset_path("data") == str(own_dir)
    assert dataset_path("T2/data") == str(shared_dir)
    # 不传名字时仍取「当前选中」的共享数据集
    assert dataset_path() == str(shared_dir)


def test_duplicate_shared_names_use_qualified_references(tmp_path, monkeypatch):
    second = _dataset_dir(tmp_path, "T2", "ds_a")
    third = _dataset_dir(tmp_path, "T3", "ds_b")
    catalog = _write_catalog(
        tmp_path,
        [
            _entry(
                "common",
                second,
                owner_id="T2",
                owner_name="Second Co",
                dataset_id="ds_a",
                shared=True,
            ),
            _entry(
                "common",
                third,
                owner_id="T3",
                owner_name="Third Co",
                dataset_id="ds_b",
                shared=True,
            ),
        ],
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    with pytest.raises(DatasetError, match="T3/common"):
        dataset_path("common")
    assert dataset_path("T3/common") == str(third)
    assert dataset_path("T2/common") == str(second)


def test_missing_and_unavailable_datasets(tmp_path, monkeypatch):
    existing = _dataset_dir(tmp_path, "T1", "ds_ok")
    missing = _dataset_dir(tmp_path, "T1", "ds_missing")
    catalog = _write_catalog(
        tmp_path,
        [
            _entry("ok", existing, owner_id="T1", dataset_id="ds_ok"),
            _entry("gone", missing, owner_id="T1", dataset_id="ds_missing"),
        ],
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    with pytest.raises(DatasetError) as exc:
        dataset_path("nope")
    assert "可用数据集" in str(exc.value)
    assert "ok" in str(exc.value)
    assert dataset_path("nope", required=False) is None

    shutil.rmtree(missing)
    with pytest.raises(DatasetError, match="目录不可用"):
        dataset_path("gone")


def test_fallback_without_catalog_uses_mounted_dataset(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    monkeypatch.delenv("WMG_DATASET_CATALOG", raising=False)
    monkeypatch.delenv("WMG_WORK_DIR", raising=False)
    monkeypatch.setenv("WMG_DATASET_DIR", str(dataset))
    monkeypatch.setenv("WMG_DATASET_NAME", "mounted-set")

    assert dataset_path() == str(dataset)
    assert dataset_path("mounted-set") == str(dataset)
    with pytest.raises(DatasetError, match="只能读取已挂载的数据集"):
        dataset_path("other-set")

    monkeypatch.delenv("WMG_DATASET_NAME")
    with pytest.raises(DatasetError, match="无法按名称解析"):
        dataset_path("named-set")


def test_malformed_catalog_fails_loudly(tmp_path, monkeypatch):
    path = tmp_path / "datasets.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(path))
    with pytest.raises(DatasetError, match="无法解析"):
        dataset_path("any")


def test_list_datasets_without_catalog_or_mount_is_empty(monkeypatch):
    monkeypatch.delenv("WMG_DATASET_CATALOG", raising=False)
    monkeypatch.delenv("WMG_WORK_DIR", raising=False)
    monkeypatch.delenv("WMG_DATASET_DIR", raising=False)
    monkeypatch.delenv("DATASET_DIR", raising=False)
    assert list_datasets() == []


def test_load_dataset_reads_splits_manifest_and_groups(tmp_path, monkeypatch):
    root = _write_dataset(_dataset_dir(tmp_path, "T1", "ds_demo"), name="demo")
    catalog = _write_catalog(
        tmp_path, [_entry("demo", root, owner_id="T1", dataset_id="ds_demo", selected=True)]
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    dataset = load_dataset("demo")
    assert isinstance(dataset, Dataset)
    assert dataset.name == "demo"
    assert dataset.reference == "demo"
    assert dataset.shared is False
    assert dataset.manifest["name"] == "demo"
    assert dataset.groups["train"] == ["g1", "g2"]
    assert len(dataset.train) == 2
    assert len(dataset.valid) == 1
    assert len(dataset.test) == 1
    assert len(dataset) == 4
    assert dataset.train[0]["answers"]["email_type"] == "Ham"
    assert dataset["test"][0]["fields"]["subject"] == "t"
    assert "train" in dataset
    assert dataset.files["train"].endswith("train.jsonl")

    only_train = load_dataset("demo", splits="train")
    assert list(only_train.splits) == ["train"]
    assert len(only_train.valid) == 0


def test_load_dataset_discovers_prefixed_artifact_names(tmp_path, monkeypatch):
    root = _write_dataset(
        _dataset_dir(tmp_path, "T1", "ds_prefixed"),
        name="wmg-mail-v3full",
        artifact_prefix="wmg_mail_v3full_",
    )
    catalog = _write_catalog(
        tmp_path,
        [_entry("wmg-mail-v3full", root, owner_id="T1", dataset_id="ds_prefixed")],
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    dataset = load_dataset("wmg-mail-v3full")
    assert len(dataset.train) == 2
    assert len(dataset.test) == 1
    assert dataset.files["train"].endswith("wmg_mail_v3full_train.jsonl")


def test_load_shared_dataset_requires_customer_id_prefix(tmp_path, monkeypatch):
    root = _write_dataset(_dataset_dir(tmp_path, "T2", "ds_shared"), name="shared-set")
    catalog = _write_catalog(
        tmp_path,
        [
            _entry(
                "shared-set",
                root,
                owner_id="T2",
                owner_name="Other Co",
                dataset_id="ds_shared",
                shared=True,
            )
        ],
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    with pytest.raises(DatasetError, match="customer_id/数据集名称"):
        load_dataset("shared-set")

    dataset = load_dataset("T2/shared-set")
    assert dataset.shared is True
    assert dataset.owner_id == "T2"
    assert dataset.reference == "T2/shared-set"
    assert len(dataset.train) == 2


def test_load_dataset_rejects_invalid_json_line(tmp_path, monkeypatch):
    root = _write_dataset(_dataset_dir(tmp_path, "T1", "ds_bad"), name="bad")
    (root / "train.jsonl").write_text('{"ok": 1}\nnot-json\n', encoding="utf-8")
    catalog = _write_catalog(tmp_path, [_entry("bad", root, owner_id="T1", dataset_id="ds_bad")])
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    with pytest.raises(DatasetError, match="第 2 行"):
        load_dataset("bad")


def test_load_dataset_without_jsonl_raises(tmp_path, monkeypatch):
    root = _dataset_dir(tmp_path, "T1", "ds_empty")
    catalog = _write_catalog(
        tmp_path, [_entry("empty", root, owner_id="T1", dataset_id="ds_empty")]
    )
    monkeypatch.setenv("WMG_DATASET_CATALOG", str(catalog))

    with pytest.raises(DatasetError, match="没有找到可读取的 JSONL"):
        load_dataset("empty")
