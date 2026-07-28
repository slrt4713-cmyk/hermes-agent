from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import hermes_cli.zoom_mcp as zoom_mcp
from hermes_cli.zoom_mcp import (
    ZoomClient,
    ZoomConnectorError,
    ZoomCredentials,
    _meeting_path_id,
    mcp,
)
from tools.blueprints import blueprint_to_job_spec, parse_blueprint

REPO_ROOT = Path(__file__).resolve().parents[2]


def credentials() -> ZoomCredentials:
    return ZoomCredentials(
        account_id="account-id",
        client_id="client-id",
        client_secret="client-secret",
        user_id="host@example.com",
    )


@pytest.mark.asyncio
async def test_exposes_only_the_five_read_only_tools() -> None:
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == {
        "meetings_list",
        "meetings_get",
        "recordings_list",
        "recordings_get",
        "recordings_transcript",
    }


def test_meeting_capture_is_an_hourly_local_blueprint() -> None:
    skill = (
        REPO_ROOT / "skills" / "productivity" / "meeting-capture" / "SKILL.md"
    ).read_text(encoding="utf-8")
    spec = parse_blueprint(skill)

    assert spec is not None
    assert spec.schedule == "0 * * * *"
    assert spec.deliver == "local"
    assert spec.no_agent is False
    job = blueprint_to_job_spec(spec)
    assert job["skills"] == ["meeting-capture"]
    assert "[SILENT]" in job["prompt"]


@pytest.mark.asyncio
async def test_authenticates_once_and_sanitizes_meeting_metadata() -> None:
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            assert request.url.params["grant_type"] == "account_credentials"
            assert request.url.params["account_id"] == "account-id"
            return httpx.Response(
                200,
                json={"access_token": "zoom-token", "expires_in": 3600},
            )
        assert request.headers["authorization"] == "Bearer zoom-token"
        return httpx.Response(
            200,
            json={
                "total_records": 1,
                "meetings": [
                    {
                        "id": 123,
                        "uuid": "uuid",
                        "topic": "Investor update",
                        "join_url": "https://zoom.us/private",
                        "start_url": "https://zoom.us/host-secret",
                        "password": "secret",
                    }
                ],
            },
        )

    client = ZoomClient(
        credentials(),
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await client.list_meetings()
        second = await client.list_meetings()
    finally:
        await client.aclose()

    assert token_calls == 1
    assert first == second
    assert first["meetings"] == [
        {"id": 123, "uuid": "uuid", "topic": "Investor update"}
    ]
    serialized = json.dumps(first)
    assert "private" not in serialized
    assert "secret" not in serialized


def test_meeting_uuid_encoding_matches_zoom_rules() -> None:
    assert _meeting_path_id("123456789") == "123456789"
    assert _meeting_path_id("abc+def==") == "abc%2Bdef%3D%3D"
    assert _meeting_path_id("/abc==") == "%252Fabc%253D%253D"
    assert _meeting_path_id("abc//def") == "abc%252F%252Fdef"


@pytest.mark.asyncio
async def test_lists_recordings_with_safe_transcript_file_metadata() -> None:
    observed_path = ""
    observed_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_path, observed_query
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        observed_path = request.url.path
        observed_query = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "from": "2026-07-01",
                "to": "2026-07-28",
                "meetings": [
                    {
                        "uuid": "meeting-uuid",
                        "topic": "Board",
                        "recording_play_passcode": "do-not-return",
                        "recording_files": [
                            {
                                "id": "transcript-id",
                                "file_type": "TRANSCRIPT",
                                "file_extension": "VTT",
                                "status": "completed",
                                "download_url": "https://zoom.us/private",
                            }
                        ],
                    }
                ],
            },
        )

    client = ZoomClient(credentials(), transport=httpx.MockTransport(handler))
    try:
        result = await client.list_recordings(
            from_date="2026-07-01",
            to_date="2026-07-28",
            page_size=50,
        )
    finally:
        await client.aclose()

    assert observed_path == "/v2/users/host@example.com/recordings"
    assert observed_query == {
        "from": "2026-07-01",
        "to": "2026-07-28",
        "page_size": "50",
    }
    assert result["meetings"][0]["recording_files"] == [
        {
            "id": "transcript-id",
            "file_type": "TRANSCRIPT",
            "file_extension": "VTT",
            "status": "completed",
        }
    ]
    assert "do-not-return" not in json.dumps(result)
    assert "download_url" not in json.dumps(result)


