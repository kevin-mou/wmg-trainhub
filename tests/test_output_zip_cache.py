"""产物下载 zip 缓存：未变化时复用，源文件更新后重新打包。"""

from __future__ import annotations

import time

from trainhub.services import output_zip


def test_output_zip_reuses_cache_and_refreshes(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    (out / "a.txt").write_text("a", encoding="utf-8")
    run = {"id": "tr_x", "name": "run-demo", "output_path": str(out)}

    first, name = output_zip(storage=None, customer={}, run=run)
    assert name.endswith("-outputs.zip")
    first_mtime = first.stat().st_mtime_ns

    second, _ = output_zip(storage=None, customer={}, run=run)
    assert second.stat().st_mtime_ns == first_mtime  # 复用，不重打包

    time.sleep(0.02)
    (out / "b.txt").write_text("b", encoding="utf-8")
    third, _ = output_zip(storage=None, customer={}, run=run)
    assert third.stat().st_mtime_ns != first_mtime  # 源更新 → 重打包

    import zipfile

    with zipfile.ZipFile(third) as archive:
        assert sorted(archive.namelist()) == ["a.txt", "b.txt"]
