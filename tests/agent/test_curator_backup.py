"""Tests for agent/curator_backup.py — snapshot + rollback of the skills tree."""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def backup_env(monkeypatch, tmp_path):
    """Isolate HERMES_HOME + reload modules so every test starts clean."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CURATOR_STATE_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Reload so get_hermes_home picks up the env var fresh.
    import hermes_constants
    importlib.reload(hermes_constants)
    from agent import curator_backup
    importlib.reload(curator_backup)
    return {"home": home, "skills": home / "skills", "cb": curator_backup}


def _write_skill(skills_dir: Path, name: str, body: str = "body") -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: t\nversion: 1.0\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return d


def _add_tar_file(
    archive: tarfile.TarFile,
    name: str,
    content: bytes,
) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    archive.addfile(member, io.BytesIO(content))


# ---------------------------------------------------------------------------
# snapshot_skills
# ---------------------------------------------------------------------------

def test_snapshot_creates_tarball_and_manifest(backup_env):
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    _write_skill(backup_env["skills"], "beta")

    snap = cb.snapshot_skills(reason="test")
    assert snap is not None, "snapshot should succeed with a populated skills dir"
    assert (snap / "skills.tar.gz").exists()
    manifest = json.loads((snap / "manifest.json").read_text())
    assert manifest["reason"] == "test"
    assert manifest["skill_files"] == 2
    assert manifest["archive_bytes"] > 0


def test_snapshot_default_keeps_curator_state_in_skills_archive(backup_env):
    cb = backup_env["cb"]
    legacy_state = backup_env["skills"] / ".curator_state"
    legacy_state.write_text('{"run_count": 2}', encoding="utf-8")

    snap = cb.snapshot_skills(reason="default-state")

    assert snap is not None
    with tarfile.open(snap / "skills.tar.gz") as tf:
        assert ".curator_state" in tf.getnames()
    assert not (snap / cb.CURATOR_STATE_FILENAME).exists()
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert "curator_state" not in manifest


def test_snapshot_captures_external_curator_state_without_moving_it(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 4}', encoding="utf-8")
    stale_legacy = backup_env["skills"] / ".curator_state"
    stale_legacy.write_text('{"run_count": 1}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))

    snap = cb.snapshot_skills(reason="external-state")

    assert snap is not None
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 4}'
    assert stale_legacy.exists(), "snapshot must not migrate or delete live files"
    assert (snap / cb.CURATOR_STATE_FILENAME).read_text(
        encoding="utf-8"
    ) == '{"run_count": 4}'
    with tarfile.open(snap / "skills.tar.gz") as tf:
        assert ".curator_state" not in tf.getnames()
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["curator_state"] == {
        "backed_up": True,
        "bytes": len(b'{"run_count": 4}'),
        "external": True,
    }


@pytest.mark.parametrize("override", ["", "state.json", "curator/state.json", "/"])
def test_snapshot_rejects_unsafe_curator_state_override(
    backup_env, monkeypatch, override
):
    cb = backup_env["cb"]
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", override)

    with pytest.raises(ValueError, match="absolute file path"):
        cb.snapshot_skills(reason="invalid-override")

    assert not (backup_env["skills"] / ".curator_backups").exists()


def test_snapshot_rejects_curator_state_directly_under_skills(
    backup_env, monkeypatch
):
    cb = backup_env["cb"]
    override = backup_env["skills"] / "curator-state.json"
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(override))

    with pytest.raises(ValueError, match="outside the skills directory"):
        cb.snapshot_skills(reason="invalid-override")


def test_snapshot_rejects_lexical_curator_state_path_under_skills(
    backup_env, monkeypatch
):
    cb = backup_env["cb"]
    override = (
        backup_env["skills"]
        / ".."
        / "curator"
        / "state.json"
    )
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(override))

    with pytest.raises(ValueError, match="outside the skills directory"):
        cb.snapshot_skills(reason="invalid-override")


def test_snapshot_rejects_symlinked_parent_resolving_into_skills(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    linked_skills = tmp_path / "linked-skills"
    linked_skills.symlink_to(
        backup_env["skills"],
        target_is_directory=True,
    )
    monkeypatch.setenv(
        "HERMES_CURATOR_STATE_PATH",
        str(linked_skills / "state.json"),
    )

    with pytest.raises(ValueError, match="must not use symlinks"):
        cb.snapshot_skills(reason="invalid-override")


def test_snapshot_rejects_symlinked_curator_state_file(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    real_state = tmp_path / "real-state.json"
    real_state.write_text("{}", encoding="utf-8")
    linked_state = tmp_path / "linked-state.json"
    linked_state.symlink_to(real_state)
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(linked_state))

    with pytest.raises(ValueError, match="must not use symlinks"):
        cb.snapshot_skills(reason="invalid-override")


def test_snapshot_excludes_backups_dir_itself(backup_env):
    """The backup must NOT contain .curator_backups/ — that would recurse
    with every subsequent snapshot and balloon disk usage."""
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    snap1 = cb.snapshot_skills(reason="first")
    assert snap1 is not None
    snap2 = cb.snapshot_skills(reason="second")
    assert snap2 is not None
    with tarfile.open(snap2 / "skills.tar.gz") as tf:
        names = tf.getnames()
    assert not any(n.startswith(".curator_backups") for n in names), (
        "second snapshot must not contain the first snapshot recursively"
    )


def test_snapshot_excludes_hub_dir(backup_env):
    """.hub/ is managed by the skills hub. Rolling it back would break
    lockfile invariants, so the snapshot omits it entirely."""
    cb = backup_env["cb"]
    hub = backup_env["skills"] / ".hub"
    hub.mkdir()
    (hub / "lock.json").write_text("{}")
    _write_skill(backup_env["skills"], "alpha")
    snap = cb.snapshot_skills(reason="t")
    assert snap is not None
    with tarfile.open(snap / "skills.tar.gz") as tf:
        names = tf.getnames()
    assert not any(n.startswith(".hub") for n in names)


def test_snapshot_disabled_returns_none(backup_env, monkeypatch):
    cb = backup_env["cb"]
    monkeypatch.setattr(cb, "is_enabled", lambda: False)
    _write_skill(backup_env["skills"], "alpha")
    assert cb.snapshot_skills() is None
    # And no backup dir should have been created
    assert not (backup_env["skills"] / ".curator_backups").exists()


def test_snapshot_uniquifies_when_same_second(backup_env, monkeypatch):
    """Two snapshots in the same wallclock second must not clobber each
    other. The module appends a counter to the second snapshot's id."""
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    frozen = "2026-05-01T12-00-00Z"
    monkeypatch.setattr(cb, "_utc_id", lambda now=None: frozen)
    s1 = cb.snapshot_skills(reason="a")
    s2 = cb.snapshot_skills(reason="b")
    assert s1 is not None and s2 is not None
    assert s1.name == frozen
    assert s2.name == f"{frozen}-01"


