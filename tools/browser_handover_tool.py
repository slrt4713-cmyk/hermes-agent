"""browser_handover: lend the agent's live cloud browser page to the user.

The agent drives a site up to a step only the user should do (typing a card on a checkout,
solving a check), then hands that exact page over. The tool keeps the cloud session alive past
the end of the turn and writes a handover record under ``$HERMES_HOME/cache/browser-handover``;
a client such as Cockpit reads the record and lets the user drive the page from their phone.
Card details then go from the user's device to the browser provider and never enter the
conversation. The janitor closes the session once the handover expires.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 20
MAX_MINUTES = 30
RECORD_DIR_NAME = "browser-handover"


def handover_dir() -> Path:
    return Path(get_hermes_home()) / "cache" / RECORD_DIR_NAME


def handover_active(session_info: Optional[dict[str, Any]], *, now: Optional[float] = None) -> bool:
    """Whether the user currently holds this browser session through a handover."""
    until = (session_info or {}).get("handover_until")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return False
    return (time.time() if now is None else now) < float(until)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _write_record(record: dict[str, Any]) -> Path:
    directory = handover_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{record['id']}.json"
    temporary = directory / f".{record['id']}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    return target


def remove_expired_records(*, now: Optional[float] = None) -> None:
    """Delete handover records past their expiry (each carries a live page token)."""
    current = time.time() if now is None else now
    directory = handover_dir()
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        try:
            expires = float(json.loads(path.read_text(encoding="utf-8")).get("expires_epoch", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            expires = 0
        if expires <= current:
            path.unlink(missing_ok=True)


def _current_url(session_key: str) -> str:
    from tools import browser_tool_session as _session
    result = _session._run_browser_command(session_key, "get", ["url"], timeout=10)
    if not result.get("success"):
        return ""
    return str((result.get("data") or {}).get("url") or "").strip()


def _pick_page(pages: list[dict[str, str]], current_url: str) -> Optional[dict[str, str]]:
    if current_url:
        for page in pages:
            if page.get("url") == current_url:
                return page
    web_pages = [page for page in pages if page.get("url", "").startswith(("https://", "http://"))]
    return web_pages[-1] if web_pages else None


def browser_handover(minutes: Any = None, task_id: Optional[str] = None) -> str:
    from tools import browser_tool as _bt
    from tools import browser_tool_cloud as _cloud
    from tools import browser_tool_lifecycle as _lifecycle

    try:
        minutes = int(minutes) if minutes is not None else DEFAULT_MINUTES
    except (TypeError, ValueError):
        minutes = DEFAULT_MINUTES
    minutes = max(1, min(MAX_MINUTES, minutes))

    session_key = _bt._last_session_key(task_id or "default")
    with _bt._cleanup_lock:
        session_info = _bt._active_sessions.get(session_key)
    if not session_info or not session_info.get("bb_session_id"):
        return json.dumps({"success": False, "error_type": "no_cloud_session",
                           "error": "No cloud browser page is open for this task. Open the page with "
                                    "browser_navigate first, then hand it over."})
    provider = _cloud._get_cloud_provider()
    live_view = getattr(provider, "live_view", None)
    if live_view is None:
        return json.dumps({"success": False, "error_type": "unsupported",
                           "error": "This browser backend cannot hand a page to the user."})
    try:
        page = _pick_page(live_view(session_info["bb_session_id"]), _current_url(session_key))
    except Exception as exc:  # provider/network failure must reach the agent as a tool error
        logger.warning("browser_handover: live view failed: %s", exc)
        return json.dumps({"success": False, "error_type": "live_view_failed",
                           "error": "The browser provider did not return the page. Try again once."})
    if page is None:
        return json.dumps({"success": False, "error_type": "no_page",
                           "error": "The browser has no web page open to hand over."})

    now = time.time()
    expires = now + minutes * 60
    handover_id = secrets.token_urlsafe(12)
    _write_record({
        "id": handover_id,
        "created_at": _iso(now),
        "expires_at": _iso(expires),
        "expires_epoch": expires,
        "page_url": page["url"],
        "page_title": page["title"],
        "ws_url": page["ws_url"],
    })
    with _bt._cleanup_lock:
        session_info["handover_until"] = expires
    _lifecycle._update_session_activity(session_key)
    logger.info("browser_handover: session %s handed over until %s", session_info["bb_session_id"], _iso(expires))
    return json.dumps({
        "success": True,
        "handover_id": handover_id,
        "expires_at": _iso(expires),
        "page_title": page["title"],
        "instructions": (
            f"Put the line HANDOVER: {handover_id} on its own line in your reply: the user's app shows it "
            f"as a button that opens this live page for {minutes} minutes. Stop using the browser for this "
            "task; the user finishes on the page (for example types their card and pays). Never ask for or "
            "repeat card details."),
    })


BROWSER_HANDOVER_SCHEMA = {
    "name": "browser_handover",
    "description": (
        "Hand the current browser page to the user so they finish a step themselves on their phone, "
        "typically paying on a checkout page or solving a human check. Call it once you reached that page "
        "and told the user what they will pay. The page stays open for the user, then closes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": f"How long the user keeps the page (default "
                                                          f"{DEFAULT_MINUTES}, at most {MAX_MINUTES})."},
        },
        "required": [],
    },
}


def _handle_handover(args: dict[str, Any], **kwargs) -> str:
    return browser_handover(minutes=(args or {}).get("minutes"), task_id=kwargs.get("task_id"))


def _check_handover_available() -> bool:
    from tools import browser_tool_cloud as _cloud
    try:
        provider = _cloud._get_cloud_provider()
    except Exception:
        return False
    return provider is not None and hasattr(provider, "live_view")


from tools.registry import no_cache_check_fn, registry

registry.register(
    name="browser_handover",
    toolset="browser",
    schema=BROWSER_HANDOVER_SCHEMA,
    handler=_handle_handover,
    check_fn=no_cache_check_fn(_check_handover_available),
    emoji="🤝",
)
