"""A recurring job whose provider refused every attempt for a transient cause (overloaded,
5xx, rate limited, timed out) before any model call completed enters the same bounded
re-run ladder as a network failure (cron/unreachable_retry.py), instead of losing its day."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cron import unreachable_retry as ur
from cron.jobs import create_job, get_due_jobs, get_job, mark_job_run
from cron.scheduler import run_job

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


def _agent_failure(reason):
    exc = RuntimeError("Error code: 529 - Our servers are currently overloaded")
    exc.failure_reason = reason
    return exc


@pytest.mark.parametrize("reason", sorted(ur.TRANSIENT_PROVIDER_REASONS))
def test_transient_provider_refusal_before_any_model_call_is_unreachable(reason):
    assert ur.is_model_unreachable_failure(_agent_failure(reason), SimpleNamespace(session_api_calls=0))


@pytest.mark.parametrize("reason", ["billing", "auth_permanent", "content_policy_blocked", "context_overflow", None])
def test_non_transient_failures_are_not_retried(reason):
    assert not ur.is_model_unreachable_failure(_agent_failure(reason), SimpleNamespace(session_api_calls=0))


def test_a_run_that_reached_the_model_is_never_rerun():
    # A mid-run overload may follow tool side effects: re-running could repeat them.
    assert not ur.is_model_unreachable_failure(_agent_failure("overloaded"), SimpleNamespace(session_api_calls=2))


def test_run_job_flags_an_overloaded_provider_for_the_ladder(tmp_path):
    job = {"id": "overloaded-brief", "name": "morning brief", "prompt": "brief me"}
    agent = MagicMock()
    agent.session_api_calls = 0
    agent.run_conversation.return_value = {
        "final_response": "The provider is overloaded.", "failed": True, "completed": False,
        "error": "Error code: 529 - Our servers are currently overloaded", "failure_reason": "overloaded",
        "messages": [], "api_calls": 3,
    }
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("run_agent.AIAgent", return_value=agent):
        success, _output, _final, error = run_job(job)

    assert success is False
    assert "overloaded" in error
    assert job.get("_model_unreachable") is True


def test_daily_cron_job_reruns_off_its_lattice_after_an_overload(tmp_path, monkeypatch):
    """The re-run instant is off the cron expression's lattice; it must still come due,
    not be re-anchored to tomorrow as a stale schedule."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fired = datetime(2026, 10, 12, 7, 0, 30, tzinfo=UTC)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: fired)
    monkeypatch.setattr(ur, "_hermes_now", lambda: fired)
    job_id = create_job("morning brief", "0 7 * * *")["id"]

    assert mark_job_run(job_id, False, "Error code: 529 - overloaded", model_unreachable=True)

    retry_at = datetime.fromisoformat(get_job(job_id)["next_run_at"])
    assert retry_at == fired + timedelta(seconds=ur.RETRY_DELAYS_SECONDS[0])
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: retry_at + timedelta(seconds=1))
    assert job_id in {due["id"] for due in get_due_jobs()}
