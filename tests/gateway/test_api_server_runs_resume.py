"""Run event resume and retention, and approval answers on /v1/runs."""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms.api_server_runs import _RUN_EVENTS_RETENTION, _RunStream
from tests.gateway.test_api_server_runs import _claim_run, _create_runs_app, _make_adapter
from tools import approval as approval_mod
from tools import approval_gateway_wait


@pytest.fixture
def adapter():
    return _make_adapter()


class TestRunRetention:
    def test_finished_run_stays_replayable_for_the_retention_window(self, adapter):
        run_id = "run_finished"
        stream = adapter._run_streams[run_id] = _RunStream()
        adapter._run_streams_created[run_id] = 1000.0
        stream.put_nowait({"event": "run.completed"})
        stream.put_nowait(None)
        stream.closed_at = 2000.0

        adapter._sweep_orphaned_runs_once(2000.0 + _RUN_EVENTS_RETENTION)
        assert run_id in adapter._run_streams

        adapter._sweep_orphaned_runs_once(2000.0 + _RUN_EVENTS_RETENTION + 1)
        assert run_id not in adapter._run_streams
        assert run_id not in adapter._run_streams_created

    def test_orphaned_transport_without_a_live_run_still_expires_after_the_ttl(self, adapter):
        run_id = "run_orphaned"
        adapter._run_streams[run_id] = _RunStream()
        adapter._run_streams_created[run_id] = 1000.0

        adapter._sweep_orphaned_runs_once(1000.0 + adapter._RUN_STREAM_TTL + 1)

        assert run_id not in adapter._run_streams


class TestRunEventResume:
    @staticmethod
    def _frames(body: str) -> list[dict]:
        return [json.loads(line.removeprefix("data: ")) for line in body.splitlines() if line.startswith("data: ")]

    @pytest.mark.asyncio
    async def test_after_resumes_past_the_given_id_and_marks_replayed_events(self, adapter):
        run_id = "run_resume"
        _claim_run(adapter, run_id)
        stream = adapter._run_streams[run_id] = _RunStream()
        for delta in ("a", "b", "c"):
            stream.put_nowait({"event": "message.delta", "delta": delta})
        stream.put_nowait({"event": "run.completed"})
        stream.put_nowait(None)

        async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
            full = self._frames(await (await cli.get(f"/v1/runs/{run_id}/events")).text())
            resumed = self._frames(await (await cli.get(f"/v1/runs/{run_id}/events?after=1")).text())

        assert [f["seq"] for f in full] == [0, 1, 2, 3]
        assert all(f["replay"] is True for f in full)
        assert [(f["seq"], f.get("delta")) for f in resumed] == [(2, "c"), (3, None)]

    @pytest.mark.asyncio
    async def test_live_events_are_not_marked_as_replay(self, adapter):
        run_id = "run_live"
        _claim_run(adapter, run_id)
        stream = adapter._run_streams[run_id] = _RunStream()
        stream.put_nowait({"event": "message.delta", "delta": "before"})

        async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
            response = await cli.get(f"/v1/runs/{run_id}/events")
            await asyncio.sleep(0.05)  # health: allow HX013 -- lets the subscriber attach before the live event
            stream.put_nowait({"event": "message.delta", "delta": "after"})
            stream.put_nowait(None)
            frames = self._frames(await asyncio.wait_for(response.text(), timeout=5))

        assert [(f["delta"], f.get("replay", False)) for f in frames] == [("before", True), ("after", False)]


@pytest.mark.asyncio
async def test_approval_endpoint_binds_single_use_confirmation_to_its_request():
    adapter = _make_adapter()
    run_id = "target-bound-run"
    entry = approval_gateway_wait._ApprovalEntry({"command": "Delete selected event", "require_request_id": True})
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_approval"}
    adapter._run_approval_sessions[run_id] = run_id
    _claim_run(adapter, run_id)
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = [entry]
    try:
        async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
            bare = await cli.post(f"/v1/runs/{run_id}/approval", json={"choice": "once"})
            assert bare.status == 409
            stale = await cli.post(f"/v1/runs/{run_id}/approval", json={"choice": "once", "request_id": "stale"})
            assert stale.status == 409
            assert not entry.event.is_set()
            exact = await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "once", "request_id": entry.data["request_id"]})
            assert exact.status == 200
            assert entry.result == "once"
    finally:
        approval_mod.unregister_gateway_notify(run_id)


@pytest.mark.asyncio
async def test_status_keeps_showing_an_approval_still_pending_after_another_answer():
    adapter = _make_adapter()
    run_id = "two-approvals-run"
    first = approval_gateway_wait._ApprovalEntry({"command": "rm -rf build-A"})
    second = approval_gateway_wait._ApprovalEntry({"command": "rm -rf build-B"})
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_approval"}
    adapter._run_approval_sessions[run_id] = run_id
    _claim_run(adapter, run_id)
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = [first, second]
    try:
        async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
            answered = await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "once", "request_id": first.data["request_id"]})
            assert answered.status == 200
            status = adapter._run_statuses[run_id]
            assert status["status"] == "waiting_for_approval"
            assert status["approval"]["request_id"] == second.data["request_id"]
            assert status["approval"]["choices"] == ["once", "session", "always", "deny"]

            last = await cli.post(
                f"/v1/runs/{run_id}/approval", json={"choice": "deny", "request_id": second.data["request_id"]})
            assert last.status == 200
            assert adapter._run_statuses[run_id]["status"] == "running"
            assert "approval" not in adapter._run_statuses[run_id]
    finally:
        approval_mod.unregister_gateway_notify(run_id)
