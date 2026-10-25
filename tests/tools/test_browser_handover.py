"""browser_handover: the user finishes a step (pays) on the agent's live cloud page."""

import json
import os
import stat
import time
from unittest.mock import MagicMock, patch

import pytest

from tools import browser_handover_tool as handover
from tools import browser_tool_lifecycle as bt_lifecycle

WS = "connect.browserbase.com/debug/sess-1/devtools/page/P1?t=tok"
DEBUG_PAYLOAD = {
    "pages": [
        {"url": "about:blank", "title": "", "debuggerFullscreenUrl":
            "https://www.browserbase.com/devtools-fullscreen/inspector.html?wss=connect.browserbase.com/debug/sess-1/devtools/page/P0?t=tok"},
        {"url": "https://shop.example/checkout", "title": "Checkout", "debuggerFullscreenUrl":
            f"https://www.browserbase.com/devtools-fullscreen/inspector.html?wss={WS}&debug=true"},
        {"url": "https://evil.example/", "title": "Elsewhere", "debuggerFullscreenUrl":
            "https://www.browserbase.com/devtools-fullscreen/inspector.html?wss=evil.example/debug/x"},
    ]
}


@pytest.fixture
def sessions():
    from tools import browser_tool
    saved = {name: getattr(browser_tool, name).copy()
             for name in ("_active_sessions", "_session_last_activity", "_last_active_session_key")}
    for name in saved:
        getattr(browser_tool, name).clear()
    yield browser_tool
    for name, value in saved.items():
        getattr(browser_tool, name).clear()
        getattr(browser_tool, name).update(value)


def _provider(pages=None, error=None):
    provider = MagicMock()
    if error is not None:
        provider.live_view.side_effect = error
    else:
        provider.live_view.return_value = pages
    return provider


def _open_cloud_session(browser_tool, task_id="task-1"):
    browser_tool._active_sessions[task_id] = {"session_name": "hermes_abc", "bb_session_id": "sess-1",
                                              "cdp_url": "wss://connect.browserbase.com?signingKey=secret"}
    browser_tool._last_active_session_key[task_id] = task_id
    browser_tool._session_last_activity[task_id] = time.time()


class TestBrowserbaseLiveView:
    def test_returns_direct_page_endpoints_and_drops_foreign_hosts(self, monkeypatch):
        from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider

        monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_key")
        monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj")
        response = MagicMock(ok=True)
        response.json.return_value = DEBUG_PAYLOAD
        with patch("plugins.browser.browserbase.provider.requests.get", return_value=response) as get:
            pages = BrowserbaseBrowserProvider().live_view("sess-1")

        assert get.call_args.args[0] == "https://api.browserbase.com/v1/sessions/sess-1/debug"
        assert [p["url"] for p in pages] == ["about:blank", "https://shop.example/checkout"]
        assert pages[1]["ws_url"] == f"wss://{WS}"

    def test_http_error_raises(self, monkeypatch):
        from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider

        monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_key")
        monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj")
        with patch("plugins.browser.browserbase.provider.requests.get", return_value=MagicMock(ok=False, status_code=404)):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                BrowserbaseBrowserProvider().live_view("gone")