def test_snapshot_prunes_to_keep_count(backup_env, monkeypatch):
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    monkeypatch.setattr(cb, "get_keep", lambda: 3)

    # Create 5 snapshots with monotonically increasing fake ids
    ids = [f"2026-05-0{i}T00-00-00Z" for i in range(1, 6)]
    for i, fid in enumerate(ids):
        monkeypatch.setattr(cb, "_utc_id", lambda now=None, _f=fid: _f)
        cb.snapshot_skills(reason=f"n{i}")

    remaining = sorted(p.name for p in (backup_env["skills"] / ".curator_backups").iterdir())
    # Newest 3 kept (lex order == date order for this id format)
    assert remaining == ids[2:], f"expected newest 3, got {remaining}"


# ---------------------------------------------------------------------------
# list_backups / _resolve_backup
# ---------------------------------------------------------------------------

def test_list_backups_empty(backup_env):
    cb = backup_env["cb"]
    assert cb.list_backups() == []


def test_list_backups_returns_manifest_data(backup_env):
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    cb.snapshot_skills(reason="m1")
    rows = cb.list_backups()
    assert len(rows) == 1
    assert rows[0]["reason"] == "m1"
    assert rows[0]["skill_files"] == 1


def test_resolve_backup_newest_when_no_id(backup_env, monkeypatch):
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    ids = ["2026-05-01T00-00-00Z", "2026-05-02T00-00-00Z"]
    for fid in ids:
        monkeypatch.setattr(cb, "_utc_id", lambda now=None, _f=fid: _f)
        cb.snapshot_skills()
    resolved = cb._resolve_backup(None)
    assert resolved is not None
    assert resolved.name == "2026-05-02T00-00-00Z", (
        "resolve(None) must return newest regular snapshot"
    )


