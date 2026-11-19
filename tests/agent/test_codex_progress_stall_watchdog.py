"""Codex model-silence watchdog and interactive call ceiling.

Production failure: an official-Codex stream starts producing output, then stops while the
backend keeps sending lifecycle/keepalive frames. Those frames refresh the event-idle watchdog,
so only the wall-clock stale floor (up to 1200s on large contexts) ended the stream, and the
unchanged resend could wedge again: one interactive reply took 41 minutes while a fresh
connection answered in under a minute. The worker path now reconnects after a bounded model
silence, caps one interactive call, and tells the person waiting that it is retrying.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests.agent.test_codex_ttfb_watchdog import (
    _install_codex_event_stream,
    _make_codex_agent,
    _shorten_implicit_idle_watchdog,
)

_LARGE = "x" * 440_000  # ~110k estimated tokens: the 1200s stale floor applies


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("HERMES_CODEX_PROGRESS_STALL_SECONDS", "HERMES_CODEX_CALL_MAX_SECONDS",
                 "HERMES_CODEX_HARD_TIMEOUT_SECONDS", "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def _keepalive_after_progress(agent):
    """One text delta, then only lifecycle frames until the watchdog retires the request."""
    def stream():
        yield SimpleNamespace(type="response.created")
        yield SimpleNamespace(type="response.output_text.delta", delta="Draft")
        while getattr(agent, "_active_codex_stream_request_token", None) is not None:
            time.sleep(0.05)
            yield SimpleNamespace(type="response.in_progress")
        raise ConnectionError("retired keepalive-only stream")
    return stream


def test_keepalive_frames_after_progress_do_not_hold_the_call(tmp_path, monkeypatch):
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    statuses = []
    monkeypatch.setattr(agent, "_emit_status", lambda message: statuses.append(str(message)))
    # Without the model-silence kill this request would live until the 5s hard ceiling.
    monkeypatch.setenv("HERMES_CODEX_HARD_TIMEOUT_SECONDS", "5")
    _shorten_implicit_idle_watchdog(monkeypatch, h, timeout=60.0, progress_stall_timeout=0.8)
    closes = []
    _install_codex_event_stream(agent, monkeypatch, _keepalive_after_progress(agent), closes)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="no model progress for .* after progress began"):
        h.interruptible_api_call(agent, {"model": "gpt-5.6-sol", "input": "x" * 40_004})

    assert time.monotonic() - started < 3.0
    assert "codex_progress_stall_kill" in closes
    assert "stale_call_kill" not in closes
    assert statuses == ["This is taking longer than usual: I'm retrying the request."]


def test_steady_model_output_is_never_cut_by_the_silence_watchdog(tmp_path, monkeypatch):
    """Control: a stream that keeps producing output past the silence budget completes."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    statuses = []
    monkeypatch.setattr(agent, "_emit_status", lambda message: statuses.append(str(message)))
    _shorten_implicit_idle_watchdog(monkeypatch, h, timeout=60.0, progress_stall_timeout=0.5)
    closes = []
    sentinel = SimpleNamespace(ok=True)

    def fake_stream(api_kwargs, client=None, on_first_delta=None):
        from agent.codex_runtime import _codex_watchdog_state_var

        state = _codex_watchdog_state_var.get()
        for _ in range(8):  # 1.6s of output, three times the silence budget
            with state.lock:
                state.last_event_ts = state.last_progress_ts = time.time()
            time.sleep(0.2)
        return sentinel

    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: SimpleNamespace())
    monkeypatch.setattr(agent, "_abort_request_openai_client", lambda c, reason=None: closes.append(reason))
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda c, reason=None: closes.append(reason))
    monkeypatch.setattr(agent, "_run_codex_stream", fake_stream)

    assert h.interruptible_api_call(agent, {"model": "gpt-5.6-sol", "input": "x" * 40_004}) is sentinel
    assert "codex_progress_stall_kill" not in closes
    assert statuses == []


def test_official_codex_defaults_bound_a_large_interactive_call(tmp_path, monkeypatch):
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    wd = h._resolve_nonstream_watchdogs(agent, {"model": "gpt-5.6-sol", "input": _LARGE})

    assert wd.est_tokens > 100_000
    assert h.openai_codex_stale_timeout_floor(wd.est_tokens) == 1200.0
    assert wd.stale_timeout == h.CODEX_INTERACTIVE_CALL_MAX_SECONDS == 300.0
    assert wd.progress_stall_timeout == h.CODEX_PROGRESS_STALL_TIMEOUT_SECONDS == 180.0


@pytest.mark.parametrize("effort", ["high", "xhigh"])
def test_high_effort_gets_longer_bounds(tmp_path, monkeypatch, effort):
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    agent.reasoning_config = {"enabled": True, "effort": effort}
    wd = h._resolve_nonstream_watchdogs(agent, {"model": "gpt-5.6-sol", "input": _LARGE})

    assert wd.stale_timeout == 2 * h.HIGH_EFFORT_SILENCE_FLOOR_SECONDS
    assert wd.progress_stall_timeout == h.HIGH_EFFORT_SILENCE_FLOOR_SECONDS


