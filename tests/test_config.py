from __future__ import annotations

import os
from pathlib import Path

from trainhub.config import _load_dotenv


def test_load_dotenv_reads_values_and_quotes(tmp_path: Path):
    env = tmp_path / "test.env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                "TRAINHUB_DOTENV_PLAIN=hello",
                'TRAINHUB_DOTENV_QUOTED="a b"',
                "export TRAINHUB_DOTENV_EXPORTED=yes",
                "IGNORED_LINE_WITHOUT_EQUALS",
            ]
        ),
        encoding="utf-8",
    )
    keys = [
        "TRAINHUB_DOTENV_PLAIN",
        "TRAINHUB_DOTENV_QUOTED",
        "TRAINHUB_DOTENV_EXPORTED",
    ]
    for key in keys:
        os.environ.pop(key, None)
    try:
        _load_dotenv(env)
        assert os.environ["TRAINHUB_DOTENV_PLAIN"] == "hello"
        assert os.environ["TRAINHUB_DOTENV_QUOTED"] == "a b"
        assert os.environ["TRAINHUB_DOTENV_EXPORTED"] == "yes"
    finally:
        for key in keys:
            os.environ.pop(key, None)


def test_process_env_wins_over_dotenv(tmp_path: Path, monkeypatch):
    env = tmp_path / "test.env"
    env.write_text("TRAINHUB_DOTENV_PRIORITY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TRAINHUB_DOTENV_PRIORITY", "from-process")
    _load_dotenv(env)
    assert os.environ["TRAINHUB_DOTENV_PRIORITY"] == "from-process"