def test_resolve_backup_unknown_id_returns_none(backup_env):
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    cb.snapshot_skills()
    assert cb._resolve_backup("not-an-id") is None


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def test_rollback_restores_deleted_skill(backup_env):
    """The whole point of this feature: user loses a skill, rollback
    brings it back."""
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    user_skill = _write_skill(skills, "my-personal-workflow", body="important content")
    cb.snapshot_skills(reason="pre-simulated-curator")

    # Simulate curator archiving it out of existence
    import shutil as _sh
    _sh.rmtree(user_skill)
    assert not user_skill.exists()

    ok, msg, _ = cb.rollback()
    assert ok, f"rollback failed: {msg}"
    assert user_skill.exists(), "my-personal-workflow should be restored"
    assert "important content" in (user_skill / "SKILL.md").read_text()


def test_rollback_restores_external_state_without_polluting_skills(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 1}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))
    _write_skill(skills, "before")
    snap = cb.snapshot_skills(reason="external-v1")
    assert snap is not None

    external_state.write_text('{"run_count": 9}', encoding="utf-8")
    (skills / ".curator_state").write_text(
        '{"stale": true}',
        encoding="utf-8",
    )
    _write_skill(skills, "after")

    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert ok, msg
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 1}'
    assert not (skills / ".curator_state").exists()
    assert (skills / "before").exists()
    assert not (skills / "after").exists()


def test_rollback_redirects_legacy_state_to_external_override(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    legacy_state = skills / ".curator_state"
    legacy_state.write_text('{"run_count": 3}', encoding="utf-8")
    snap = cb.snapshot_skills(reason="legacy-state")
    assert snap is not None

    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 8}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))
    legacy_state.write_text('{"stale": true}', encoding="utf-8")

    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert ok, msg
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 3}'
    assert not legacy_state.exists()


def test_rollback_external_snapshot_requires_explicit_override(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 1}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))
    _write_skill(skills, "snapshot-skill")
    snap = cb.snapshot_skills(reason="external-state")
    assert snap is not None

    monkeypatch.delenv("HERMES_CURATOR_STATE_PATH")
    _write_skill(skills, "live-skill")

    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert not ok
    assert "HERMES_CURATOR_STATE_PATH" in msg
    assert (skills / "live-skill").exists()
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 1}'


def test_rollback_rejects_symlinked_external_state_companion(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 1}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))
    _write_skill(skills, "snapshot-skill")
    snap = cb.snapshot_skills(reason="external-state")
    assert snap is not None

    companion = snap / cb.CURATOR_STATE_FILENAME
    companion.unlink()
    attacker_file = tmp_path / "attacker-state.json"
    attacker_file.write_text('{"run_count": 999}', encoding="utf-8")
    companion.symlink_to(attacker_file)
    _write_skill(skills, "live-skill")
    external_state.write_text('{"run_count": 7}', encoding="utf-8")

    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert not ok
    assert "unsafe curator state companion" in msg
    assert (skills / "live-skill").exists()
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 7}'


def test_rollback_external_state_write_failure_restores_live_state(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 1}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))
    _write_skill(skills, "snapshot-skill")
    snap = cb.snapshot_skills(reason="external-state")
    assert snap is not None

    external_state.write_text('{"run_count": 7}', encoding="utf-8")
    _write_skill(skills, "live-skill")
    original_restore = cb._atomic_restore_curator_state
    calls = 0

    def fail_first_restore(path, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated external state write failure")
        return original_restore(path, content)

    monkeypatch.setattr(
        cb,
        "_atomic_restore_curator_state",
        fail_first_restore,
    )

    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert not ok
    assert "external curator state restore failed" in msg
    assert (skills / "snapshot-skill").exists()
    assert (skills / "live-skill").exists()
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 7}'