def test_operator_env_overrides_and_disables(tmp_path, monkeypatch):
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    agent.reasoning_config = {"enabled": True, "effort": "high"}
    monkeypatch.setenv("HERMES_CODEX_PROGRESS_STALL_SECONDS", "90")
    monkeypatch.setenv("HERMES_CODEX_CALL_MAX_SECONDS", "200")
    wd = h._resolve_nonstream_watchdogs(agent, {"model": "gpt-5.6-sol", "input": _LARGE})
    assert (wd.progress_stall_timeout, wd.stale_timeout) == (90.0, 200.0)

    monkeypatch.setenv("HERMES_CODEX_PROGRESS_STALL_SECONDS", "0")
    monkeypatch.setenv("HERMES_CODEX_CALL_MAX_SECONDS", "0")
    wd = h._resolve_nonstream_watchdogs(agent, {"model": "gpt-5.6-sol", "input": _LARGE})
    assert wd.progress_stall_timeout == 0.0
    assert wd.stale_timeout == 1200.0


def test_compatible_backends_keep_their_policy(tmp_path, monkeypatch):
    """Only the official Codex backend wedges this way; other Responses backends are untouched."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch, provider="xai-oauth", base_url="https://api.x.ai/v1")
    agent.reasoning_config = {"enabled": False}
    wd = h._resolve_nonstream_watchdogs(agent, {"model": "grok-4", "input": _LARGE})

    assert wd.progress_stall_timeout == 0.0
    assert wd.stale_timeout > h.CODEX_INTERACTIVE_CALL_MAX_SECONDS


def test_retry_notice_is_sent_once_per_window(tmp_path, monkeypatch):
    from agent import chat_completion_nonstream as ns

    agent = _make_codex_agent(tmp_path, monkeypatch)
    statuses = []
    monkeypatch.setattr(agent, "_emit_status", lambda message: statuses.append(str(message)))
    request = ns._NonStreamRequest(agent, {"model": "gpt-5.6-sol", "input": "hi"})

    request._notify_wedge_retry()
    request._notify_wedge_retry()
    assert len(statuses) == 1

    agent._codex_wedge_notice_at -= ns._WEDGE_NOTICE_INTERVAL + 1
    request._notify_wedge_retry()
    assert len(statuses) == 2


def test_retry_notice_follows_the_display_language(tmp_path, monkeypatch):
    from agent import i18n

    monkeypatch.setenv("HERMES_LANGUAGE", "fr")
    assert i18n.t("display.notice.codex_wedge_retry", lang="fr") == (
        "Ça prend plus longtemps que d'habitude : je relance la demande.")


def test_explicit_stale_timeout_is_not_capped(tmp_path, monkeypatch):
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    agent.reasoning_config = {"enabled": False}
    monkeypatch.setattr(agent, "_stale_timeout_is_explicit", lambda: True)
    wd = h._resolve_nonstream_watchdogs(agent, {"model": "gpt-5.6-sol", "input": _LARGE})

    assert wd.stale_timeout == 1200.0
    assert not wd.interactive_cap


def test_a_call_cut_by_the_cap_lets_the_resend_use_the_floor(tmp_path, monkeypatch):
    """A genuinely long answer must not be cut twice in a row by the interactive ceiling."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    agent.reasoning_config = {"enabled": False}
    payload = {"model": "gpt-5.6-sol", "input": _LARGE}
    assert h._resolve_nonstream_watchdogs(agent, payload).interactive_cap

    agent._codex_interactive_cap_cut_at = time.time()
    resend = h._resolve_nonstream_watchdogs(agent, payload)
    assert (resend.stale_timeout, resend.interactive_cap) == (1200.0, False)

    agent._codex_interactive_cap_cut_at = time.time() - h.CODEX_CAP_COOLDOWN_SECONDS - 1
    assert h._resolve_nonstream_watchdogs(agent, payload).interactive_cap


def test_cap_kill_records_the_cut_and_notifies(tmp_path, monkeypatch):
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    statuses = []
    monkeypatch.setattr(agent, "_emit_status", lambda message: statuses.append(str(message)))
    _shorten_implicit_idle_watchdog(monkeypatch, h, timeout=60.0, stale_timeout=0.8,
                                    interactive_cap=True, progress_stall_timeout=0.0)

    def steady(api_kwargs, client=None, on_first_delta=None):
        from agent.codex_runtime import _codex_watchdog_state_var

        state = _codex_watchdog_state_var.get()
        deadline = time.time() + 10
        while time.time() < deadline and getattr(agent, "_active_codex_stream_request_token", None) is not None:
            with state.lock:
                state.last_event_ts = state.last_progress_ts = time.time()
            time.sleep(0.05)
        raise ConnectionError("cut")

    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: SimpleNamespace())
    monkeypatch.setattr(agent, "_abort_request_openai_client", lambda c, reason=None: None)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda c, reason=None: None)
    monkeypatch.setattr(agent, "_run_codex_stream", steady)

    with pytest.raises(TimeoutError, match="timed out after"):
        h.interruptible_api_call(agent, {"model": "gpt-5.6-sol", "input": "x" * 40_004})
    assert isinstance(agent._codex_interactive_cap_cut_at, float)
    assert len(statuses) == 1


