"""Regression: Australia/Sydney spring-forward (2026-10-04, +10:00 to +11:00) must not fire a
cron job an hour early and then again at the right hour.

Observed in production on an engine without the zone-anchored ``compute_next_run``: every job
whose last run was before the change got a next run one hour early (17:00 instead of 18:00),
fired, then fired again at 18:00. The last-run timestamps below are the real ones.
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

pytest.importorskip("croniter")

import hermes_time
from cron.jobs import compute_next_run

SYDNEY = ZoneInfo("Australia/Sydney")


@pytest.fixture
def sydney(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEZONE", "Australia/Sydney")
    hermes_time.reset_cache()
    yield
    hermes_time.reset_cache()


@pytest.mark.parametrize(
    ("expr", "last_run_at", "expected_local"),
    [
        ("0 18 * * *", "2026-10-03T18:02:00+10:00", "2026-10-04T18:00"),
        ("0 7 * * 1-5", "2026-10-02T07:01:00+10:00", "2026-10-05T07:00"),
        ("45 11 * * 1-5", "2026-10-02T11:46:00+10:00", "2026-10-05T11:45"),
        ("0 8 * * *", "2026-10-03T08:02:00+10:00", "2026-10-04T08:00"),
        ("0 15-18 * * *", "2026-10-03T18:02:00+10:00", "2026-10-04T15:00"),
    ],
)
def test_first_run_after_spring_forward_keeps_the_wall_clock(sydney, expr, last_run_at,
                                                              expected_local):
    nxt = datetime.fromisoformat(
        compute_next_run({"kind": "cron", "expr": expr}, last_run_at=last_run_at))
    local = nxt.astimezone(SYDNEY)
    assert local.strftime("%Y-%m-%dT%H:%M") == expected_local
    assert local.utcoffset().total_seconds() == 11 * 3600


def test_daily_job_fires_once_per_day_across_the_change(sydney):
    """Chaining next runs from 2 Oct to 7 Oct yields exactly one 18:00 slot per local day."""
    last = "2026-10-02T18:00:00+10:00"
    days = []
    for _ in range(5):
        last = compute_next_run({"kind": "cron", "expr": "0 18 * * *"}, last_run_at=last)
        local = datetime.fromisoformat(last).astimezone(SYDNEY)
        assert (local.hour, local.minute) == (18, 0)
        days.append(local.date().isoformat())
    assert days == ["2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07"]