def test_rollback_is_itself_undoable(backup_env):
    """A rollback creates its own safety snapshot before replacing the
    tree, so the user can undo a mistaken rollback. The safety snapshot
    is a real tarball with reason='pre-rollback to <id>' — it's
    listed by list_backups() just like any other snapshot and can be
    restored the same way."""
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "v1")
    cb.snapshot_skills(reason="snapshot-of-v1")

    # Overwrite with a new skill state
    import shutil as _sh
    _sh.rmtree(skills / "v1")
    _write_skill(skills, "v2")

    ok, _, _ = cb.rollback()
    assert ok
    assert (skills / "v1").exists()

    # list_backups should show a safety snapshot tagged "pre-rollback to <target-id>"
    rows = cb.list_backups()
    pre_rollback_entries = [r for r in rows if "pre-rollback" in (r.get("reason") or "")]
    assert len(pre_rollback_entries) >= 1, (
        f"expected a pre-rollback safety snapshot in list_backups(), got: "
        f"{[(r.get('id'), r.get('reason')) for r in rows]}"
    )
    # And the transient staging dir must be gone (it's implementation detail)
    backups_dir = skills / ".curator_backups"
    staging_dirs = [p for p in backups_dir.iterdir() if p.name.startswith(".rollback-staging-")]
    assert staging_dirs == [], (
        f"staging dir should be cleaned up on success, got: {staging_dirs}"
    )


def test_rollback_no_snapshots_returns_error(backup_env):
    cb = backup_env["cb"]
    ok, msg, _ = cb.rollback()
    assert not ok
    assert "no matching backup" in msg.lower() or "no snapshot" in msg.lower()


def test_rollback_rejects_unsafe_tarball(backup_env, monkeypatch):
    """Tarballs with absolute paths or .. components must be refused even
    if someone crafts a malicious snapshot. Defense in depth — normal
    curator snapshots never produce these."""
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "alpha")
    cb.snapshot_skills(reason="legit")

    # Hand-craft a malicious tarball replacing the legit one
    rows = cb.list_backups()
    snap_dir = Path(rows[0]["path"])
    mal = snap_dir / "skills.tar.gz"
    mal.unlink()
    with tarfile.open(mal, "w:gz") as tf:
        evil = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
        evil.write(b"evil")
        evil.close()
        tf.add(evil.name, arcname="../../etc/evil.md")
        os.unlink(evil.name)

    ok, msg, _ = cb.rollback()
    assert not ok
    assert "unsafe" in msg.lower() or "refus" in msg.lower() or "extract" in msg.lower()
    assert (skills / "alpha").exists()


def test_rollback_normalizes_legacy_curator_state_member(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "original")
    snap = cb.snapshot_skills(reason="legacy-state")
    assert snap is not None

    archive_path = snap / "skills.tar.gz"
    archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_tar_file(
            archive,
            "././.curator_state",
            b'{"run_count": 2}',
        )
        _add_tar_file(
            archive,
            "./original/SKILL.md",
            b"restored",
        )

    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 9}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))
    (skills / ".curator_state").write_text("stale", encoding="utf-8")

    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert ok, msg
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 2}'
    assert not (skills / ".curator_state").exists()


def test_rollback_excludes_entire_legacy_curator_state_namespace(
    backup_env, monkeypatch, tmp_path
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "original")
    snap = cb.snapshot_skills(reason="adversarial-state-directory")
    assert snap is not None

    archive_path = snap / "skills.tar.gz"
    archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_tar_file(
            archive,
            ".curator_state/child",
            b"must-not-land-in-skills",
        )
        _add_tar_file(archive, "original/SKILL.md", b"restored")
    (snap / cb.CURATOR_STATE_FILENAME).write_bytes(b'{"run_count": 2}')

    external_state = tmp_path / "runtime-data" / "curator" / "state.json"
    external_state.parent.mkdir(parents=True)
    external_state.write_text('{"run_count": 9}', encoding="utf-8")
    monkeypatch.setenv("HERMES_CURATOR_STATE_PATH", str(external_state))

    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert ok, msg
    assert external_state.read_text(encoding="utf-8") == '{"run_count": 2}'
    assert not (skills / ".curator_state").exists()


def test_rollback_rejects_tar_symlink_pivot(backup_env, tmp_path):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "live-skill")
    snap = cb.snapshot_skills(reason="pivot")
    assert snap is not None

    archive_path = snap / "skills.tar.gz"
    archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as archive:
        pivot = tarfile.TarInfo("pivot")
        pivot.type = tarfile.SYMTYPE
        pivot.linkname = ".."
        archive.addfile(pivot)
        _add_tar_file(archive, "pivot/escaped.txt", b"escaped")

    escaped = backup_env["home"] / "escaped.txt"
    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert not ok
    assert "extract" in msg.lower() or "outside" in msg.lower()
    assert not escaped.exists()
    assert (skills / "live-skill").exists()


