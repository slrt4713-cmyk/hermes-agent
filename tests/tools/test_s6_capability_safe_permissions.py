from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISE_PERMS = REPO_ROOT / "docker" / "cont-init.d" / "015-supervise-perms"
RECONCILE_PROFILES = (
    REPO_ROOT / "docker" / "cont-init.d" / "02-reconcile-profiles"
)


def test_static_supervise_modes_are_set_after_ownership_changes() -> None:
    script = SUPERVISE_PERMS.read_text(encoding="utf-8")

    supervise_mode = 'chmod 0710 "$svc/supervise"'
    supervise_owner = 'chown -R root:hermes "$svc/supervise"'
    event_mode = 'chmod 03730 "$svc/event"'
    event_owner = 'chown root:hermes "$svc/event"'

    assert script.index(supervise_owner) < script.index(supervise_mode)
    assert script.index(event_owner) < script.index(event_mode)
    assert 'chmod 03730 "$svc/supervise/event"' in script


def test_profile_scandir_mode_is_set_before_ownership_changes() -> None:
    script = RECONCILE_PROFILES.read_text(encoding="utf-8")

    assert script.index('chmod 0700 "$profile_scandir"') < script.index(
        'chown hermes:hermes "$profile_scandir"'
    )
