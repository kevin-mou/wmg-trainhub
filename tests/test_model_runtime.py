from __future__ import annotations

import os
from pathlib import Path

import pytest

from trainhub.runtime_env import RUNTIME_PACKAGE_ROOT, kernel_runtime_env
from trainhub_runtime import models as runtime_models


def _fake_downloader(monkeypatch, *, files: tuple[str, ...] = ("config.json",)):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise RuntimeError("cache miss")
        target = Path(kwargs["cache_dir"]) / "models--acme--demo" / "snapshots" / "abc123"
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        return str(target)

    monkeypatch.setattr(runtime_models, "_snapshot_download", fake)
    return calls


def test_ensure_model_returns_local_directory(tmp_path: Path):
    local = tmp_path / "model"
    local.mkdir()
    (local / "config.json").write_text("{}", encoding="utf-8")
    path = runtime_models.ensure_model(str(local), required_files=["config.json"])
    assert path == str(local.resolve())


def test_ensure_model_downloads_into_shared_cache(tmp_path: Path, monkeypatch):
    calls = _fake_downloader(monkeypatch)
    root = tmp_path / "models"
    path = runtime_models.ensure_model("acme/demo", cache_root=root, required_files=["config.json"])
    assert Path(path).is_dir()
    assert calls[0]["local_files_only"] is True
    assert calls[-1]["local_files_only"] is False
    assert calls[-1]["cache_dir"] == str(root)
    assert (root / ".trainhub-locks").is_dir()


def test_model_cache_root_ignores_blank_explicit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WMG_MODEL_CACHE", str(tmp_path / "shared"))
    monkeypatch.delenv("TRAINHUB_MODEL_DIR", raising=False)
    # 空串不能被当成"显式指定"：Path('') 会落成当前目录
    assert runtime_models.model_cache_root("") == tmp_path / "shared"
    assert runtime_models.model_cache_root("   ") == tmp_path / "shared"


def test_ensure_model_exports_cache_env(tmp_path: Path, monkeypatch):
    _fake_downloader(monkeypatch)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_XET_CACHE", raising=False)
    root = tmp_path / "models"
    runtime_models.ensure_model("acme/demo", cache_root=root)
    assert os.environ["HF_HUB_CACHE"] == str(root)
    assert os.environ["HF_XET_CACHE"] == str(root / "xet")


def test_ensure_model_defaults_to_wmg_model_cache(tmp_path: Path, monkeypatch):
    calls = _fake_downloader(monkeypatch)
    shared = tmp_path / "shared"
    monkeypatch.setenv("WMG_MODEL_CACHE", str(shared))
    monkeypatch.delenv("TRAINHUB_MODEL_DIR", raising=False)
    path = runtime_models.ensure_model("acme/demo", required_files=["config.json"])
    assert Path(path).is_dir()
    assert calls[-1]["cache_dir"] == str(shared)


def test_ensure_model_cache_hit_skips_network(tmp_path: Path, monkeypatch):
    root = tmp_path / "models"
    target = root / "models--acme--demo" / "snapshots" / "rev1"
    target.mkdir(parents=True)
    (target / "config.json").write_text("{}", encoding="utf-8")
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        assert kwargs["local_files_only"] is True
        return str(target)

    monkeypatch.setattr(runtime_models, "_snapshot_download", fake)
    path = runtime_models.ensure_model("acme/demo", cache_root=root)
    assert path == str(target)
    assert len(calls) == 1


def test_ensure_model_checks_required_files(tmp_path: Path, monkeypatch):
    _fake_downloader(monkeypatch, files=())
    with pytest.raises(RuntimeError, match="缺少文件"):
        runtime_models.ensure_model(
            "acme/demo", cache_root=tmp_path / "models", required_files=["config.json"]
        )


def test_ensure_model_offline_miss_does_not_attempt_network(tmp_path: Path, monkeypatch):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("cache miss")

    monkeypatch.setattr(runtime_models, "_snapshot_download", fake)
    with pytest.raises(RuntimeError, match="离线模式"):
        runtime_models.ensure_model(
            "acme/demo", cache_root=tmp_path / "models", local_files_only=True
        )
    assert len(calls) == 1
    assert calls[0]["local_files_only"] is True


def test_cached_model_returns_none_when_missing(tmp_path: Path, monkeypatch):
    def fake(**kwargs):
        raise RuntimeError("cache miss")

    monkeypatch.setattr(runtime_models, "_snapshot_download", fake)
    assert runtime_models.cached_model("acme/demo", cache_root=tmp_path / "models") is None


def test_kernel_runtime_env_injects_shared_cache(settings):
    env = kernel_runtime_env(settings, {"PYTHONPATH": "/existing/src"})
    root = settings.model_dir_path
    assert env["WMG_MODEL_CACHE"] == str(root)
    assert env["HF_HUB_CACHE"] == str(root)
    assert env["HF_XET_CACHE"] == str(root / "xet")
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(RUNTIME_PACKAGE_ROOT)
    assert "/existing/src" in parts
    assert (root / ".trainhub-locks").is_dir()


async def test_admin_model_cache_endpoint(client, settings):
    entry = settings.model_dir_path / "models--acme--demo"
    (entry / "blobs").mkdir(parents=True)
    (entry / "blobs" / "abc").write_bytes(b"x" * 10)
    (entry / "refs").mkdir()
    (entry / "refs" / "main").write_text("abc", encoding="utf-8")

    response = await client.get("/api/admin/models", headers={"X-Admin-Token": "admin-test-token"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["totalBytes"] >= 10
    assert data["models"][0]["sizeBytes"] >= 10
    assert data["models"][0]["repoId"] == "acme/demo"
    assert data["models"][0]["refs"] == ["main"]