@pytest.mark.parametrize(
    ("member_type", "linkname"),
    [
        (tarfile.SYMTYPE, "regular.txt"),
        (tarfile.LNKTYPE, "regular.txt"),
    ],
)
def test_rollback_fallback_refuses_links_without_safe_filter(
    backup_env, monkeypatch, member_type, linkname
):
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "live-skill")
    snap = cb.snapshot_skills(reason="fallback-link")
    assert snap is not None

    archive_path = snap / "skills.tar.gz"
    archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_tar_file(archive, "regular.txt", b"regular")
        link = tarfile.TarInfo("linked.txt")
        link.type = member_type
        link.linkname = linkname
        archive.addfile(link)

    monkeypatch.delattr(cb.tarfile, "data_filter", raising=False)
    ok, msg, _ = cb.rollback(backup_id=snap.name)

    assert not ok
    assert "safe tar extraction filter unavailable" in msg
    assert (skills / "live-skill").exists()


# ---------------------------------------------------------------------------
# Integration with run_curator_review
# ---------------------------------------------------------------------------

def test_real_run_takes_pre_snapshot(backup_env, monkeypatch):
    """A real (non-dry) curator pass must snapshot the tree before calling
    apply_automatic_transitions. This is the safety net #18373 asked for."""
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "alpha")

    # Reload curator module against the freshly-env'd hermes_constants
    from agent import curator
    importlib.reload(curator)

    # Stub out LLM review and auto transitions — we only care about the
    # snapshot side-effect.
    monkeypatch.setattr(
        curator, "_run_llm_review",
        lambda p: {"final": "", "summary": "s", "model": "", "provider": "",
                   "tool_calls": [], "error": None},
    )
    monkeypatch.setattr(
        curator, "apply_automatic_transitions",
        lambda now=None: {"checked": 1, "marked_stale": 0, "archived": 0, "reactivated": 0},
    )

    curator.run_curator_review(synchronous=True)
    # Pre-run snapshot should exist
    rows = cb.list_backups()
    assert any(r.get("reason") == "pre-curator-run" for r in rows), (
        f"expected a pre-curator-run snapshot, got {[r.get('reason') for r in rows]}"
    )


def test_dry_run_skips_snapshot(backup_env, monkeypatch):
    """Dry-run previews must not spend disk on a snapshot — they don't
    mutate anything, so there's nothing to back up."""
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    _write_skill(skills, "alpha")

    from agent import curator
    importlib.reload(curator)
    monkeypatch.setattr(
        curator, "_run_llm_review",
        lambda p: {"final": "", "summary": "s", "model": "", "provider": "",
                   "tool_calls": [], "error": None},
    )

    curator.run_curator_review(synchronous=True, dry_run=True)
    rows = cb.list_backups()
    assert not any(r.get("reason") == "pre-curator-run" for r in rows), (
        "dry-run must not create a pre-run snapshot"
    )


# ---------------------------------------------------------------------------
# cron-jobs backup + rollback (the part issue #18671's follow-up adds)
# ---------------------------------------------------------------------------


def _write_cron_jobs(home: Path, jobs: list) -> Path:
    """Write a synthetic cron/jobs.json under HERMES_HOME. Returns the path.
    Mirrors cron.jobs.save_jobs() wrapper shape: `{"jobs": [...], "updated_at": ...}`.
    """
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    path = cron_dir / "jobs.json"
    path.write_text(
        json.dumps({"jobs": jobs, "updated_at": "2026-05-01T00:00:00Z"}, indent=2),
        encoding="utf-8",
    )
    return path


def _reload_cron_jobs(home: Path):
    """Reload cron.jobs so its module-level HERMES_DIR picks up the tmp HOME."""
    import hermes_constants
    importlib.reload(hermes_constants)
    if "cron.jobs" in sys.modules:
        import cron.jobs as _cj
        importlib.reload(_cj)
    else:
        import cron.jobs as _cj  # noqa: F401
    import cron.jobs as cj
    return cj


