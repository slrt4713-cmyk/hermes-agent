"""Single-use confirmations (MCP elicitation, untrusted MCP tool, payment fill) are bound to the
exact request the user approved: a bare /approve, /approve all, a stale button or a session/always
scope can never approve them."""

import threading

import pytest

from tools import approval, approval_context
from tools.approval_context import set_current_session_key
from tools.approval_gateway_wait import _ApprovalEntry
from tools.approval_prompt import request_elicitation_consent


@pytest.fixture
def queued():
    session = "test-targeted-elicitation"
    entry = _ApprovalEntry({"command": "Delete the named event", "require_request_id": True,
                            "allow_session": False, "allow_permanent": False})
    approval._gateway_queues[session] = [entry]
    yield session, entry
    approval._gateway_queues.pop(session, None)


def test_unscoped_approve_cannot_authorize_single_use_confirmation(queued):
    session, entry = queued
    assert approval.resolve_gateway_approval(session, "once") == 0
    assert approval.resolve_gateway_approval(session, "once", resolve_all=True) == 0
    assert not entry.event.is_set()


def test_old_button_cannot_approve_a_newer_action(queued):
    session, entry = queued
    assert approval.resolve_gateway_approval(session, "once", request_id="expired-id") == 0
    assert not entry.event.is_set()
    assert approval.resolve_gateway_approval(session, "once", request_id=entry.data["request_id"]) == 1
    assert entry.result == "once"
    assert approval.resolve_gateway_approval(session, "once", request_id=entry.data["request_id"]) == 0


@pytest.mark.parametrize("choice", ["session", "always"])
def test_a_wider_answer_naming_the_request_counts_once(queued, choice):
    session, entry = queued
    assert approval.resolve_gateway_approval(session, choice, request_id=entry.data["request_id"]) == 1
    assert entry.result == "once"


@pytest.mark.parametrize("resolve_all", [False, True])
def test_a_bare_wider_answer_leaves_a_single_use_confirmation_pending(queued, resolve_all):
    session, entry = queued
    assert approval.resolve_gateway_approval(session, "always", resolve_all=resolve_all) == 0
    assert not entry.event.is_set()


def test_a_bare_approval_skips_a_single_use_request_ahead_of_it(queued):
    session, entry = queued
    regular = _ApprovalEntry({"command": "make clean"})
    approval._gateway_queues[session].append(regular)
    assert approval.resolve_gateway_approval(session, "once") == 1
    assert regular.result == "once" and not entry.event.is_set()


def test_a_wider_bare_answer_on_a_request_without_that_scope_counts_once():
    session = "test-no-scope"
    entry = _ApprovalEntry({"command": "make clean", "allow_session": False, "allow_permanent": False})
    approval._gateway_queues[session] = [entry]
    try:
        assert approval.resolve_gateway_approval(session, "session") == 1
        assert entry.result == "once"
    finally:
        approval._gateway_queues.pop(session, None)


def test_deny_still_applies_without_request_id(queued):
    session, entry = queued
    assert approval.resolve_gateway_approval(session, "deny") == 1
    assert entry.result == "deny"


def test_regular_command_approval_keeps_fifo_and_scopes():
    session = "test-regular-approval"
    entry = _ApprovalEntry({"command": "rm -rf build"})
    approval._gateway_queues[session] = [entry]
    try:
        assert approval.resolve_gateway_approval(session, "session") == 1
        assert entry.result == "session"
    finally:
        approval._gateway_queues.pop(session, None)


@pytest.fixture
def gateway_session(monkeypatch):
    session_key = "test:session:targeted-elicitation"
    token = set_current_session_key(session_key)
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "manual", "timeout": 60})
    try:
        yield session_key
    finally:
        approval.unregister_gateway_notify(session_key)
        approval_context._approval_session_key.reset(token)


def _answer_in_background(session_key, *answers):
    """Notify callback that replays *answers* (choice, use_request_id) from another thread, like a
    user tapping buttons after the card was sent."""
    sent = []

    def notify(data):
        sent.append(dict(data))

        def tap():
            for choice, bound in answers:
                approval.resolve_gateway_approval(
                    session_key, choice, request_id=data["request_id"] if bound else None)

        threading.Thread(target=tap, daemon=True).start()

    approval.register_gateway_notify(session_key, notify)
    return sent


def test_gateway_consent_offers_once_only_and_requires_the_exact_request(gateway_session):
    sent = _answer_in_background(gateway_session, ("session", True), ("once", False), ("once", True))

    assert request_elicitation_consent("Delete event 42?", "server asks", timeout_seconds=10) == "accept"
    assert sent[0]["require_request_id"] is True
    assert sent[0]["allow_session"] is False and sent[0]["allow_permanent"] is False


def test_gateway_consent_uses_the_elicitation_timeout(gateway_session):
    _answer_in_background(gateway_session, ("once", False))

    # The unbound "once" cannot approve it; the elicitation's own 1s budget (not the 60s
    # approvals.timeout) ends the wait as unanswered.
    assert request_elicitation_consent("Delete event 42?", "server asks", timeout_seconds=1) == "cancel"
