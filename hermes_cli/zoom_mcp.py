"""Read-only Zoom MCP connector for meetings and cloud transcripts."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE_URL = "https://api.zoom.us/v2"
TOKEN_URL = "https://zoom.us/oauth/token"
REQUEST_TIMEOUT_SECONDS = 30.0
TOKEN_EXPIRY_SKEW_SECONDS = 30
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 200_000
DEFAULT_TRANSCRIPT_CHARS = 100_000
MAX_REDIRECTS = 5

MEETING_TYPES = {
    "scheduled",
    "live",
    "upcoming",
    "upcoming_meetings",
    "previous_meetings",
}

mcp = FastMCP("Hermes Zoom")


class ZoomConnectorError(RuntimeError):
    """Safe connector error whose message never contains a Zoom response body."""


@dataclass(frozen=True)
class ZoomCredentials:
    account_id: str
    client_id: str
    client_secret: str
    user_id: str

    @classmethod
    def from_env(cls) -> ZoomCredentials:
        values = {
            name: os.environ.get(name, "").strip()
            for name in (
                "ZOOM_ACCOUNT_ID",
                "ZOOM_CLIENT_ID",
                "ZOOM_CLIENT_SECRET",
                "ZOOM_USER_ID",
            )
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ZoomConnectorError(
                f"Missing required Zoom configuration: {', '.join(missing)}"
            )
        return cls(
            account_id=values["ZOOM_ACCOUNT_ID"],
            client_id=values["ZOOM_CLIENT_ID"],
            client_secret=values["ZOOM_CLIENT_SECRET"],
            user_id=values["ZOOM_USER_ID"],
        )


def _page_size(value: int) -> int:
    if not 1 <= value <= 300:
        raise ZoomConnectorError("page_size must be between 1 and 300")
    return value


def _transcript_window(offset: int, limit: int) -> tuple[int, int]:
    if offset < 0:
        raise ZoomConnectorError("offset must be zero or greater")
    if not 1 <= limit <= MAX_TRANSCRIPT_CHARS:
        raise ZoomConnectorError(
            f"limit must be between 1 and {MAX_TRANSCRIPT_CHARS}"
        )
    return offset, limit


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ZoomConnectorError(f"{field} must use YYYY-MM-DD format") from exc


def _recording_dates(
    from_date: str | None, to_date: str | None
) -> tuple[str | None, str | None]:
    start = _parse_date(from_date, "from_date")
    end = _parse_date(to_date, "to_date")
    if start and end:
        if start > end:
            raise ZoomConnectorError("from_date must not be after to_date")
        if (end - start).days > 31:
            raise ZoomConnectorError("recording date range must not exceed 31 days")
    return (
        start.isoformat() if start else None,
        end.isoformat() if end else None,
    )


def _meeting_path_id(meeting_id: str | int) -> str:
    raw = str(meeting_id).strip()
    if not raw:
        raise ZoomConnectorError("meeting_id is required")
    encoded = quote(raw, safe="")
    if raw.startswith("/") or "//" in raw:
        encoded = quote(encoded, safe="")
    return encoded


def _pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


def _meeting_summary(meeting: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
    keys = (
        "id",
        "uuid",
        "topic",
        "type",
        "start_time",
        "duration",
        "timezone",
        "host_id",
        "host_email",
        "created_at",
    )
    if detailed:
        keys += ("agenda", "occurrences")
    return _pick(meeting, keys)


def _recording_file_summary(recording_file: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        recording_file,
        (
            "id",
            "meeting_id",
            "file_type",
            "file_extension",
            "file_size",
            "status",
            "recording_type",
            "recording_start",
            "recording_end",
        ),
    )


def _recording_summary(recording: dict[str, Any]) -> dict[str, Any]:
    result = _pick(
        recording,
        (
            "id",
            "uuid",
            "topic",
            "type",
            "start_time",
            "duration",
            "timezone",
            "host_id",
            "host_email",
            "recording_count",
            "total_size",
        ),
    )
    files = recording.get("recording_files")
    if isinstance(files, list):
        result["recording_files"] = [
            _recording_file_summary(item)
            for item in files
            if isinstance(item, dict)
        ]
    return result


def _zoom_download_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and (host == "zoom.us" or host.endswith(".zoom.us"))
    )


class ZoomClient:
    def __init__(
        self,
        credentials: ZoomCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base_url: str = API_BASE_URL,
        token_url: str = TOKEN_URL,
    ) -> None:
        self._credentials = credentials
        self._api_base_url = api_base_url.rstrip("/")
        self._token_url = token_url
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            try:
                response = await self._http.post(
                    self._token_url,
                    params={
                        "grant_type": "account_credentials",
                        "account_id": self._credentials.account_id,
                    },
                    auth=httpx.BasicAuth(
                        self._credentials.client_id,
                        self._credentials.client_secret,
                    ),
                )
            except httpx.HTTPError as exc:
                raise ZoomConnectorError("Zoom authentication request failed") from exc
            if response.status_code != 200:
                raise ZoomConnectorError(
                    f"Zoom authentication failed with HTTP {response.status_code}"
                )
            try:
                payload = response.json()
                token = payload["access_token"]
                expires_in = int(payload.get("expires_in", 3600))
            except (KeyError, TypeError, ValueError) as exc:
                raise ZoomConnectorError(
                    "Zoom authentication returned an invalid response"
                ) from exc
            if not isinstance(token, str) or not token:
                raise ZoomConnectorError(
                    "Zoom authentication returned an invalid access token"
                )
            self._token = token
            self._token_expires_at = time.monotonic() + max(
                1, expires_in - TOKEN_EXPIRY_SKEW_SECONDS
            )
            return token

    async def _request_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        clean_params = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
        for attempt in range(2):
            token = await self._access_token()
            try:
                response = await self._http.get(
                    f"{self._api_base_url}{path}",
                    params=clean_params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise ZoomConnectorError("Zoom API request failed") from exc
            if response.status_code == 401 and attempt == 0:
                self._token = None
                self._token_expires_at = 0.0
                continue
            if not 200 <= response.status_code < 300:
                raise ZoomConnectorError(
                    f"Zoom API request failed with HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ZoomConnectorError(
                    "Zoom API returned an invalid JSON response"
                ) from exc
            if not isinstance(payload, dict):
                raise ZoomConnectorError("Zoom API returned an invalid response")
            return payload
        raise ZoomConnectorError("Zoom API authentication failed")

    async def list_meetings(
        self,
        *,
        meeting_type: str = "scheduled",
        page_size: int = 30,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        if meeting_type not in MEETING_TYPES:
            raise ZoomConnectorError(
                f"meeting_type must be one of: {', '.join(sorted(MEETING_TYPES))}"
            )
        user_id = quote(self._credentials.user_id, safe="")
        payload = await self._request_json(
            f"/users/{user_id}/meetings",
            params={
                "type": meeting_type,
                "page_size": _page_size(page_size),
                "next_page_token": next_page_token,
            },
        )
        return {
            **_pick(
                payload,
                (
                    "page_count",
                    "page_number",
                    "page_size",
                    "total_records",
                    "next_page_token",
                ),
            ),
            "meetings": [
                _meeting_summary(item)
                for item in payload.get("meetings", [])
                if isinstance(item, dict)
            ],
        }

    async def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        payload = await self._request_json(
            f"/meetings/{_meeting_path_id(meeting_id)}"
        )
        return _meeting_summary(payload, detailed=True)

    async def list_recordings(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        page_size: int = 30,
        next_page_token: str | None = None,
    ) -> dict[str, Any]:
        start, end = _recording_dates(from_date, to_date)
        user_id = quote(self._credentials.user_id, safe="")
        payload = await self._request_json(
            f"/users/{user_id}/recordings",
            params={
                "from": start,
                "to": end,
                "page_size": _page_size(page_size),
                "next_page_token": next_page_token,
            },
        )
        return {
            **_pick(
                payload,
                (
                    "from",
                    "to",
                    "page_count",
                    "page_size",
                    "total_records",
                    "next_page_token",
                ),
            ),
            "meetings": [
                _recording_summary(item)
                for item in payload.get("meetings", [])
                if isinstance(item, dict)
            ],
        }

    async def get_recording(self, meeting_id: str) -> dict[str, Any]:
        payload = await self._request_json(
            f"/meetings/{_meeting_path_id(meeting_id)}/recordings"
        )
        return _recording_summary(payload)

    async def _download_transcript(self, url: str) -> str:
        current_url = url
        for _ in range(MAX_REDIRECTS + 1):
            if not _zoom_download_url(current_url):
                raise ZoomConnectorError(
                    "Zoom returned an unsafe transcript download URL"
                )
            token = await self._access_token()
            try:
                async with self._http.stream(
                    "GET",
                    current_url,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ZoomConnectorError(
                                "Zoom transcript download returned an invalid redirect"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code != 200:
                        raise ZoomConnectorError(
                            "Zoom transcript download failed with HTTP "
                            f"{response.status_code}"
                        )
                    declared_size = response.headers.get("content-length")
                    if declared_size:
                        try:
                            if int(declared_size) > MAX_DOWNLOAD_BYTES:
                                raise ZoomConnectorError(
                                    "Zoom transcript exceeds the download size limit"
                                )
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    downloaded = 0
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_BYTES:
                            raise ZoomConnectorError(
                                "Zoom transcript exceeds the download size limit"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise ZoomConnectorError("Zoom transcript download failed") from exc
        raise ZoomConnectorError("Zoom transcript download redirected too many times")

    async def get_transcript(
        self,
        meeting_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_TRANSCRIPT_CHARS,
    ) -> dict[str, Any]:
        offset, limit = _transcript_window(offset, limit)
        metadata = await self._request_json(
            f"/meetings/{_meeting_path_id(meeting_id)}/transcript"
        )
        if metadata.get("can_download") is not True:
            return {
                **_pick(
                    metadata,
                    (
                        "account_id",
                        "auto_delete",
                        "auto_delete_date",
                        "can_download",
                        "download_restriction_reason",
                    ),
                ),
                "meeting_id": str(meeting_id),
                "content": "",
                "total_chars": 0,
                "offset": offset,
                "next_offset": None,
                "complete": False,
            }
        download_url = metadata.get("download_url")
        if not isinstance(download_url, str) or not download_url:
            raise ZoomConnectorError(
                "Zoom transcript response did not include a download URL"
            )
        content = await self._download_transcript(download_url)
        chunk = content[offset : offset + limit]
        next_offset = offset + len(chunk)
        complete = next_offset >= len(content)
        return {
            **_pick(
                metadata,
                ("account_id", "auto_delete", "auto_delete_date", "can_download"),
            ),
            "meeting_id": str(meeting_id),
            "content": chunk,
            "total_chars": len(content),
            "offset": offset,
            "next_offset": None if complete else next_offset,
            "complete": complete,
        }


_zoom_client: ZoomClient | None = None


def _client() -> ZoomClient:
    global _zoom_client
    if _zoom_client is None:
        _zoom_client = ZoomClient(ZoomCredentials.from_env())
    return _zoom_client


@mcp.tool()
async def meetings_list(
    meeting_type: str = "scheduled",
    page_size: int = 30,
    next_page_token: str | None = None,
) -> dict[str, Any]:
    """List a Zoom user's scheduled meetings without exposing join credentials."""
    return await _client().list_meetings(
        meeting_type=meeting_type,
        page_size=page_size,
        next_page_token=next_page_token,
    )


@mcp.tool()
async def meetings_get(meeting_id: str) -> dict[str, Any]:
    """Get safe metadata for one Zoom meeting."""
    return await _client().get_meeting(meeting_id)


@mcp.tool()
async def recordings_list(
    from_date: str | None = None,
    to_date: str | None = None,
    page_size: int = 30,
    next_page_token: str | None = None,
) -> dict[str, Any]:
    """List cloud recordings and transcript file identifiers for a Zoom user."""
    return await _client().list_recordings(
        from_date=from_date,
        to_date=to_date,
        page_size=page_size,
        next_page_token=next_page_token,
    )


@mcp.tool()
async def recordings_get(meeting_id: str) -> dict[str, Any]:
    """Get safe cloud recording metadata for one Zoom meeting."""
    return await _client().get_recording(meeting_id)


@mcp.tool()
async def recordings_transcript(
    meeting_id: str,
    offset: int = 0,
    limit: int = DEFAULT_TRANSCRIPT_CHARS,
) -> dict[str, Any]:
    """Download one Zoom transcript as a bounded VTT text chunk."""
    return await _client().get_transcript(
        meeting_id,
        offset=offset,
        limit=limit,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