def test_snapshot_includes_cron_jobs(backup_env):
    """With a cron/jobs.json present, snapshot writes cron-jobs.json and records it in manifest."""
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    _write_cron_jobs(backup_env["home"], [
        {"id": "job-a", "name": "a", "schedule": "every 1h", "skills": ["alpha"]},
        {"id": "job-b", "name": "b", "schedule": "every 2h", "skill": "alpha"},
    ])

    snap = cb.snapshot_skills(reason="test")
    assert snap is not None
    assert (snap / cb.CRON_JOBS_FILENAME).exists()

    mf = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert mf["cron_jobs"]["backed_up"] is True
    assert mf["cron_jobs"]["jobs_count"] == 2


def test_snapshot_without_cron_jobs_file_still_succeeds(backup_env):
    """No cron/jobs.json on disk → snapshot succeeds, manifest records absence."""
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    # Deliberately do not create ~/.hermes/cron/jobs.json

    snap = cb.snapshot_skills(reason="test")
    assert snap is not None
    assert not (snap / cb.CRON_JOBS_FILENAME).exists()

    mf = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert mf["cron_jobs"]["backed_up"] is False
    assert "cron/jobs.json" in mf["cron_jobs"]["reason"]


def test_snapshot_cron_jobs_malformed_json_still_captured(backup_env):
    """Malformed jobs.json is still copied to the snapshot (fidelity over
    validation); the manifest notes the parse warning."""
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    (backup_env["home"] / "cron").mkdir()
    (backup_env["home"] / "cron" / "jobs.json").write_text("{oh no", encoding="utf-8")

    snap = cb.snapshot_skills(reason="test")
    assert snap is not None
    # Raw file was copied even though we couldn't parse it
    assert (snap / cb.CRON_JOBS_FILENAME).read_text() == "{oh no"

    mf = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert mf["cron_jobs"]["backed_up"] is True
    assert mf["cron_jobs"]["jobs_count"] == 0
    assert "parse_warning" in mf["cron_jobs"]


def test_rollback_restores_cron_skill_links(backup_env):
    """End-to-end: snapshot with job [alpha,beta], curator-style in-place
    rewrite to [umbrella], then rollback → skills restored to [alpha,beta]."""
    cb = backup_env["cb"]
    home = backup_env["home"]
    _write_skill(backup_env["skills"], "alpha")
    _write_skill(backup_env["skills"], "beta")
    _write_skill(backup_env["skills"], "umbrella")

    cj = _reload_cron_jobs(home)
    cj.create_job(name="weekly", prompt="p", schedule="every 7d",
                  skills=["alpha", "beta"])

    snap = cb.snapshot_skills(reason="pre-curator-run")
    assert snap is not None

    # Simulate the curator's in-place cron rewrite after consolidation
    cj.rewrite_skill_refs(
        consolidated={"alpha": "umbrella", "beta": "umbrella"},
        pruned=[],
    )
    live_after_curator = cj.load_jobs()
    assert live_after_curator[0]["skills"] == ["umbrella"]

    # Now roll back
    ok, msg, _ = cb.rollback(backup_id=snap.name)
    assert ok, msg
    assert "cron links" in msg

    live_after_rollback = cj.load_jobs()
    # skills restored; legacy `skill` mirror follows first element
    assert live_after_rollback[0]["skills"] == ["alpha", "beta"]


def test_rollback_only_touches_skill_fields(backup_env):
    """Every field other than skills/skill must remain untouched across rollback.
    Schedule, enabled, prompt, timestamps — all live state, hands off."""
    cb = backup_env["cb"]
    home = backup_env["home"]
    _write_skill(backup_env["skills"], "alpha")

    # Hand-rolled jobs.json with varied fields (no real create_job — we want
    # exact field control).
    _write_cron_jobs(home, [{
        "id": "stable-id",
        "name": "original-name",
        "prompt": "original prompt",
        "schedule": "every 1h",
        "skills": ["alpha"],
        "enabled": True,
        "last_run_at": "2026-04-01T00:00:00Z",
    }])
    snap = cb.snapshot_skills(reason="pre-curator-run")
    assert snap is not None

    # User/scheduler activity AFTER the snapshot: rename the job, change
    # the schedule, update timestamps, and (curator) rewrite the skills list.
    cj = _reload_cron_jobs(home)
    jobs = cj.load_jobs()
    jobs[0]["name"] = "renamed-since-snapshot"
    jobs[0]["schedule"] = "every 30m"
    jobs[0]["last_run_at"] = "2026-05-01T12:00:00Z"
    jobs[0]["skills"] = ["umbrella"]  # pretend curator did this
    cj.save_jobs(jobs)

    ok, _, _ = cb.rollback(backup_id=snap.name)
    assert ok

    after = cj.load_jobs()
    job = after[0]
    # skills: restored
    assert job["skills"] == ["alpha"]
    # everything else: untouched (live state preserved)
    assert job["name"] == "renamed-since-snapshot"
    assert job["schedule"] == "every 30m"
    assert job["last_run_at"] == "2026-05-01T12:00:00Z"
    assert job["prompt"] == "original prompt"


