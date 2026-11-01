from __future__ import annotations

import asyncio

from trainhub.kernel_sessions import KernelSession, plan_execution, source_digest


def _notebook_payload(source: str = "x = 1") -> dict:
    return {
        "name": "upto",
        "content": {
            "cells": [{"cell_type": "code", "metadata": {}, "source": source}],
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        },
    }


def _make_session(tmp_path, executed=None):
    session = KernelSession(
        session_id="ks_test",
        customer_id="cust",
        notebook_id="nb",
        kernel_name="python3",
        base_dir=tmp_path,
        km=None,
        client=None,
    )
    session.executed = dict(executed or {})
    session.is_alive = lambda: True  # type: ignore[method-assign]
    return session


def test_plan_execution_skips_unchanged_and_empty_cells():
    cells = [
        {"cellId": "a", "source": "x = 1"},
        {"cellId": "b", "source": "   \n"},
        {"cellId": "c", "source": "print(x)"},
    ]
    plan = plan_execution(cells, {"a": source_digest("x = 1")})
    assert [item["skip"] for item in plan] == [True, True, False]
    assert [item["cellId"] for item in plan] == ["a", "b", "c"]


def test_plan_execution_force_all_reruns_executed_cells():
    cells = [
        {"cellId": "a", "source": "x = 1"},
        {"cellId": "b", "source": "   \n"},
        {"cellId": "c", "source": "print(x)"},
    ]
    executed = {"a": source_digest("x = 1"), "c": source_digest("print(x)")}
    plan = plan_execution(cells, executed, force_all=True)
    assert [item["skip"] for item in plan] == [False, True, False]


def test_execute_sequence_runs_only_missing_cells_and_records(tmp_path):
    session = _make_session(tmp_path, {"a": source_digest("x = 1")})
    calls = []

    async def fake_execute(code, timeout):
        calls.append(code)
        return {"status": "ok", "executionCount": len(calls), "outputs": [], "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    results = asyncio.run(
        session.execute_sequence(
            [
                {"cellId": "a", "source": "x = 1"},
                {"cellId": "b", "source": "y = 2"},
                {"cellId": "c", "source": "print(x, y)"},
            ],
            60,
        )
    )
    assert calls == ["y = 2", "print(x, y)"]
    assert [item["status"] for item in results] == ["skipped", "ok", "ok"]
    assert session.executed["b"] == source_digest("y = 2")
    assert session.executed["c"] == source_digest("print(x, y)")


