"""Send-only email delivery through an SMTP relay (Brevo, SendGrid).

Relays log in with a technical username (EMAIL_SMTP_USERNAME) that differs from the
From address, and a send-only instance has no IMAP mailbox (EMAIL_SEND_ONLY=true).
"""

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

RELAY_ENV = {
    "EMAIL_ADDRESS": "assistant@agemo-amo.com",
    "EMAIL_PASSWORD": "relay-key",
    "EMAIL_SMTP_HOST": "smtp-relay.brevo.com",
    "EMAIL_SMTP_USERNAME": "9a1b2c001@smtp-brevo.com",
    "EMAIL_IMAP_HOST": "",
    "EMAIL_SEND_ONLY": "true",
}


def _adapter(env):
    from gateway.config import PlatformConfig
    from plugins.platforms.email.adapter import EmailAdapter
    with patch.dict(os.environ, env, clear=False):
        return EmailAdapter(PlatformConfig(enabled=True))


class TestSmtpUsername(unittest.TestCase):

    def test_adapter_logs_in_with_the_relay_username_and_sends_from_the_address(self):
        adapter = _adapter(RELAY_ENV)
        with patch("smtplib.SMTP") as mock_smtp:
            server = mock_smtp.return_value
            adapter._send_email("secretary@agemo-amo.com", "Daily summary", None)
        server.login.assert_called_once_with("9a1b2c001@smtp-brevo.com", "relay-key")
        sent = server.send_message.call_args[0][0]
        self.assertEqual(sent["From"], "assistant@agemo-amo.com")

    def test_standalone_send_logs_in_with_the_relay_username(self):
        from plugins.platforms.email.adapter import _standalone_send
        with patch.dict(os.environ, RELAY_ENV, clear=False), patch("smtplib.SMTP") as mock_smtp:
            server = mock_smtp.return_value
            result = asyncio.run(_standalone_send(SimpleNamespace(extra={}), "secretary@agemo-amo.com", "Hello"))
        self.assertTrue(result["success"])
        server.login.assert_called_once_with("9a1b2c001@smtp-brevo.com", "relay-key")
        self.assertEqual(server.send_message.call_args[0][0]["From"], "assistant@agemo-amo.com")

    def test_login_defaults_to_the_address_without_a_username(self):
        adapter = _adapter({**RELAY_ENV, "EMAIL_SMTP_USERNAME": ""})
        with patch("smtplib.SMTP") as mock_smtp:
            adapter._send_email("secretary@agemo-amo.com", "Hi", None)
        mock_smtp.return_value.login.assert_called_once_with("assistant@agemo-amo.com", "relay-key")

    def test_first_message_has_a_plain_subject(self):
        from agent.i18n import t
        adapter = _adapter(RELAY_ENV)
        with patch("smtplib.SMTP") as mock_smtp:
            adapter._send_email("secretary@agemo-amo.com", "Daily summary", None)
        sent = mock_smtp.return_value.send_message.call_args[0][0]
        self.assertEqual(sent["Subject"], t("platform.email.standalone_subject"))


class TestSendOnly(unittest.TestCase):

    def test_connect_skips_imap_and_never_polls(self):
        adapter = _adapter(RELAY_ENV)
        with patch("imaplib.IMAP4_SSL") as mock_imap, patch("imaplib.IMAP4") as mock_plain, \
                patch("smtplib.SMTP") as mock_smtp:
            self.assertTrue(asyncio.run(adapter.connect()))
        mock_imap.assert_not_called()
        mock_plain.assert_not_called()
        mock_smtp.return_value.login.assert_called_once_with("9a1b2c001@smtp-brevo.com", "relay-key")
        self.assertIsNone(adapter._poll_task)
        self.assertTrue(adapter._running)

    def test_bad_relay_credentials_fail_without_retry(self):
        import smtplib
        adapter = _adapter(RELAY_ENV)
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad key")
            self.assertFalse(asyncio.run(adapter.connect()))
        self.assertEqual(adapter.fatal_error_code, "email_auth_error")
        self.assertFalse(adapter.fatal_error_retryable)
        self.assertIn("9a1b2c001@smtp-brevo.com", adapter.fatal_error_message or "")

    def test_missing_imap_host_is_still_an_error_without_send_only(self):
        adapter = _adapter({**RELAY_ENV, "EMAIL_SEND_ONLY": "false"})
        with patch("imaplib.IMAP4_SSL") as mock_imap:
            self.assertFalse(asyncio.run(adapter.connect()))
        mock_imap.assert_not_called()
        self.assertEqual(adapter.fatal_error_code, "email_missing_configuration")
        self.assertIn("EMAIL_IMAP_HOST", adapter.fatal_error_message or "")

    def test_requirements_accept_send_only_but_not_a_blank_mailbox(self):
        from plugins.platforms.email.adapter import check_email_requirements
        with patch.dict(os.environ, RELAY_ENV, clear=False):
            self.assertTrue(check_email_requirements())
        with patch.dict(os.environ, {**RELAY_ENV, "EMAIL_SEND_ONLY": ""}, clear=False):
            self.assertFalse(check_email_requirements())
        with patch.dict(os.environ, {**RELAY_ENV, "EMAIL_SMTP_HOST": ""}, clear=False):
            self.assertFalse(check_email_requirements())

    def test_env_enables_the_platform_with_a_home_address(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides
        env = {**RELAY_ENV, "EMAIL_HOME_ADDRESS": "secretary@agemo-amo.com"}
        with patch.dict(os.environ, env, clear=False):
            config = GatewayConfig()
            _apply_env_overrides(config)
        self.assertTrue(config.platforms[Platform.EMAIL].enabled)
        self.assertEqual(config.platforms[Platform.EMAIL].home_channel.chat_id, "secretary@agemo-amo.com")
        # The client's inbox only receives requested results, never restart notices.
        self.assertFalse(config.platforms[Platform.EMAIL].gateway_restart_notification)

    def test_a_mailbox_instance_keeps_its_restart_notices(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides
        env = {**RELAY_ENV, "EMAIL_SEND_ONLY": "", "EMAIL_IMAP_HOST": "imap.agemo-amo.com"}
        with patch.dict(os.environ, env, clear=False):
            config = GatewayConfig()
            _apply_env_overrides(config)
        self.assertTrue(config.platforms[Platform.EMAIL].gateway_restart_notification)

    def test_send_only_adapter_never_emits_diagnostics(self):
        adapter = _adapter(RELAY_ENV)
        self.assertFalse(adapter.warning_notifications_enabled())
        with patch("smtplib.SMTP") as mock_smtp:
            result = asyncio.run(adapter.emit_warning("secretary@agemo-amo.com", "disk almost full"))
        self.assertIsNone(result)
        mock_smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