def test_rollback_skips_jobs_the_user_deleted(backup_env):
    """If the user deleted a cron job after the snapshot, rollback must
    NOT resurrect it — the user's delete is a later, explicit choice."""
    cb = backup_env["cb"]
    home = backup_env["home"]
    _write_skill(backup_env["skills"], "alpha")

    _write_cron_jobs(home, [
        {"id": "keep-me", "name": "keep", "schedule": "every 1h", "skills": ["alpha"]},
        {"id": "delete-me", "name": "gone", "schedule": "every 1h", "skills": ["alpha"]},
    ])
    snap = cb.snapshot_skills(reason="pre-curator-run")

    # User deletes one job after the snapshot
    cj = _reload_cron_jobs(home)
    cj.save_jobs([j for j in cj.load_jobs() if j["id"] != "delete-me"])

    ok, _, _ = cb.rollback(backup_id=snap.name)
    assert ok

    live_after = cj.load_jobs()
    live_ids = {j["id"] for j in live_after}
    assert "keep-me" in live_ids
    assert "delete-me" not in live_ids  # not resurrected


def test_rollback_leaves_new_jobs_untouched(backup_env):
    """Jobs created AFTER the snapshot must pass through rollback unchanged."""
    cb = backup_env["cb"]
    home = backup_env["home"]
    _write_skill(backup_env["skills"], "alpha")
    _write_cron_jobs(home, [
        {"id": "original", "name": "o", "schedule": "every 1h", "skills": ["alpha"]},
    ])
    snap = cb.snapshot_skills(reason="pre-curator-run")

    cj = _reload_cron_jobs(home)
    jobs = cj.load_jobs()
    jobs.append({"id": "new-after-snapshot", "name": "new",
                 "schedule": "every 15m", "skills": ["brand-new-skill"]})
    cj.save_jobs(jobs)

    ok, _, _ = cb.rollback(backup_id=snap.name)
    assert ok

    live = cj.load_jobs()
    by_id = {j["id"]: j for j in live}
    assert "new-after-snapshot" in by_id
    # New job's fields completely preserved
    assert by_id["new-after-snapshot"]["skills"] == ["brand-new-skill"]
    assert by_id["new-after-snapshot"]["schedule"] == "every 15m"


def test_rollback_with_snapshot_missing_cron_succeeds(backup_env):
    """Older snapshots (created before this feature shipped) have no
    cron-jobs.json. Rollback must still restore the skills tree and not
    error out."""
    cb = backup_env["cb"]
    home = backup_env["home"]
    _write_skill(backup_env["skills"], "alpha")

    # No cron/jobs.json at snapshot time — simulates a pre-feature snapshot
    snap = cb.snapshot_skills(reason="test")
    assert snap is not None
    assert not (snap / cb.CRON_JOBS_FILENAME).exists()

    # Later the user created a cron job
    _write_cron_jobs(home, [
        {"id": "later-job", "name": "l", "schedule": "every 1h", "skills": ["x"]},
    ])

    ok, msg, _ = cb.rollback(backup_id=snap.name)
    # Main rollback still succeeds; cron report notes the missing file.
    assert ok, msg
    # Jobs.json untouched (nothing to restore from)
    cj = _reload_cron_jobs(home)
    jobs = cj.load_jobs()
    assert jobs[0]["id"] == "later-job"
    assert jobs[0]["skills"] == ["x"]


