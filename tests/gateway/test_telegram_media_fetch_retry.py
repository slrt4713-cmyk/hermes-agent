"""Inbound Telegram media survives a short network outage instead of asking the user to resend."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

import plugins.platforms.telegram.adapter as telegram_adapter
from tests.gateway.test_telegram_group_gating import _make_adapter


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(telegram_adapter.asyncio, "sleep", fake_sleep)
    return sleeps


def _source(*outcomes):
    """A Telegram file source whose get_file() raises or succeeds in order."""
    file_obj = SimpleNamespace(file_path="voice/file_1.oga", download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg")))
    side_effects = [outcome if isinstance(outcome, Exception) else file_obj for outcome in outcomes]
    return SimpleNamespace(get_file=AsyncMock(side_effect=side_effects)), file_obj


def test_transient_timeouts_are_retried_until_the_file_arrives(_no_real_sleep):
    adapter = _make_adapter()
    source, file_obj = _source(TimedOut(), NetworkError("Connection reset"), "ok")

    got_file, data = asyncio.run(adapter._fetch_telegram_file(source))

    assert got_file is file_obj
    assert data == bytearray(b"ogg")
    assert source.get_file.await_count == 3
    assert _no_real_sleep == list(telegram_adapter._MEDIA_FETCH_RETRY_DELAYS[:2])


def test_bad_request_is_not_retried(_no_real_sleep):
    adapter = _make_adapter()
    source, _ = _source(BadRequest("File is too big"))

    with pytest.raises(BadRequest):
        asyncio.run(adapter._fetch_telegram_file(source))

    assert source.get_file.await_count == 1
    assert _no_real_sleep == []


def test_retries_stop_within_the_deadline(_no_real_sleep, monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(telegram_adapter.time, "monotonic", lambda: clock["now"])

    async def fake_sleep(delay):
        _no_real_sleep.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(telegram_adapter.asyncio, "sleep", fake_sleep)
    adapter = _make_adapter()
    source, _ = _source(*[TimedOut()] * 20)

    with pytest.raises(TimedOut):
        asyncio.run(adapter._fetch_telegram_file(source))

    assert sum(_no_real_sleep) <= telegram_adapter._MEDIA_FETCH_DEADLINE_SECONDS
    assert source.get_file.await_count == len(_no_real_sleep) + 1


def test_voice_download_recovers_without_asking_to_resend():
    adapter = _make_adapter()
    adapter._surface_media_cache_failure = AsyncMock()
    adapter._telegram_media_size_allowed = lambda source, label: (True, None)
    source, _ = _source(TimedOut(), "ok")
    event = SimpleNamespace(text="", media_urls=[], media_types=[])
    cache = AsyncMock(return_value="/opt/data/cache/audio/a.ogg")

    original = telegram_adapter.cache_audio_from_bytes_async
    telegram_adapter.cache_audio_from_bytes_async = cache
    try:
        asyncio.run(adapter._cache_inbound_av(SimpleNamespace(), event, source, "voice message", "voice", ".ogg", "audio/ogg"))
    finally:
        telegram_adapter.cache_audio_from_bytes_async = original

    adapter._surface_media_cache_failure.assert_not_awaited()
    assert event.media_urls == ["/opt/data/cache/audio/a.ogg"]
