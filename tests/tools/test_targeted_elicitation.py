import pytest

import tools.approval as approval


@pytest.fixture
def queued():
    session = "test-targeted-elicitation"
    entry = approval._ApprovalEntry({"command": "Delete the named event", "require_request_id": True,
                                     "allow_session": False, "allow_permanent": False})
    approval._gateway_queues[session] = [entry]
    yield session, entry
    approval._gateway_queues.pop(session, None)


def test_unscoped_approve_cannot_authorize_elicitation(queued):
    session, entry = queued
    assert approval.resolve_gateway_approval(session, "once") == 0
    assert not entry.event.is_set()
    assert approval.resolve_gateway_approval(session, "once", resolve_all=True) == 0
    assert not entry.event.is_set()


def test_old_button_cannot_approve_new_action(queued):
    session, entry = queued
    assert approval.resolve_gateway_approval(session, "once", request_id="expired-id") == 0
    assert not entry.event.is_set()
    assert approval.resolve_gateway_approval(session, "once", request_id=entry.data["request_id"]) == 1
    assert approval.resolve_gateway_approval(session, "once", request_id=entry.data["request_id"]) == 0


def test_session_and_permanent_approval_cannot_authorize_single_use_action(queued):
    session, entry = queued
    for choice in ("session", "always"):
        assert approval.resolve_gateway_approval(session, choice, request_id=entry.data["request_id"]) == 0
    assert not entry.event.is_set()
