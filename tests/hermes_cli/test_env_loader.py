import importlib
import os
import sys

from hermes_cli.env_loader import load_hermes_dotenv


def test_user_env_overrides_stale_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://new.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"


def test_project_env_overrides_stale_shell_values_when_user_env_missing(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://project.example/v1"


def test_project_env_is_sanitized_before_loading(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=0123456789:test"
        "ANTHROPIC_API_KEY=sk-ant-test123\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("TELEGRAM_BOT_TOKEN") == "0123456789:test"
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-test123"


def test_user_env_takes_precedence_over_project_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    user_env = home / ".env"
    project_env = tmp_path / ".env"
    user_env.write_text("OPENAI_BASE_URL=https://user.example/v1\n", encoding="utf-8")
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\nOPENAI_API_KEY=project-key\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [user_env, project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://user.example/v1"
    assert os.getenv("OPENAI_API_KEY") == "project-key"


def test_null_bytes_in_user_env_are_stripped(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # Null bytes can be introduced when copy-pasting API keys.
    env_file.write_text("GLM_API_KEY=abc\x00\x00\nOPENAI_API_KEY=sk-123\n", encoding="utf-8")

    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("GLM_API_KEY") == "abc"
    assert os.getenv("OPENAI_API_KEY") == "sk-123"


def test_dotenv_cannot_override_process_plugin_policy(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text(
        "HERMES_REQUIRED_BUNDLED_PLUGINS=attacker-plugin\n"
        "HERMES_REQUIRED_BUNDLED_PLUGINS_ROOT=/tmp/attacker-root\n"
        "HERMES_BUNDLED_PLUGINS=/tmp/attacker-plugins\n"
        "HERMES_AUDIT_KEY_FILE=/tmp/attacker.key\n"
        "HERMES_AUDIT_SOCKET=/tmp/attacker.sock\n"
        "HERMES_HOSTED_IMMUTABLE_RUNTIME=0\n"
        "HTTPS_PROXY=http://attacker.invalid:8080\n",
        encoding="utf-8",
    )
    expected = {
        "HERMES_REQUIRED_BUNDLED_PLUGINS": "hermes-audit",
        "HERMES_REQUIRED_BUNDLED_PLUGINS_ROOT": "/opt/hermes/plugins",
        "HERMES_BUNDLED_PLUGINS": "/opt/hermes/plugins",
        "HERMES_AUDIT_KEY_FILE": "/opt/hermes-example/audit.key",
        "HERMES_AUDIT_SOCKET": "/run/hermes-agent-audit/events.sock",
        "HERMES_HOSTED_IMMUTABLE_RUNTIME": "1",
        "HTTPS_PROXY": "http://host.docker.internal:8888",
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)

    load_hermes_dotenv(hermes_home=home)

    assert {name: os.environ.get(name) for name in expected} == expected


def test_dotenv_cannot_define_process_plugin_policy(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "HERMES_REQUIRED_BUNDLED_PLUGINS=attacker-plugin\n"
        "HERMES_REQUIRED_BUNDLED_PLUGINS_ROOT=/tmp/attacker-root\n"
        "HERMES_BUNDLED_PLUGINS=/tmp/attacker-plugins\n"
        "HERMES_AUDIT_KEY_FILE=/tmp/attacker.key\n"
        "HERMES_AUDIT_SOCKET=/tmp/attacker.sock\n"
        "HERMES_HOSTED_IMMUTABLE_RUNTIME=0\n"
        "HTTPS_PROXY=http://attacker.invalid:8080\n",
        encoding="utf-8",
    )
    policy_names = (
        "HERMES_REQUIRED_BUNDLED_PLUGINS",
        "HERMES_REQUIRED_BUNDLED_PLUGINS_ROOT",
        "HERMES_BUNDLED_PLUGINS",
        "HERMES_AUDIT_KEY_FILE",
        "HERMES_AUDIT_SOCKET",
        "HERMES_HOSTED_IMMUTABLE_RUNTIME",
        "HTTPS_PROXY",
    )
    for name in policy_names:
        monkeypatch.delenv(name, raising=False)

    load_hermes_dotenv(hermes_home=home)

    assert all(name not in os.environ for name in policy_names)


def test_main_import_applies_user_env_over_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://new.example/v1\nHERMES_INFERENCE_PROVIDER=custom\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")

    sys.modules.pop("hermes_cli.main", None)
    importlib.import_module("hermes_cli.main")

    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"
    assert os.getenv("HERMES_INFERENCE_PROVIDER") == "custom"
