"""Resumable /v1/runs event streams.

A client that disconnects (closed tab, phone asleep) must be able to
subscribe again, replay what the run already produced, then follow it live.
"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _RunEventLog


def _adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    return app


def _delta(text: str) -> dict:
    return {"event": "message.delta", "run_id": "run_1", "delta": text}


def _frames(body: str) -> list[dict]:
    events = []
    for frame in body.split("\n\n"):
        data = [line[5:].strip() for line in frame.split("\n") if line.startswith("data:")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


async def _read_until(response, marker: str) -> str:
    text = ""
    while marker not in text:
        chunk = await asyncio.wait_for(response.content.readany(), timeout=3)
        if not chunk:
            break
        text += chunk.decode()
    return text


@pytest.mark.asyncio
async def test_reconnect_replays_history_then_follows_live():
    adapter = _adapter()
    log = _RunEventLog()
    adapter._run_streams["run_1"] = log
    adapter._run_streams_created["run_1"] = 0.0
    log.put_nowait(_delta("Hello "))

    async with TestClient(TestServer(_app(adapter))) as client:
        first = await client.get("/v1/runs/run_1/events")
        assert await _read_until(first, "Hello ")
        first.close()
        await asyncio.sleep(0.05)

        # The run keeps producing while nobody is subscribed.
        assert adapter._run_streams.get("run_1") is log
        log.put_nowait(_delta("world"))

        second = await client.get("/v1/runs/run_1/events")
        backlog = await _read_until(second, "world")
        log.put_nowait({"event": "run.completed", "run_id": "run_1", "output": "Hello world"})
        log.put_nowait(None)
        body = backlog + await second.text()

    events = _frames(body)
    assert [e.get("delta") for e in events if e["event"] == "message.delta"] == ["Hello ", "world"]
    assert all(e.get("replay") is True for e in events if e["event"] == "message.delta")
    completed = [e for e in events if e["event"] == "run.completed"]
    assert completed and "replay" not in completed[0]
    assert "id: 3" in body


@pytest.mark.asyncio
async def test_two_subscribers_receive_the_same_events():
    adapter = _adapter()
    log = _RunEventLog()
    adapter._run_streams["run_1"] = log
    adapter._run_streams_created["run_1"] = 0.0

    async with TestClient(TestServer(_app(adapter))) as client:
        a = await client.get("/v1/runs/run_1/events")
        b = await client.get("/v1/runs/run_1/events")
        await asyncio.sleep(0.05)
        log.put_nowait(_delta("shared"))
        log.put_nowait(None)
        bodies = [await a.text(), await b.text()]

    for body in bodies:
        assert [e["delta"] for e in _frames(body)] == ["shared"]
    assert adapter._run_stream_subscribers.get("run_1", 0) == 0


@pytest.mark.asyncio
async def test_last_event_id_resumes_after_the_seen_events():
    adapter = _adapter()
    log = _RunEventLog()
    adapter._run_streams["run_1"] = log
    adapter._run_streams_created["run_1"] = 0.0
    for text in ("a", "b", "c"):
        log.put_nowait(_delta(text))
    log.put_nowait(None)

    async with TestClient(TestServer(_app(adapter))) as client:
        by_header = await client.get(
            "/v1/runs/run_1/events", headers={"Last-Event-ID": "2"}
        )
        by_query = await client.get("/v1/runs/run_1/events?after=1")
        header_body, query_body = await by_header.text(), await by_query.text()

    assert [e["delta"] for e in _frames(header_body)] == ["c"]
    assert [e["delta"] for e in _frames(query_body)] == ["b", "c"]


@pytest.mark.asyncio
async def test_finished_run_can_still_be_replayed_until_retention_ends():
    adapter = _adapter()
    log = _RunEventLog()
    adapter._run_streams["run_1"] = log
    adapter._run_streams_created["run_1"] = 0.0
    log.put_nowait(_delta("done"))
    log.put_nowait(None)

    adapter._sweep_orphaned_runs_once(log.closed_at + 60)
    assert adapter._run_streams.get("run_1") is log

    async with TestClient(TestServer(_app(adapter))) as client:
        body = await (await client.get("/v1/runs/run_1/events")).text()
    assert [e["delta"] for e in _frames(body)] == ["done"]

    adapter._sweep_orphaned_runs_once(log.closed_at + adapter._RUN_EVENTS_RETENTION + 1)
    assert "run_1" not in adapter._run_streams
    assert "run_1" not in adapter._run_streams_created


def test_live_run_history_survives_the_sweeper():
    adapter = _adapter()
    log = _RunEventLog()
    adapter._run_streams["run_1"] = log
    adapter._run_streams_created["run_1"] = 0.0
    log.put_nowait(_delta("still running"))

    adapter._sweep_orphaned_runs_once(adapter._RUN_STREAM_TTL * 100)

    assert adapter._run_streams.get("run_1") is log


def test_history_is_bounded_without_losing_terminal_events():
    log = _RunEventLog(max_events=3)
    for text in ("1", "2", "3", "4"):
        log.put_nowait(_delta(text))
    log.put_nowait({"event": "run.completed", "output": "1234"})
    log.put_nowait(None)

    assert [e.get("delta") for e in log.events if e["event"] == "message.delta"] == ["1", "2", "3"]
    assert log.events[-1]["event"] == "run.completed"
    assert log.dropped_events == 1
    assert log.closed
