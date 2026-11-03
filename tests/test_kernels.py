#!/usr/bin/env python3
"""训练内核发现：Docker 模式的 manifest/环境变量配置。"""

from __future__ import annotations

from types import SimpleNamespace

from src.trainhub.kernels import configured_kernels, effective_kernels, manifest_kernels


def test_manifest_kernels_declares_three_drivers() -> None:
    names = [item["name"] for item in manifest_kernels()]
    assert names == ["wmg-trainer", "wmg-trainer-laya", "wmg-trainer-startlux"]


def test_configured_kernels_parses_env() -> None:
    items = configured_kernels("wmg-trainer-laya, wmg-trainer-startlux")
    assert [item["name"] for item in items] == ["wmg-trainer-laya", "wmg-trainer-startlux"]


def test_effective_kernels_docker_uses_config() -> None:
    settings = SimpleNamespace(sandbox_mode="docker", sandbox_kernels="wmg-trainer-laya")
    assert [item["name"] for item in effective_kernels(settings)] == ["wmg-trainer-laya"]
