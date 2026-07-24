"""Harness: in-container integration tests for S6ServiceManager.

The unit tests in tests/hermes_cli/test_service_manager.py exercise the
class against a tmp-path scandir with a stubbed ``subprocess.run``.
These tests run the real class inside a real container against the
real s6-svc / s6-svscanctl binaries, validating end-to-end.

Phase 3 only registers the service slot — it doesn't depend on the
gateway actually starting (the binary will refuse to start without a
valid profile config). The full register → start → supervised-restart
→ unregister cycle is covered by Phase 4 once profile create/delete
hooks land.

Every ``docker exec`` here runs as the unprivileged ``hermes`` user
(via :func:`docker_exec` in conftest); see the conftest module
docstring. ``/run/hermes-profile-services`` is owned by hermes and watched
by a nested s6-svscan process that also runs as hermes. The root
``/run/service`` tree stays inaccessible to the runtime user.
"""
from __future__ import annotations

import subprocess
import time

from tests.docker.conftest import docker_exec


_REGISTER_SCRIPT = """
import sys
sys.path.insert(0, "/opt/hermes")
from hermes_cli.service_manager import S6ServiceManager
S6ServiceManager().register_profile_gateway("phase3test")
# Don't worry about whether the gateway actually starts — we only care
# that the supervision slot was created. The gateway run script will
# likely error out (no profile config exists) but that's expected.
print("REGISTERED")
"""

_UNREGISTER_SCRIPT = """
import sys
sys.path.insert(0, "/opt/hermes")
from hermes_cli.service_manager import S6ServiceManager
S6ServiceManager().unregister_profile_gateway("phase3test")
print("UNREGISTERED")
"""

_NESTED_SUPERVISOR_PROBE = r"""
import pathlib
import subprocess
import time

scandir = pathlib.Path("/run/hermes-profile-services")
slot = scandir / "security-probe"
slot.mkdir()
run = slot / "run"
run.write_text(
    "#!/bin/sh\n"
    "id -u > /opt/data/s6-security-probe.uid\n"
    "exec sleep 120\n"
)
run.chmod(0o755)
subprocess.run(
    ["/command/s6-svscanctl", "-a", str(scandir)],
    check=True,
    timeout=5,
)
uid_file = pathlib.Path("/opt/data/s6-security-probe.uid")
observed_uid = ""
for _ in range(50):
    try:
        observed_uid = uid_file.read_text().strip()
    except FileNotFoundError:
        observed_uid = ""
    if observed_uid:
        break
    time.sleep(0.1)
print(observed_uid)
"""


def _exec(container: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return docker_exec(container, *args, timeout=timeout)


def _start_hardened_container(built_image: str, container_name: str) -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--read-only",
            "--tmpfs",
            "/run:rw,nosuid,nodev,exec,size=64m,mode=0755",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            "-e",
            "S6_READ_ONLY_ROOT=1",
            built_image,
            "sleep",
            "120",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


def test_runtime_cannot_register_a_root_s6_service(
    built_image: str, container_name: str,
) -> None:
    _start_hardened_container(built_image, container_name)
    time.sleep(3)

    root_slot = _exec(
        container_name,
        "mkdir",
        "/run/service/hermes-security-probe",
    )
    assert root_slot.returncode != 0, "runtime user can write the root s6 scandir"

    root_rescan = _exec(
        container_name,
        "/command/s6-svscanctl",
        "-a",
        "/run/service",
    )
    assert root_rescan.returncode != 0, "runtime user can control root s6-svscan"

    expected_uid = _exec(container_name, "id", "-u")
    probe = _exec(
        container_name,
        "python3",
        "-c",
        _NESTED_SUPERVISOR_PROBE,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == expected_uid.stdout.strip()


def test_s6_register_creates_service_dir_in_live_container(
    built_image: str, container_name: str,
) -> None:
    """S6ServiceManager.register_profile_gateway must create
    ``/run/hermes-profile-services/gateway-<profile>/`` and trigger s6-svscan rescan
    against the real s6 supervision tree."""
    _start_hardened_container(built_image, container_name)
    # Give the supervision tree a moment to come up.
    time.sleep(3)

    r = _exec(container_name, "python3", "-c", _REGISTER_SCRIPT, timeout=30)
    assert "REGISTERED" in r.stdout, (
        f"register failed: stderr={r.stderr!r} stdout={r.stdout!r}"
    )

    # Service directory exists with the expected structure.
    r = _exec(
        container_name,
        "test",
        "-d",
        "/run/hermes-profile-services/gateway-phase3test",
    )
    assert r.returncode == 0, "service directory not created"

    r = _exec(
        container_name,
        "test",
        "-f",
        "/run/hermes-profile-services/gateway-phase3test/run",
    )
    assert r.returncode == 0, "run script not created"

    r = _exec(container_name, "test", "-f",
              "/run/hermes-profile-services/gateway-phase3test/log/run")
    assert r.returncode == 0, "log/run script not created"

    # s6-svscan picked it up — s6-svstat works against the dir.
    # `docker exec` doesn't put /command/ on PATH (only the supervision
    # tree does), so call s6-svstat by absolute path.
    r = _exec(container_name, "/command/s6-svstat",
              "/run/hermes-profile-services/gateway-phase3test")
    assert r.returncode == 0, f"s6-svstat failed: {r.stderr or r.stdout}"

    # list_profile_gateways picks it up.
    r = _exec(container_name, "python3", "-c", (
        "from hermes_cli.service_manager import S6ServiceManager;"
        "print(S6ServiceManager().list_profile_gateways())"
    ))
    assert "phase3test" in r.stdout, f"list output: {r.stdout!r}"


def test_s6_unregister_removes_service_dir_in_live_container(
    built_image: str, container_name: str,
) -> None:
    """unregister_profile_gateway must stop the service, remove the
    directory, and trigger s6-svscan rescan so the supervise process
    is dropped."""
    _start_hardened_container(built_image, container_name)
    time.sleep(3)

    # First register so we have something to unregister.
    r = _exec(container_name, "python3", "-c", _REGISTER_SCRIPT, timeout=30)
    assert "REGISTERED" in r.stdout

    # Then unregister.
    r = _exec(container_name, "python3", "-c", _UNREGISTER_SCRIPT, timeout=30)
    assert "UNREGISTERED" in r.stdout, (
        f"unregister failed: stderr={r.stderr!r} stdout={r.stdout!r}"
    )

    # Directory is gone.
    r = _exec(
        container_name,
        "test",
        "-d",
        "/run/hermes-profile-services/gateway-phase3test",
    )
    assert r.returncode != 0, "service directory still exists after unregister"

    # list_profile_gateways no longer includes it.
    r = _exec(container_name, "python3", "-c", (
        "from hermes_cli.service_manager import S6ServiceManager;"
        "print(S6ServiceManager().list_profile_gateways())"
    ))
    assert "phase3test" not in r.stdout