def test_restore_cron_skill_links_standalone(backup_env):
    """Unit-level test on _restore_cron_skill_links without the full rollback.
    Verifies the report structure carefully."""
    cb = backup_env["cb"]
    home = backup_env["home"]

    # Prime a snapshot dir manually with cron-jobs.json
    backups_dir = home / "skills" / ".curator_backups" / "fake-id"
    backups_dir.mkdir(parents=True)
    (backups_dir / cb.CRON_JOBS_FILENAME).write_text(json.dumps([
        {"id": "job-1", "name": "one", "skills": ["narrow-a", "narrow-b"]},
        {"id": "job-2", "name": "two", "skill": "legacy-single"},
        {"id": "job-gone", "name": "deleted", "skills": ["whatever"]},
    ]), encoding="utf-8")

    # Live jobs: job-1 got rewritten, job-2 unchanged, job-gone deleted
    _write_cron_jobs(home, [
        {"id": "job-1", "name": "one", "skills": ["umbrella"], "schedule": "every 1h"},
        {"id": "job-2", "name": "two", "skill": "legacy-single", "schedule": "every 1h"},
        {"id": "job-new", "name": "new", "skills": ["x"], "schedule": "every 1h"},
    ])
    _reload_cron_jobs(home)

    report = cb._restore_cron_skill_links(backups_dir)
    assert report["attempted"] is True
    assert report["error"] is None
    assert report["unchanged"] == 1  # job-2 matched
    assert len(report["restored"]) == 1  # job-1 got restored
    assert report["restored"][0]["job_id"] == "job-1"
    assert report["restored"][0]["to"]["skills"] == ["narrow-a", "narrow-b"]
    assert len(report["skipped_missing"]) == 1
    assert report["skipped_missing"][0]["job_id"] == "job-gone"


# ---------------------------------------------------------------------------
# Rollback must not let the pre-rollback safety snapshot prune the target
# (regression: restoring the oldest snapshot at the keep limit destroyed it)
# ---------------------------------------------------------------------------

def _three_ordered_snapshots(cb, skills, monkeypatch):
    """Create snapshots 05-01 / 05-02 / 05-03 capturing growing trees, with
    keep=3 so the backups dir is exactly at the retention limit. 05-01 holds
    only 'pristine'; later snapshots add 'extra2' and 'extra3'. Leaves
    _utc_id patched to a newest id so the rollback safety snapshot sorts
    last. Returns the oldest snapshot id."""
    monkeypatch.setattr(cb, "get_keep", lambda: 3)
    plan = [
        ("2026-05-01T00-00-00Z", ["pristine"]),
        ("2026-05-02T00-00-00Z", ["pristine", "extra2"]),
        ("2026-05-03T00-00-00Z", ["pristine", "extra2", "extra3"]),
    ]
    for snap_id, names in plan:
        for n in names:
            _write_skill(skills, n)
        monkeypatch.setattr(cb, "_utc_id", lambda now=None, _i=snap_id: _i)
        assert cb.snapshot_skills(reason=snap_id) is not None
    monkeypatch.setattr(cb, "_utc_id", lambda now=None: "2026-05-09T00-00-00Z")
    return "2026-05-01T00-00-00Z"


def test_rollback_to_oldest_snapshot_at_keep_limit_succeeds(backup_env, monkeypatch):
    """Restoring the oldest snapshot when the backups dir is at the keep limit
    must succeed: the pre-rollback safety snapshot's prune step must not evict
    the snapshot being restored."""
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    oldest = _three_ordered_snapshots(cb, skills, monkeypatch)

    ok, msg, _ = cb.rollback(backup_id=oldest)

    assert ok is True, f"rollback to oldest snapshot should succeed, got: {msg}"
    # 05-01 only contained 'pristine'; a real restore reflects exactly that.
    assert (skills / "pristine" / "SKILL.md").exists()
    assert not (skills / "extra3").exists(), "tree was not restored to the oldest snapshot"


def test_rollback_does_not_delete_the_snapshot_it_restores_from(backup_env, monkeypatch):
    """The snapshot a rollback restores from must still exist afterwards — the
    safety snapshot's prune must never delete the target."""
    cb = backup_env["cb"]
    skills = backup_env["skills"]
    oldest = _three_ordered_snapshots(cb, skills, monkeypatch)
    target_dir = skills / ".curator_backups" / oldest
    assert target_dir.exists(), "precondition: target snapshot exists before rollback"

    cb.rollback(backup_id=oldest)

    assert target_dir.exists(), (
        "the pre-rollback safety snapshot pruned away the snapshot being "
        "restored — the oldest restore point is destroyed by restoring to it"
    )
