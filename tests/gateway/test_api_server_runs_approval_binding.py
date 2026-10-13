"""/v1/runs approvals: a single-use confirmation is resolved only by its exact request_id, and the
pollable status keeps showing an approval still pending after another one was answered."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.gateway.test_api_server_runs import _claim_run, _create_runs_app, _make_adapter
from tools import approval as approval_mod
from tools import approval_gateway_wait


def _waiting_run(adapter, run_id, *entries):
    adapter._run_statuses[run_id] = {"run_id": run_id, "status": "waiting_for_approval"}
    adapter._run_approval_sessions[run_id] = run_id
    _claim_run(adapter, run_id)
    with approval_mod._lock:
        approval_mod._gateway_queues[run_id] = list(entries)


@pytest.mark.asyncio
async def test_approval_endpoint_binds_single_use_confirmation_to_its_request():
    adapter = _make_adapter()
    run_id = "target-bound-run"
    entry = approval_gateway_wait._ApprovalEntry({"command": "Delete selected event", "require_request_id": True})
    _waiting_run(adapter, run_id, entry)
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
    _waiting_run(adapter, run_id, first, second)
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