class TestBrowserHandover:
    def test_no_cloud_session_is_refused(self, sessions):
        result = json.loads(handover.browser_handover(task_id="task-1"))
        assert result["success"] is False
        assert result["error_type"] == "no_cloud_session"

    def test_hands_over_the_current_page(self, sessions):
        _open_cloud_session(sessions)
        pages = [{"url": "about:blank", "title": "", "ws_url": "wss://connect.browserbase.com/a"},
                 {"url": "https://shop.example/checkout", "title": "Checkout", "ws_url": f"wss://{WS}"}]
        with (patch("tools.browser_tool_cloud._get_cloud_provider", return_value=_provider(pages)),
              patch("tools.browser_handover_tool._current_url", return_value="https://shop.example/checkout")):
            result = json.loads(handover.browser_handover(minutes=10, task_id="task-1"))

        assert result["success"] is True
        assert f"HANDOVER: {result['handover_id']}" in result["instructions"]
        record_path = handover.handover_dir() / f"{result['handover_id']}.json"
        record = json.loads(record_path.read_text())
        assert record["ws_url"] == f"wss://{WS}"
        assert record["page_title"] == "Checkout"
        assert 590 < record["expires_epoch"] - time.time() <= 600
        raw = record_path.read_text()
        assert "signingKey" not in raw and "bb_key" not in raw
        assert stat.S_IMODE(os.stat(record_path).st_mode) == 0o640
        assert handover.handover_active(sessions._active_sessions["task-1"])

    def test_minutes_are_capped(self, sessions):
        _open_cloud_session(sessions)
        pages = [{"url": "https://shop.example/checkout", "title": "Checkout", "ws_url": f"wss://{WS}"}]
        with (patch("tools.browser_tool_cloud._get_cloud_provider", return_value=_provider(pages)),
              patch("tools.browser_handover_tool._current_url", return_value="")):
            result = json.loads(handover.browser_handover(minutes=500, task_id="task-1"))
        record = json.loads((handover.handover_dir() / f"{result['handover_id']}.json").read_text())
        assert record["expires_epoch"] - time.time() <= handover.MAX_MINUTES * 60

    def test_provider_failure_is_reported_and_keeps_normal_cleanup(self, sessions):
        _open_cloud_session(sessions)
        with (patch("tools.browser_tool_cloud._get_cloud_provider", return_value=_provider(error=RuntimeError("502"))),
              patch("tools.browser_handover_tool._current_url", return_value="")):
            result = json.loads(handover.browser_handover(task_id="task-1"))
        assert result == {"success": False, "error_type": "live_view_failed",
                          "error": "The browser provider did not return the page. Try again once."}
        assert not handover.handover_active(sessions._active_sessions["task-1"])

    def test_backend_without_live_view_is_unsupported(self, sessions):
        _open_cloud_session(sessions)
        with patch("tools.browser_tool_cloud._get_cloud_provider", return_value=object()):
            result = json.loads(handover.browser_handover(task_id="task-1"))
        assert result["error_type"] == "unsupported"


class TestHandoverLifecycle:
    def test_turn_cleanup_keeps_a_handed_over_session(self, sessions):
        _open_cloud_session(sessions)
        sessions._active_sessions["task-1"]["handover_until"] = time.time() + 600
        with patch("tools.browser_tool_lifecycle._cleanup_single_browser_session") as reap:
            bt_lifecycle.cleanup_browser("task-1")
        reap.assert_not_called()
        assert "task-1" in sessions._active_sessions

    def test_expired_handover_is_cleaned_normally(self, sessions):
        _open_cloud_session(sessions)
        sessions._active_sessions["task-1"]["handover_until"] = time.time() - 1
        with patch("tools.browser_tool_lifecycle._cleanup_single_browser_session") as reap:
            bt_lifecycle.cleanup_browser("task-1")
        reap.assert_called_once_with("task-1")

    def test_shutdown_forces_the_close(self, sessions):
        _open_cloud_session(sessions)
        sessions._active_sessions["task-1"]["handover_until"] = time.time() + 600
        with patch("tools.browser_tool_lifecycle._cleanup_single_browser_session") as reap:
            bt_lifecycle.cleanup_all_browsers()
        reap.assert_any_call("task-1")

    def test_janitor_treats_a_live_handover_as_activity(self, sessions):
        _open_cloud_session(sessions)
        sessions._active_sessions["task-1"]["handover_until"] = time.time() + 600
        sessions._session_last_activity["task-1"] = 0.0
        with patch("tools.browser_tool_lifecycle.cleanup_browser") as cleanup:
            bt_lifecycle._cleanup_inactive_browser_sessions()
        cleanup.assert_not_called()
        assert sessions._session_last_activity["task-1"] > 0.0

    def test_janitor_reaps_after_the_handover_expires(self, sessions):
        _open_cloud_session(sessions)
        sessions._active_sessions["task-1"]["handover_until"] = time.time() - 1
        sessions._session_last_activity["task-1"] = 0.0
        with patch("tools.browser_tool_lifecycle.cleanup_browser") as cleanup:
            bt_lifecycle._cleanup_inactive_browser_sessions()
        cleanup.assert_called_once_with("task-1")

    def test_expired_records_are_deleted(self):
        directory = handover.handover_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "old.json").write_text(json.dumps({"expires_epoch": time.time() - 5}))
        (directory / "live.json").write_text(json.dumps({"expires_epoch": time.time() + 500}))
        (directory / "broken.json").write_text("{not json")
        handover.remove_expired_records()
        assert sorted(p.name for p in directory.glob("*.json")) == ["live.json"]
