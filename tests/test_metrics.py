from __future__ import annotations

import json
from pathlib import Path

from helpers import upload_notebook, wait_for_status

from trainhub.metrics import parse_metrics_text, read_metrics_file


def test_parse_metrics_contract_and_skips_invalid_lines():
    text = (
        '{"step": 1, "metrics": {"loss": 0.9, "accuracy": 0.6}}\n'
        "not-json\n"
        '{"step": 2, "metrics": {"loss": 0.5, "accuracy": true}}\n'
        '{"epoch": 3, "metrics": {"loss": 0.3}}\n'
        '{"step": 4, "metrics": {}}\n'
    )
    parsed = parse_metrics_text(text)
    assert parsed["sampleCount"] == 3
    assert parsed["pointCount"] == 4
    assert parsed["skippedLines"] == 2
    assert parsed["truncated"] is False
    by_name = {item["name"]: item["points"] for item in parsed["series"]}
    assert by_name["loss"] == [[1.0, 0.9], [2.0, 0.5], [3.0, 0.3]]
    assert by_name["accuracy"] == [[1.0, 0.6]]


def test_parse_metrics_uses_sample_index_and_ignores_partial_tail():
    text = (
        '{"metrics": {"loss": 1.0}}\n'
        '{"metrics": {"loss": 0.8}}\n'
        '{"step": 3, "metrics": {"loss": 0.6'
    )
    parsed = parse_metrics_text(text)
    assert parsed["sampleCount"] == 2
    assert parsed["series"][0]["points"] == [[1.0, 1.0], [2.0, 0.8]]
    assert parsed["skippedLines"] == 0


def test_parse_metrics_downsamples_without_losing_edges():
    lines = [json.dumps({"step": index, "metrics": {"loss": 1 / index}}) for index in range(1, 11)]
    parsed = parse_metrics_text("\n".join(lines) + "\n", max_points=3)
    points = parsed["series"][0]["points"]
    assert len(points) == 3
    assert points[0] == [1.0, 1.0]
    assert points[-1] == [10.0, 0.1]
    assert parsed["truncated"] is True


def test_read_metrics_file_missing_empty_and_symlink(tmp_path: Path):
    missing = read_metrics_file(tmp_path / "metrics.jsonl")
    assert missing["present"] is False
    assert missing["series"] == []

    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("", encoding="utf-8")
    empty = read_metrics_file(empty_path)
    assert empty["present"] is True
    assert empty["series"] == []

    target = tmp_path / "target.jsonl"
    target.write_text('{"step": 1, "metrics": {"loss": 0.1}}\n', encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    linked = read_metrics_file(link)
    assert linked["present"] is True
    assert linked["series"] == []
    assert "符号链接" in linked["error"]


def _metrics_notebook_source(epochs: int = 3) -> str:
    return (
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "path = pathlib.Path(os.environ['WMG_METRICS_FILE'])\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with path.open('a', encoding='utf-8') as fh:\n"
        f"    for epoch in range(1, {epochs + 1}):\n"
        "        fh.write(json.dumps({'step': epoch, 'metrics': {'loss': 1.0 / epoch, "
        "'accuracy': 0.5 + 0.1 * epoch}}) + '\\n')\n"
    )


async def test_run_metrics_api_and_environment_snapshot(client, headers):
    notebook = await upload_notebook(
        client, headers, _metrics_notebook_source(), name="metrics demo"
    )
    response = await client.post("/api/runs", headers=headers, json={"notebookId": notebook["id"]})
    assert response.status_code == 200, response.text
    run = response.json()["data"]
    final = await wait_for_status(client, headers, run["id"], ("succeeded", "failed"))
    assert final["status"] == "succeeded", final.get("message")

    metrics = (await client.get(f"/api/runs/{run['id']}/metrics", headers=headers)).json()["data"]
    assert metrics["present"] is True
    assert metrics["sampleCount"] == 3
    assert metrics["pointCount"] == 6
    assert metrics["skippedLines"] == 0
    assert metrics["truncated"] is False
    series = {item["name"]: item["points"] for item in metrics["series"]}
    assert series["loss"] == [[1.0, 1.0], [2.0, 0.5], [3.0, 1 / 3]]
    assert series["accuracy"] == [[1.0, 0.6], [2.0, 0.7], [3.0, 0.8]]

    detail = (await client.get(f"/api/runs/{run['id']}", headers=headers)).json()["data"]
    environment = detail["environment"]
    assert environment["trainhubVersion"]
    assert environment["service"]["pythonVersion"]
    assert environment["service"]["platform"]
    assert environment["kernel"]["effective"] == "python3"
    assert environment["gpu"] == [] or isinstance(environment["gpu"], list)

    admin = await client.get(
        f"/api/admin/runs/{run['id']}/metrics", headers={"X-Admin-Token": "admin-test-token"}
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["data"]["pointCount"] == 6


async def test_run_without_metrics_returns_empty_payload(client, headers):
    notebook = await upload_notebook(client, headers, 'print("no metrics here")', name="plain")
    response = await client.post("/api/runs", headers=headers, json={"notebookId": notebook["id"]})
    run = response.json()["data"]
    final = await wait_for_status(client, headers, run["id"], ("succeeded", "failed"))
    assert final["status"] == "succeeded"

    metrics = (await client.get(f"/api/runs/{run['id']}/metrics", headers=headers)).json()["data"]
    assert metrics["present"] is False
    assert metrics["series"] == []
    assert metrics["pointCount"] == 0


def test_read_metrics_file_reads_only_tail_when_file_is_large(tmp_path: Path, monkeypatch):
    import trainhub.metrics as metrics_module

    path = tmp_path / "metrics.jsonl"
    lines = [json.dumps({"step": index, "metrics": {"loss": index / 10}}) for index in range(1, 11)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(metrics_module, "MAX_METRICS_BYTES", 80)

    parsed = metrics_module.read_metrics_file(path)
    assert parsed["present"] is True
    assert parsed["truncated"] is True
    points = parsed["series"][0]["points"]
    assert points[-1] == [10.0, 1.0]
    assert points[0][0] > 1.0


def test_parse_metrics_caps_series_count(monkeypatch):
    import trainhub.metrics as metrics_module

    monkeypatch.setattr(metrics_module, "MAX_SERIES", 1)
    parsed = metrics_module.parse_metrics_text(
        '{"step": 1, "metrics": {"loss": 0.5, "accuracy": 0.8}}\n'
    )
    assert [item["name"] for item in parsed["series"]] == ["loss"]
    assert parsed["truncated"] is True
