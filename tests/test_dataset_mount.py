from __future__ import annotations

from pathlib import Path

from trainhub.dataset_mount import sync_dataset_mount


def _record(path: Path, filename: str | None = None) -> dict:
    return {"filename": filename or path.name, "storage_path": str(path)}


def test_sync_dataset_mount_links_and_replaces(tmp_path: Path):
    first = tmp_path / "first.txt"
    first.write_bytes(b"first")
    second = tmp_path / "second.txt"
    second.write_bytes(b"second")
    target = tmp_path / "dataset"

    linked, missing = sync_dataset_mount([_record(first)], target)
    assert (linked, missing) == (1, [])
    assert (target / "first.txt").read_bytes() == b"first"

    linked, missing = sync_dataset_mount([_record(second)], target)
    assert (linked, missing) == (1, [])
    assert not (target / "first.txt").exists()
    assert (target / "second.txt").read_bytes() == b"second"


def test_sync_dataset_mount_creates_subdirectories(tmp_path: Path):
    source = tmp_path / "nested.jsonl"
    source.write_bytes(b"nested")
    target = tmp_path / "dataset"
    linked, missing = sync_dataset_mount(
        [{"filename": "sub/nested.jsonl", "storage_path": str(source)}], target
    )
    assert (linked, missing) == (1, [])
    assert (target / "sub" / "nested.jsonl").read_bytes() == b"nested"


def test_sync_dataset_mount_reports_missing_and_invalid_names(tmp_path: Path):
    source = tmp_path / "ok.txt"
    source.write_bytes(b"ok")
    target = tmp_path / "dataset"

    linked, missing = sync_dataset_mount(
        [
            _record(source),
            {"filename": "../escape.txt", "storage_path": str(source)},
            {"filename": "gone.txt", "storage_path": str(tmp_path / "gone.txt")},
        ],
        target,
    )
    assert linked == 1
    assert missing == ["../escape.txt", "gone.txt"]
    assert (target / "ok.txt").read_bytes() == b"ok"
    assert not (tmp_path / "escape.txt").exists()
