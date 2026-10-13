"""/v1/runs events stay resumable for the whole run and a while after it, ``?after=`` aliases
``?last_seq=``, and replayed events are marked."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.platforms import api_server_runs
from gateway.platforms.api_server_runs import _RunStream
from tests.gateway.test_api_server_runs import _claim_run, _create_runs_app, _make_adapter
from tests.waiting import async_wait_until


@pytest.fixture
def adapter():
    return _make_adapter()


def _frames(body: str) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in body.splitlines() if line.startswith("data: ")]


def test_finished_run_stays_replayable_for_the_retention_window(adapter):
    run_id = "run_finished"
    stream = adapter._run_streams[run_id] = _RunStream()
    adapter._run_streams_created[run_id] = 1000.0
    stream.put_nowait({"event": "run.completed"})
    stream.put_nowait(None)
    stream.closed_at = 2000.0

    adapter._sweep_orphaned_runs_once(2000.0 + api_server_runs.RUN_EVENTS_RETENTION)
    assert run_id in adapter._run_streams

    adapter._sweep_orphaned_runs_once(2000.0 + api_server_runs.RUN_EVENTS_RETENTION + 1)
    assert run_id not in adapter._run_streams
    assert run_id not in adapter._run_streams_created


def test_orphaned_transport_without_a_live_run_still_expires_after_the_ttl(adapter):
    run_id = "run_orphaned"
    adapter._run_streams[run_id] = _RunStream()
    adapter._run_streams_created[run_id] = 1000.0

    adapter._sweep_orphaned_runs_once(1000.0 + adapter._RUN_STREAM_TTL + 1)

    assert run_id not in adapter._run_streams


@pytest.mark.asyncio
async def test_after_resumes_past_the_given_id_and_marks_replayed_events(adapter):
    run_id = "run_resume"
    _claim_run(adapter, run_id)
    stream = adapter._run_streams[run_id] = _RunStream()
    for delta in ("a", "b", "c"):
        stream.put_nowait({"event": "message.delta", "delta": delta})
    stream.put_nowait({"event": "run.completed"})
    stream.put_nowait(None)

    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        full = _frames(await (await cli.get(f"/v1/runs/{run_id}/events")).text())
        resumed = _frames(await (await cli.get(f"/v1/runs/{run_id}/events?after=1")).text())

    assert [f["seq"] for f in full] == [0, 1, 2, 3]
    assert all(f["replay"] is True for f in full)
    assert [(f["seq"], f.get("delta")) for f in resumed] == [(2, "c"), (3, None)]


@pytest.mark.asyncio
async def test_live_events_are_not_marked_as_replay(adapter):
    run_id = "run_live"
    _claim_run(adapter, run_id)
    stream = adapter._run_streams[run_id] = _RunStream()
    stream.put_nowait({"event": "message.delta", "delta": "before"})

    async with TestClient(TestServer(_create_runs_app(adapter))) as cli:
        response = await cli.get(f"/v1/runs/{run_id}/events")
        await async_wait_until(lambda: stream.subscribers, timeout=5)
        stream.put_nowait({"event": "message.delta", "delta": "after"})
        stream.put_nowait(None)
        frames = _frames(await response.text())

    assert [(f["delta"], f.get("replay", False)) for f in frames] == [("before", True), ("after", False)]
