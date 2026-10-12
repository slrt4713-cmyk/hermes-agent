"""One delayed retry for recurring jobs that fail on a transient provider error.

Incident 2026-10-12: OpenAI answered "Our servers are currently overloaded"
and three daily jobs (Nick's brief, two of Simon's checks) were lost until
their next day. A recurring job that fails on a transient provider error now
gets one retry TRANSIENT_RETRY_DELAY_MINUTES later, then its normal schedule.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("croniter")

OVERLOADED = "RuntimeError: Our servers are currently overloaded. Please try again later."


@pytest.fixture
def jobs_mod(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    now = datetime(2026, 10, 13, 0, 0, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs_mod, "_hermes_now", lambda: now)
    jobs_mod.test_now = now
    return jobs_mod


def _daily_job(jobs_mod):
    return jobs_mod.create_job(prompt="brief", schedule="0 0 * * *")


def _job(jobs_mod, job_id):
    return next(job for job in jobs_mod.load_jobs() if job["id"] == job_id)


def test_overloaded_failure_schedules_one_retry(jobs_mod):
    job = _daily_job(jobs_mod)

    jobs_mod.mark_job_run(job["id"], False, OVERLOADED)

    stored = _job(jobs_mod, job["id"])
    expected = jobs_mod.test_now + timedelta(minutes=jobs_mod.TRANSIENT_RETRY_DELAY_MINUTES)
    assert datetime.fromisoformat(stored["next_run_at"]) == expected
    assert stored["transient_retry_pending"] is True
    assert stored["last_status"] == "error"


def test_failed_retry_returns_to_the_normal_schedule(jobs_mod):
    job = _daily_job(jobs_mod)
    jobs_mod.mark_job_run(job["id"], False, OVERLOADED)

    jobs_mod.mark_job_run(job["id"], False, OVERLOADED)

    stored = _job(jobs_mod, job["id"])
    assert datetime.fromisoformat(stored["next_run_at"]).date().isoformat() == "2026-10-14"
    assert "transient_retry_pending" not in stored


def test_successful_retry_clears_the_marker(jobs_mod):
    job = _daily_job(jobs_mod)
    jobs_mod.mark_job_run(job["id"], False, OVERLOADED)

    jobs_mod.mark_job_run(job["id"], True)

    stored = _job(jobs_mod, job["id"])
    assert "transient_retry_pending" not in stored
    assert datetime.fromisoformat(stored["next_run_at"]).date().isoformat() == "2026-10-14"


@pytest.mark.parametrize(
    "error",
    [
        "Script exited with code 1\nstderr:\nWeekly plan failed: RuntimeError: Sibyl tenant configuration is unavailable",
        "ValueError: invalid prompt",
        None,
    ],
)
def test_non_transient_failure_keeps_the_normal_schedule(jobs_mod, error):
    job = _daily_job(jobs_mod)

    jobs_mod.mark_job_run(job["id"], False, error)

    stored = _job(jobs_mod, job["id"])
    assert datetime.fromisoformat(stored["next_run_at"]).date().isoformat() == "2026-10-14"
    assert "transient_retry_pending" not in stored


@pytest.mark.parametrize(
    "error",
    [
        OVERLOADED,
        "RateLimitError: Error code: 429 - rate limit exceeded",
        "APIStatusError: Error code: 503 - Service temporarily unavailable",
        "InternalServerError: Error code: 502 - Bad gateway",
    ],
)
def test_transient_provider_errors_are_recognized(jobs_mod, error):
    assert jobs_mod._is_transient_provider_error(error)


def test_retry_never_delays_a_sooner_scheduled_run(jobs_mod):
    job = jobs_mod.create_job(prompt="watch", schedule="every 5m")

    jobs_mod.mark_job_run(job["id"], False, OVERLOADED)

    stored = _job(jobs_mod, job["id"])
    assert datetime.fromisoformat(stored["next_run_at"]) <= jobs_mod.test_now + timedelta(minutes=5)
    assert "transient_retry_pending" not in stored
