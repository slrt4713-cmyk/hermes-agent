"""Browserbase cloud browser provider: direct ``BROWSERBASE_API_KEY`` + ``BROWSERBASE_PROJECT_ID``
only (the Nous subscription routes through Browser Use). Config ``browser.cloud_provider:
"browserbase"``; knobs ``BROWSERBASE_BASE_URL``, ``BROWSERBASE_PROXIES`` (true),
``BROWSERBASE_ADVANCED_STEALTH`` (false), ``BROWSERBASE_KEEP_ALIVE`` (true),
``BROWSERBASE_SESSION_TIMEOUT`` (seconds, max 21600)."""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any, Dict, Optional

import requests

from agent.secret_scope import get_secret
from agent.ssl_verify import requests_verify
from plugins.browser._common import CloudBrowserProvider

logger = logging.getLogger(__name__)

# 402 fallbacks, in retry order: (session_config key, warning logged when dropped).
_PAID_FEATURE_FALLBACKS = (
    ("keepAlive", "keepAlive may require paid plan (402), retrying without it. "
                  "Sessions may timeout during long operations."),
    ("proxies", "Proxies unavailable (402), retrying without proxies. "
                "Bot detection may be less effective."))


def _config_session_timeout() -> Optional[str]:
    """``browser.session_timeout`` (seconds) from the profile config, the deployed alternative
    to ``BROWSERBASE_SESSION_TIMEOUT``: the project default (5 min) would end a handed-over
    checkout while the user pays."""
    try:
        from hermes_cli.config import read_raw_config
        value = (read_raw_config().get("browser") or {}).get("session_timeout")
    except Exception as exc:
        logger.debug("Could not read browser.session_timeout: %s", exc)
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return str(value).strip() or None


class BrowserbaseBrowserProvider(CloudBrowserProvider):
    """Browserbase (https://browserbase.com) cloud browser backend."""

    provider_id = "browserbase"
    label = "Browserbase"
    release_method = "post"
    release_path = "/v1/sessions/{session_id}"
    missing_credentials_error = (
        "Browserbase requires BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID "
        "environment variables.")
    close_fail_fmt = "Failed to close session %s: HTTP %s - %s"
    setup_tag = "Cloud browser with stealth and proxies"
    setup_env_vars = [
        {"key": "BROWSERBASE_API_KEY", "prompt": "Browserbase API key", "url": "https://browserbase.com"},
        {"key": "BROWSERBASE_PROJECT_ID", "prompt": "Browserbase project ID"},
    ]

    def _get_config_or_none(self) -> Optional[dict[str, Any]]:
        api_key = get_secret("BROWSERBASE_API_KEY")
        project_id = get_secret("BROWSERBASE_PROJECT_ID")
        if not (api_key and project_id):
            return None
        return {
            "api_key": api_key,
            "project_id": project_id,
            # Per-profile like the key: the scoped key must not be sent to the default profile's endpoint.
            "base_url": (get_secret("BROWSERBASE_BASE_URL", "") or "https://api.browserbase.com").rstrip("/"),
        }

    def _headers(self, config: dict[str, Any]) -> dict[str, str]:
        return {"Content-Type": "application/json", "X-BB-API-Key": config["api_key"]}

    def _release_headers(self, config: dict[str, Any]) -> dict[str, str]:
        return {"X-BB-API-Key": config["api_key"], "Content-Type": "application/json"}

    def _release_body(self, config: dict[str, Any]) -> dict[str, object]:
        return {"projectId": config["project_id"], "status": "REQUEST_RELEASE"}

    def create_session(self, task_id: str) -> dict[str, object]:
        config = self._get_config()
        enable_proxies = os.environ.get("BROWSERBASE_PROXIES", "true").lower() != "false"
        enable_advanced_stealth = os.environ.get("BROWSERBASE_ADVANCED_STEALTH", "false").lower() == "true"
        enable_keep_alive = os.environ.get("BROWSERBASE_KEEP_ALIVE", "true").lower() != "false"
        custom_timeout_ms = os.environ.get("BROWSERBASE_SESSION_TIMEOUT") or _config_session_timeout()

        session_config: dict[str, object] = {"projectId": config["project_id"]}
        if enable_keep_alive:
            session_config["keepAlive"] = True
        if custom_timeout_ms:
            try:
                timeout_val = int(custom_timeout_ms)
                if timeout_val > 0:
                    session_config["timeout"] = timeout_val
            except ValueError:
                logger.warning("Invalid BROWSERBASE_SESSION_TIMEOUT value: %s", custom_timeout_ms)
        if enable_proxies:
            session_config["proxies"] = True
        if enable_advanced_stealth:
            session_config["browserSettings"] = {"advancedStealth": True}

        url = f"{config['base_url']}/v1/sessions"
        headers = self._headers(config)
        response = self._post_create(url, headers, session_config)

        # 402 — paid features unavailable: drop keepAlive, then proxies, and retry.
        dropped = set()
        for key, warning in _PAID_FEATURE_FALLBACKS:
            if response.status_code == 402 and key in session_config:
                dropped.add(key)
                logger.warning(warning)
                session_config.pop(key)
                response = self._post_create(url, headers, session_config)
        self._check_created(response)

        session_data = response.json()
        session_name = self._session_name(task_id)
        features_enabled = {
            "basic_stealth": True,
            "proxies": enable_proxies and "proxies" not in dropped,
            "advanced_stealth": enable_advanced_stealth,
            "keep_alive": enable_keep_alive and "keepAlive" not in dropped,
            "custom_timeout": bool(custom_timeout_ms) and "timeout" in session_config,
        }
        feature_str = ", ".join(k for k, v in features_enabled.items() if v)
        logger.info("Created Browserbase session %s with features: %s", session_name, feature_str)
        return {
            "session_name": session_name,
            "bb_session_id": session_data["id"],
            "cdp_url": session_data["connectUrl"],
            "features": features_enabled,
        }

    def live_view(self, session_id: str) -> list[dict[str, str]]:
        """The session's open pages with a direct DevTools WebSocket for each, so a person can
        drive the same page from their own device (browser_handover). The URL carries a
        session-scoped token and no API key."""
        config = self._get_config()
        try:
            response = requests.get(
                f"{config['base_url']}/v1/sessions/{session_id}/debug",
                headers=self._headers(config), timeout=15, verify=requests_verify())
        except requests.RequestException as exc:
            raise RuntimeError(f"{self.label} API connection failed: {exc}") from exc
        if not response.ok:
            raise RuntimeError(f"{self.label} live view failed: HTTP {response.status_code}")
        pages = []
        for page in response.json().get("pages") or []:
            # debuggerFullscreenUrl is the hosted inspector; its ``wss`` query value is the
            # page's DevTools endpoint (already carrying ``?t=<token>``).
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(page.get("debuggerFullscreenUrl") or "")).query)
            endpoint = (query.get("wss") or [""])[0]
            if not endpoint.startswith("connect.browserbase.com/"):
                continue
            pages.append({"url": str(page.get("url") or ""), "title": str(page.get("title") or ""),
                          "ws_url": f"wss://{endpoint}"})
        return pages
