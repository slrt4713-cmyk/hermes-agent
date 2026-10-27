"""Email env -> gateway config bridge (``PlatformEntry.env_overrides``)."""

from gateway.config import Platform
from gateway.platform_env import EnvCred, getenv, home_step
from utils import is_truthy_value

ENV_ENABLE_CREDENTIALS = ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST")


def _send_only_lane(_config, platform_config) -> None:
    """A send-only mailbox only carries requested results: no restart or shutdown notices."""
    if is_truthy_value(getenv("EMAIL_SEND_ONLY")):
        platform_config.gateway_restart_notification = False


ENV_OVERRIDES = (
    EnvCred(
        # EMAIL_SEND_ONLY stands in for EMAIL_IMAP_HOST (send-only delivery, no inbound mailbox).
        Platform.EMAIL, ("EMAIL_ADDRESS", "EMAIL_PASSWORD", ("EMAIL_IMAP_HOST", "EMAIL_SEND_ONLY"), "EMAIL_SMTP_HOST"),
        fixed=(("address", "EMAIL_ADDRESS"), ("imap_host", "EMAIL_IMAP_HOST"), ("smtp_host", "EMAIL_SMTP_HOST")),
        then=_send_only_lane,
    ),
    home_step(Platform.EMAIL, "EMAIL_HOME_ADDRESS"),
)

# Authorization env names (``PlatformEntry`` fields). Core reads them from this file when the
# live entry lacks them: a user plugin overriding this platform, safe mode, the plugin disabled.
AUTHZ = {
    "allowed_users_env": "EMAIL_ALLOWED_USERS",
    "allow_all_env": "EMAIL_ALLOW_ALL_USERS",
    "allowlist_suppresses_pairing": True,
}