def test_small_requests_have_no_silence_watchdog(tmp_path, monkeypatch):
    """Below 10k tokens the stream retry does not reset progress, so the silence watchdog stays off."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    agent.reasoning_config = {"enabled": False}
    wd = h._resolve_nonstream_watchdogs(agent, {"model": "gpt-5.6-sol", "input": "hello"})

    assert wd.official_codex
    assert wd.progress_stall_timeout == 0.0


def test_no_retry_notice_for_compatible_backends(tmp_path, monkeypatch):
    from agent import chat_completion_nonstream as ns

    agent = _make_codex_agent(tmp_path, monkeypatch, provider="xai-oauth", base_url="https://api.x.ai/v1")
    statuses = []
    monkeypatch.setattr(agent, "_emit_status", lambda message: statuses.append(str(message)))
    ns._NonStreamRequest(agent, {"model": "grok-4", "input": "hi"})._notify_wedge_retry()

    assert statuses == []


def test_a_failed_notice_does_not_silence_the_next_one(tmp_path, monkeypatch):
    from agent import chat_completion_nonstream as ns

    agent = _make_codex_agent(tmp_path, monkeypatch)
    statuses = []

    def flaky(message):
        if not statuses:
            statuses.append("failed")
            raise RuntimeError("gateway callback down")
        statuses.append(str(message))

    monkeypatch.setattr(agent, "_emit_status", flaky)
    request = ns._NonStreamRequest(agent, {"model": "gpt-5.6-sol", "input": "hi"})
    request._notify_wedge_retry()
    request._notify_wedge_retry()

    assert statuses[-1] == "This is taking longer than usual: I'm retrying the request."


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_retry_notice_reaches_telegram_unfiltered(lang):
    """The gateway drops noisy or provider-error-shaped statuses; the retry notice must get through."""
    from agent import i18n
    from gateway.config import Platform
    from gateway.run import _prepare_gateway_status_message

    text = i18n.t("display.notice.codex_wedge_retry", lang=lang)
    assert _prepare_gateway_status_message(Platform.TELEGRAM, "lifecycle", text) == text


def test_one_silence_kill_per_reply(tmp_path, monkeypatch):
    """After a model-silence kill the resend has no silence watchdog: a model that was really
    working silently loses one attempt, never loops."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    agent.reasoning_config = {"enabled": False}
    payload = {"model": "gpt-5.6-sol", "input": _LARGE}
    assert h._resolve_nonstream_watchdogs(agent, payload).progress_stall_timeout == 180.0

    agent._codex_progress_stall_cut_at = time.time()
    assert h._resolve_nonstream_watchdogs(agent, payload).progress_stall_timeout == 0.0

    agent._codex_progress_stall_cut_at = time.time() - h.CODEX_CAP_COOLDOWN_SECONDS - 1
    assert h._resolve_nonstream_watchdogs(agent, payload).progress_stall_timeout == 180.0


def test_silence_kill_records_its_time(tmp_path, monkeypatch):
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_HARD_TIMEOUT_SECONDS", "5")
    _shorten_implicit_idle_watchdog(monkeypatch, h, timeout=60.0, progress_stall_timeout=0.8)
    closes = []
    _install_codex_event_stream(agent, monkeypatch, _keepalive_after_progress(agent), closes)

    with pytest.raises(TimeoutError):
        h.interruptible_api_call(agent, {"model": "gpt-5.6-sol", "input": "x" * 40_004})
    assert time.time() - agent._codex_progress_stall_cut_at < 5


def test_long_reply_logs_its_longest_model_silence(tmp_path, monkeypatch, caplog):
    import logging

    from agent import chat_completion_helpers as h
    from agent import chat_completion_nonstream as ns

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(ns, "_SILENCE_LOG_MIN_SECONDS", 0.5)
    _shorten_implicit_idle_watchdog(monkeypatch, h, timeout=60.0, progress_stall_timeout=0.0)
    sentinel = SimpleNamespace(ok=True)

    def gap_then_answer(api_kwargs, client=None, on_first_delta=None):
        from agent.codex_runtime import _codex_watchdog_state_var

        state = _codex_watchdog_state_var.get()
        with state.lock:
            state.last_event_ts = state.last_progress_ts = time.time()
        time.sleep(1.2)  # the silence to measure
        with state.lock:
            state.last_event_ts = state.last_progress_ts = time.time()
        return sentinel

    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: SimpleNamespace())
    monkeypatch.setattr(agent, "_abort_request_openai_client", lambda c, reason=None: None)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda c, reason=None: None)
    monkeypatch.setattr(agent, "_run_codex_stream", gap_then_answer)
    caplog.set_level(logging.INFO, logger="agent.chat_completion_helpers")

    assert h.interruptible_api_call(agent, {"model": "gpt-5.6-sol", "input": "x" * 40_004}) is sentinel
    line = next(r.getMessage() for r in caplog.records if "longest model silence" in r.getMessage())
    silence = float(line.split("longest model silence after progress began ")[1].split("s")[0])
    assert 1.0 <= silence <= 2.0
