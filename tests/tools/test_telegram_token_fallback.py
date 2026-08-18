"""Standalone Telegram token resolution for hosted cron delivery."""

import asyncio

from tools.send_message_tool import _resolve_telegram_token, _send_telegram


def test_empty_token_uses_process_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:from-env")
    assert _resolve_telegram_token("") == "123456:from-env"
    assert _resolve_telegram_token(None) == "123456:from-env"
    assert _resolve_telegram_token("  explicit  ") == "explicit"


def test_empty_token_uses_hosted_s6_file(tmp_path, monkeypatch):
    from hermes_cli import env_loader

    s6_dir = tmp_path / "s6"
    s6_dir.mkdir()
    (s6_dir / "TELEGRAM_BOT_TOKEN").write_text("123456:from-s6", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOSTED_IMMUTABLE_RUNTIME", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(env_loader, "_HOSTED_CONTAINER_ENV_DIR", s6_dir)

    assert _resolve_telegram_token("") == "123456:from-s6"


def test_missing_token_returns_error_not_botfather(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_HOSTED_IMMUTABLE_RUNTIME", raising=False)

    result = asyncio.run(_send_telegram("", "123", "hello"))

    assert result == {"error": "Telegram bot token is missing"}
