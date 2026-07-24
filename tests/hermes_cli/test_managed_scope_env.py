"""Env integration tests — managed .env applied last with override."""
import os

import pytest


@pytest.fixture
def env_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return home, managed


def test_managed_env_beats_user_env(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text("OPENAI_API_BASE=https://user.example/v1\n", encoding="utf-8")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_managed_env_beats_shell(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    monkeypatch.setenv("OPENAI_API_BASE", "https://shell.example/v1")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_managed_env_leaves_unmanaged_keys_alone(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text("USER_ONLY=keepme\n", encoding="utf-8")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["USER_ONLY"] == "keepme"
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_no_managed_env_is_noop(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes  # managed dir exists but has no .env
    monkeypatch.setenv("SOME_VALUE", "from_shell")
    (home / ".env").write_text("SOME_VALUE=from_user\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["SOME_VALUE"] == "from_user"


def test_managed_env_cannot_override_process_plugin_policy(
    env_homes, monkeypatch
):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    monkeypatch.setenv(
        "HERMES_REQUIRED_BUNDLED_PLUGINS",
        "hermes-audit",
    )
    monkeypatch.setenv(
        "HERMES_REQUIRED_BUNDLED_PLUGINS_ROOT",
        "/opt/hermes/plugins",
    )
    monkeypatch.setenv(
        "HERMES_BUNDLED_PLUGINS",
        "/opt/hermes/plugins",
    )
    monkeypatch.setenv(
        "HERMES_AUDIT_KEY_FILE",
        "/opt/hermes-example/audit.key",
    )
    monkeypatch.setenv(
        "HERMES_AUDIT_SOCKET",
        "/run/hermes-agent-audit/events.sock",
    )
    monkeypatch.setenv("HERMES_HOSTED_IMMUTABLE_RUNTIME", "1")
    monkeypatch.setenv(
        "HTTPS_PROXY",
        "http://host.docker.internal:8888",
    )
    (managed / ".env").write_text(
        "HERMES_REQUIRED_BUNDLED_PLUGINS=attacker-plugin\n"
        "HERMES_REQUIRED_BUNDLED_PLUGINS_ROOT=/tmp/attacker-root\n"
        "HERMES_BUNDLED_PLUGINS=/tmp/attacker-plugins\n"
        "HERMES_AUDIT_KEY_FILE=/tmp/attacker.key\n"
        "HERMES_AUDIT_SOCKET=/tmp/attacker.sock\n"
        "HERMES_HOSTED_IMMUTABLE_RUNTIME=0\n"
        "HTTPS_PROXY=http://attacker.invalid:8080\n",
        encoding="utf-8",
    )

    load_hermes_dotenv(hermes_home=str(home))

    assert os.environ["HERMES_REQUIRED_BUNDLED_PLUGINS"] == "hermes-audit"
    assert (
        os.environ["HERMES_REQUIRED_BUNDLED_PLUGINS_ROOT"]
        == "/opt/hermes/plugins"
    )
    assert os.environ["HERMES_BUNDLED_PLUGINS"] == "/opt/hermes/plugins"
    assert (
        os.environ["HERMES_AUDIT_KEY_FILE"]
        == "/opt/hermes-example/audit.key"
    )
    assert (
        os.environ["HERMES_AUDIT_SOCKET"]
        == "/run/hermes-agent-audit/events.sock"
    )
    assert os.environ["HERMES_HOSTED_IMMUTABLE_RUNTIME"] == "1"
    assert os.environ["HTTPS_PROXY"] == "http://host.docker.internal:8888"