@pytest.mark.asyncio
async def test_downloads_and_pages_transcript_without_returning_url() -> None:
    transcript = "WEBVTT\n\n00:00.000 --> 00:02.000\nSimon: Hello"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        if request.url.path == "/v2/meetings/meeting-uuid/transcript":
            return httpx.Response(
                200,
                json={
                    "account_id": "account-id",
                    "can_download": True,
                    "download_url": "https://us02web.zoom.us/rec/transcript.vtt",
                },
            )
        if request.url.path == "/rec/transcript.vtt":
            assert request.headers["authorization"] == "Bearer token"
            return httpx.Response(
                200,
                content=transcript.encode(),
                headers={"content-type": "text/vtt"},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = ZoomClient(credentials(), transport=httpx.MockTransport(handler))
    try:
        result = await client.get_transcript("meeting-uuid", limit=20)
        remainder = await client.get_transcript(
            "meeting-uuid",
            offset=result["next_offset"],
            limit=200,
        )
    finally:
        await client.aclose()

    assert result["content"] == transcript[:20]
    assert result["complete"] is False
    assert result["next_offset"] == 20
    assert remainder["content"] == transcript[20:]
    assert remainder["complete"] is True
    assert remainder["next_offset"] is None
    assert "download_url" not in result


@pytest.mark.asyncio
async def test_rejects_non_zoom_transcript_download_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        return httpx.Response(
            200,
            json={
                "can_download": True,
                "download_url": "https://attacker.example/transcript.vtt",
            },
        )

    client = ZoomClient(credentials(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ZoomConnectorError, match="unsafe"):
            await client.get_transcript("meeting-uuid")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stops_streaming_when_transcript_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zoom_mcp, "MAX_DOWNLOAD_BYTES", 10)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        if request.url.path == "/v2/meetings/meeting-uuid/transcript":
            return httpx.Response(
                200,
                json={
                    "can_download": True,
                    "download_url": "https://us02web.zoom.us/rec/transcript.vtt",
                },
            )
        return httpx.Response(200, stream=httpx.ByteStream(b"01234567890"))

    client = ZoomClient(credentials(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ZoomConnectorError, match="download size limit"):
            await client.get_transcript("meeting-uuid")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_api_errors_do_not_expose_zoom_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        return httpx.Response(
            403,
            json={"message": "private tenant detail", "secret": "leaked-secret"},
        )

    client = ZoomClient(credentials(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ZoomConnectorError) as error:
            await client.get_meeting("123")
    finally:
        await client.aclose()

    assert str(error.value) == "Zoom API request failed with HTTP 403"
    assert "private tenant detail" not in str(error.value)
    assert "leaked-secret" not in str(error.value)


def test_credentials_require_account_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ZOOM_ACCOUNT_ID",
        "ZOOM_CLIENT_ID",
        "ZOOM_CLIENT_SECRET",
        "ZOOM_USER_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ZoomConnectorError, match="ZOOM_USER_ID"):
        ZoomCredentials.from_env()


@pytest.mark.asyncio
async def test_recording_range_is_bounded() -> None:
    client = ZoomClient(
        credentials(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500)
        ),
    )
    try:
        with pytest.raises(ZoomConnectorError, match="31 days"):
            await client.list_recordings(
                from_date="2026-01-01",
                to_date="2026-03-01",
            )
    finally:
        await client.aclose()