def test_execute_sequence_reexecutes_changed_cells(tmp_path):
    session = _make_session(tmp_path, {"a": source_digest("x = 1")})
    calls = []

    async def fake_execute(code, timeout):
        calls.append(code)
        return {"status": "ok", "executionCount": 1, "outputs": [], "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    results = asyncio.run(session.execute_sequence([{"cellId": "a", "source": "x = 2"}], 60))
    assert calls == ["x = 2"]
    assert results[0]["status"] == "ok"
    assert session.executed["a"] == source_digest("x = 2")


def test_execute_sequence_stops_on_failure_and_forgets_cell(tmp_path):
    session = _make_session(tmp_path)
    calls = []

    async def fake_execute(code, timeout, on_update=None):
        calls.append(code)
        if "boom" in code:
            return {
                "status": "error",
                "executionCount": 2,
                "outputs": [],
                "message": "BoomError: x",
            }
        return {"status": "ok", "executionCount": 1, "outputs": [], "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    results = asyncio.run(
        session.execute_sequence(
            [
                {"cellId": "a", "source": "ok()"},
                {"cellId": "b", "source": "boom()"},
                {"cellId": "c", "source": "after()"},
            ],
            60,
        )
    )
    assert calls == ["ok()", "boom()"]
    assert [item["status"] for item in results] == ["ok", "error"]
    assert "b" not in session.executed
    assert "c" not in session.executed


def test_single_cell_execute_records_and_clears_on_error(tmp_path):
    session = _make_session(tmp_path)

    async def scenario():
        async def ok(code, timeout):
            return {"status": "ok", "executionCount": 1, "outputs": [], "message": None}

        session._execute_locked = ok
        await session.execute("x = 1", 60, cell_id="a")
        assert session.executed["a"] == source_digest("x = 1")

        async def err(code, timeout):
            return {"status": "error", "executionCount": 2, "outputs": [], "message": "E"}

        session._execute_locked = err
        await session.execute("boom", 60, cell_id="a")
        assert "a" not in session.executed

    asyncio.run(scenario())


async def test_execute_upto_route_validates_target_payload_and_session(client, headers):
    notebook = await client.post("/api/notebooks", headers=headers, json=_notebook_payload())
    notebook = notebook.json()["data"]
    base = f"/api/notebooks/{notebook['id']}/sessions/ks_missing/execute-upto"

    # cells 为空 → 参数校验 422
    empty = await client.post(base, headers=headers, json={"cells": []})
    assert empty.status_code == 422

    # 目标单元格不在提交列表 → 400
    bad_target = await client.post(
        base,
        headers=headers,
        json={"cells": [{"cellId": "a", "source": "x = 1"}], "targetCellId": "b"},
    )
    assert bad_target.status_code == 400

    # 合法请求但会话不存在 → 404（路由已注册且鉴权/归属校验通过）
    missing = await client.post(
        base,
        headers=headers,
        json={"cells": [{"cellId": "a", "source": "x = 1"}], "targetCellId": "a"},
    )
    assert missing.status_code == 404
    assert "内核会话" in missing.json()["message"]


def test_execute_sequence_always_runs_forced_target_cell(tmp_path):
    """点击运行的那一格必须总是重新执行；只有前置格按未变跳过。"""
    session = _make_session(
        tmp_path,
        {"a": source_digest("x = 1"), "b": source_digest("print(x)")},
    )
    calls = []

    async def fake_execute(code, timeout):
        calls.append(code)
        return {"status": "ok", "executionCount": 3, "outputs": [], "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    results = asyncio.run(
        session.execute_sequence(
            [
                {"cellId": "a", "source": "x = 1"},
                {"cellId": "b", "source": "print(x)"},
            ],
            60,
            force_cell_id="b",
        )
    )
    assert calls == ["print(x)"]
    assert [item["status"] for item in results] == ["skipped", "ok"]


def test_execute_sequence_force_all_reruns_executed_cells(tmp_path):
    session = _make_session(
        tmp_path,
        {"a": source_digest("x = 1"), "b": source_digest("print(x)")},
    )
    calls = []

    async def fake_execute(code, timeout):
        calls.append(code)
        return {"status": "ok", "executionCount": len(calls), "outputs": [], "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    results = asyncio.run(
        session.execute_sequence(
            [
                {"cellId": "a", "source": "x = 1"},
                {"cellId": "b", "source": "print(x)"},
            ],
            60,
            force_all=True,
        )
    )
    assert calls == ["x = 1", "print(x)"]
    assert [item["status"] for item in results] == ["ok", "ok"]


def test_background_run_records_state_and_supports_polling(tmp_path):
    """执行是后台任务：立即返回运行态，轮询可取回逐格结果；运行中再启动返回 409。"""
    import pytest
    from types import SimpleNamespace

    from trainhub.kernel_sessions import KernelSessionManager
    from trainhub.responses import ApiError

    session = _make_session(tmp_path)
    session.session_id = "ks_run"
    calls = []

    async def fake_execute(code, timeout, on_update=None):
        calls.append(code)
        return {
            "status": "ok",
            "executionCount": len(calls),
            "outputs": [{"output_type": "stream", "name": "stdout", "text": "ok\n"}],
            "message": None,
        }

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    manager = KernelSessionManager(
        db=None, settings=SimpleNamespace(kernel_cell_timeout_seconds=60), storage=None
    )
    manager._sessions[("cust", "nb")] = session

    async def scenario():
        started = await manager.start_run(
            customer_id="cust",
            notebook_id="nb",
            session_id="ks_run",
            mode="upto",
            cells=[
                {"cellId": "a", "source": "x = 1"},
                {"cellId": "b", "source": "print(x)"},
            ],
            force_cell_id="b",
        )
        assert started["status"] == "running"
        assert started["sessionId"] == "ks_run"
        with pytest.raises(ApiError) as excinfo:
            await manager.start_run(
                customer_id="cust",
                notebook_id="nb",
                session_id="ks_run",
                mode="cell",
                cells=[{"cellId": "a", "source": "x = 1"}],
                force_cell_id="a",
            )
        assert excinfo.value.status_code == 409

        await session.run_task
        current = manager.current_run(customer_id="cust", notebook_id="nb", include_outputs=True)
        assert current is not None
        assert current["status"] == "succeeded"
        assert [item["status"] for item in current["cells"]] == ["ok", "ok"]
        assert current["cells"][1]["outputs"][0]["text"] == "ok\n"
        assert calls == ["x = 1", "print(x)"]

        light = manager.current_run(customer_id="cust", notebook_id="nb", include_outputs=False)
        assert "outputs" not in light["cells"][0]
        assert light["cells"][0]["hasOutputs"] is True

    asyncio.run(scenario())


def test_background_run_skips_unchanged_predecessors_and_stops_on_failure(tmp_path):
    from types import SimpleNamespace

    from trainhub.kernel_sessions import KernelSessionManager

    session = _make_session(tmp_path, {"a": source_digest("x = 1")})
    session.session_id = "ks_run2"
    calls = []

    async def fake_execute(code, timeout, on_update=None):
        calls.append(code)
        if "boom" in code:
            return {"status": "error", "executionCount": 2, "outputs": [], "message": "Boom: x"}
        return {"status": "ok", "executionCount": 1, "outputs": [], "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    manager = KernelSessionManager(
        db=None, settings=SimpleNamespace(kernel_cell_timeout_seconds=60), storage=None
    )
    manager._sessions[("cust", "nb2")] = session

    async def scenario():
        await manager.start_run(
            customer_id="cust",
            notebook_id="nb2",
            session_id="ks_run2",
            mode="upto",
            cells=[
                {"cellId": "a", "source": "x = 1"},
                {"cellId": "b", "source": "boom()"},
                {"cellId": "c", "source": "after()"},
            ],
            force_cell_id="c",
        )
        await session.run_task
        current = manager.current_run(customer_id="cust", notebook_id="nb2", include_outputs=True)
        assert [item["status"] for item in current["cells"]] == ["skipped", "error", "pending"]
        assert current["status"] == "failed"
        assert current["failedCellId"] == "b"
        assert current["message"] == "Boom: x"
        assert calls == ["boom()"]

    asyncio.run(scenario())


def test_background_run_all_reruns_executed_cells(tmp_path):
    """运行全部：已执行且源码未变的单元格也要重新执行。"""
    from types import SimpleNamespace

    from trainhub.kernel_sessions import KernelSessionManager

    session = _make_session(tmp_path, {"a": source_digest("x = 1")})
    session.session_id = "ks_all"
    calls = []

    async def fake_execute(code, timeout, on_update=None):
        calls.append(code)
        return {"status": "ok", "executionCount": len(calls), "outputs": [], "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    manager = KernelSessionManager(
        db=None, settings=SimpleNamespace(kernel_cell_timeout_seconds=60), storage=None
    )
    manager._sessions[("cust", "nb_all")] = session

    async def scenario():
        await manager.start_run(
            customer_id="cust",
            notebook_id="nb_all",
            session_id="ks_all",
            mode="all",
            cells=[
                {"cellId": "a", "source": "x = 1"},
                {"cellId": "b", "source": "print(x)"},
            ],
            force_all=True,
        )
        await session.run_task
        current = manager.current_run(
            customer_id="cust", notebook_id="nb_all", include_outputs=True
        )
        assert current is not None
        assert current["status"] == "succeeded"
        assert [item["status"] for item in current["cells"]] == ["ok", "ok"]
        assert calls == ["x = 1", "print(x)"]

    asyncio.run(scenario())


async def test_run_routes_validation_and_empty_state(client, headers):
    notebook = (
        await client.post("/api/notebooks", headers=headers, json=_notebook_payload())
    ).json()["data"]
    base = f"/api/notebooks/{notebook['id']}/runs"

    current = await client.get(f"{base}/current", headers=headers)
    assert current.status_code == 200
    assert current.json()["data"] is None

    empty = await client.post(base, headers=headers, json={"mode": "upto", "cells": []})
    assert empty.status_code == 400
    blank = await client.post(base, headers=headers, json={"mode": "cell", "code": "   "})
    assert blank.status_code == 400
    bad_target = await client.post(
        base,
        headers=headers,
        json={
            "mode": "upto",
            "cells": [{"cellId": "a", "source": "x = 1"}],
            "targetCellId": "b",
        },
    )
    assert bad_target.status_code == 400
    bad_mode = await client.post(base, headers=headers, json={"mode": "bogus"})
    assert bad_mode.status_code == 422
    all_empty = await client.post(base, headers=headers, json={"mode": "all"})
    assert all_empty.status_code == 400

    stream = await client.get(f"{base}/stream", headers=headers)
    assert stream.status_code == 404


async def test_run_stream_emits_terminal_event_for_finished_snapshot(client, app, headers):
    notebook = (
        await client.post("/api/notebooks", headers=headers, json=_notebook_payload())
    ).json()["data"]
    snapshot = {
        "runId": "run_done",
        "status": "succeeded",
        "failedCellId": None,
        "message": None,
        "lastEventSeq": 9,
        "cells": [],
    }

    class FakeSession:
        latest_run = snapshot

        def subscribe_run(self):
            return asyncio.Queue()

        def unsubscribe_run(self, _queue):
            return None

    class FakeManager:
        def get_session(self, **_kwargs):
            return FakeSession()

        def current_run(self, **_kwargs):
            return snapshot

    original = app.state.kernel_sessions
    app.state.kernel_sessions = FakeManager()
    try:
        response = await client.get(
            f"/api/notebooks/{notebook['id']}/runs/stream?runId=run_done",
            headers=headers,
        )
    finally:
        app.state.kernel_sessions = original

    assert response.status_code == 200
    assert '"e": "state"' in response.text
    assert '"e": "run"' in response.text
    assert '"status": "succeeded"' in response.text


async def test_restart_route_recreates_kernel_session(client, app, headers):
    """重启端点：只重建内核会话，不提交任何运行。"""
    notebook = (
        await client.post("/api/notebooks", headers=headers, json=_notebook_payload())
    ).json()["data"]
    captured = {}

    class FakeManager:
        async def restart_session(self, **kwargs):
            captured.update(kwargs)
            return {"sessionId": "ks_fresh"}

    original = app.state.kernel_sessions
    app.state.kernel_sessions = FakeManager()
    try:
        response = await client.post(
            f"/api/notebooks/{notebook['id']}/sessions/restart",
            headers=headers,
            json={"kernel": "python3"},
        )
    finally:
        app.state.kernel_sessions = original

    assert response.status_code == 200
    assert response.json()["data"]["sessionId"] == "ks_fresh"
    assert captured["customer"]["id"]
    assert captured["notebook"]["id"] == notebook["id"]
    assert captured["kernel_name"] == "python3"


async def test_run_route_all_mode_passes_force_all(client, app, headers):
    """API 接线：mode=all 时提交全部单元格且 force_all=True、不带目标单元格。"""
    notebook = (
        await client.post("/api/notebooks", headers=headers, json=_notebook_payload())
    ).json()["data"]
    captured = {}

    class FakeManager:
        async def ensure_session(self, **_kwargs):
            return {"sessionId": "ks_fake"}

        async def start_run(self, **kwargs):
            captured.update(kwargs)
            return {"runId": "run_fake", "status": "running", "cells": []}

    original = app.state.kernel_sessions
    app.state.kernel_sessions = FakeManager()
    try:
        response = await client.post(
            f"/api/notebooks/{notebook['id']}/runs",
            headers=headers,
            json={
                "mode": "all",
                "cells": [
                    {"cellId": "a", "source": "x = 1"},
                    {"cellId": "b", "source": "print(x)"},
                ],
            },
        )
    finally:
        app.state.kernel_sessions = original

    assert response.status_code == 200
    assert captured["mode"] == "all"
    assert captured["force_all"] is True
    assert captured["force_cell_id"] is None
    assert [cell["cellId"] for cell in captured["cells"]] == ["a", "b"]


def test_run_publishes_sse_events_with_incremental_stream(tmp_path):
    """后台运行实时发布 SSE 事件：cell/count/output(首个 stream)/stream(增量)/cell(终态)/run。"""
    from types import SimpleNamespace

    from trainhub.kernel_sessions import KernelSessionManager

    session = _make_session(tmp_path)
    session.session_id = "ks_stream"
    gate = asyncio.Event()

    async def fake_execute(code, timeout, on_update=None):
        outputs = [{"output_type": "stream", "name": "stdout", "text": "part1\n"}]
        if on_update:
            on_update(outputs, 1)
        await gate.wait()
        outputs[-1]["text"] += "part2\n"
        if on_update:
            on_update(outputs, 1)
        outputs.append(
            {
                "output_type": "execute_result",
                "data": {"text/plain": "42"},
                "metadata": {},
                "execution_count": 1,
            }
        )
        if on_update:
            on_update(outputs, 1)
        return {"status": "ok", "executionCount": 1, "outputs": outputs, "message": None}

    session._execute_locked = fake_execute  # type: ignore[method-assign]
    manager = KernelSessionManager(
        db=None, settings=SimpleNamespace(kernel_cell_timeout_seconds=60), storage=None
    )
    manager._sessions[("cust", "nb3")] = session

    async def scenario():
        queue = session.subscribe_run()

        async def take(timeout=2.0):
            try:
                return await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return None

        await manager.start_run(
            customer_id="cust",
            notebook_id="nb3",
            session_id="ks_stream",
            mode="cell",
            cells=[{"cellId": "a", "source": "run()"}],
            force_cell_id="a",
        )
        events = []
        while True:
            event = await take()
            assert event is not None, "应在执行结束前收到事件"
            events.append(event)
            if event.get("e") in ("stream", "output"):
                break
        kinds = [event.get("e") for event in events]
        assert kinds[0] == "cell" and events[0]["status"] == "running"
        assert "count" in kinds
        first_output = next(event for event in events if event.get("e") in ("stream", "output"))
        if first_output["e"] == "output":
            assert first_output["output"]["text"] == "part1\n"
        else:
            assert first_output["text"] == "part1\n"
        seqs = [event["seq"] for event in events if "seq" in event]
        assert seqs == sorted(seqs)

        current = manager.current_run(customer_id="cust", notebook_id="nb3", include_outputs=True)
        assert current["status"] == "running"
        assert current["cells"][0]["outputs"][0]["text"] == "part1\n"
        assert current["lastEventSeq"] >= 1

        gate.set()
        await session.run_task
        rest = []
        while True:
            event = await take()
            if event is None:
                break
            rest.append(event)
            if event.get("e") == "run":
                break
        rest_kinds = [event.get("e") for event in rest]
        assert "stream" in rest_kinds and "output" in rest_kinds
        assert "run" in rest_kinds
        suffix = next(event["text"] for event in rest if event.get("e") == "stream")
        assert suffix == "part2\n"  # 增量只发新增文本

        final = manager.current_run(customer_id="cust", notebook_id="nb3", include_outputs=True)
        assert final["status"] == "succeeded"
        assert [item["output_type"] for item in final["cells"][0]["outputs"]] == [
            "stream",
            "execute_result",
        ]
        assert final["cells"][0]["outputs"][0]["text"] == "part1\npart2\n"

    asyncio.run(scenario())
