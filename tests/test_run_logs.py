from __future__ import annotations

from datetime import datetime

from trainhub.runner import RunLogWriter


def test_run_log_timestamp_uses_server_local_time(tmp_path, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None, "训练日志时间戳必须使用服务所在主机的本地时间"
            return cls(2026, 11, 18, 16, 23, 53)

    monkeypatch.setattr("trainhub.runner.datetime", FixedDateTime)
    path = tmp_path / "run.log"
    writer = RunLogWriter(path, 4096)
    try:
        writer.line("hello")
    finally:
        writer.close()

    assert path.read_text(encoding="utf-8") == "2026-11-18 16:23:53 hello\n"
