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
    """The backup must NOT contain .curator_backups/, which would recurse
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









# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------



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








def test_snapshot_cron_jobs_utf8_bom_counted_and_backup_bomless(backup_env):
    """A UTF-8 BOM on jobs.json (Windows editors) must not break the job
    count, and the snapshot copy is written BOM-less so rollback restores a
    file cron/jobs.load_jobs can read."""
    cb = backup_env["cb"]
    _write_skill(backup_env["skills"], "alpha")
    cron_dir = backup_env["home"] / "cron"
    cron_dir.mkdir()
    payload = json.dumps({"jobs": [{"id": "job-a"}, {"id": "job-b"}]})
    (cron_dir / "jobs.json").write_bytes(b"\xef\xbb\xbf" + payload.encode())

    snap = cb.snapshot_skills(reason="test")
    assert snap is not None

    mf = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert mf["cron_jobs"]["backed_up"] is True
    assert mf["cron_jobs"]["jobs_count"] == 2
    assert "parse_warning" not in mf["cron_jobs"]

    # Backup copy is decoded text — no BOM survives into the snapshot.
    backup_bytes = (snap / cb.CRON_JOBS_FILENAME).read_bytes()
    assert not backup_bytes.startswith(b"\xef\xbb\xbf")
    assert json.loads(backup_bytes) == json.loads(payload)


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




# ---------------------------------------------------------------------------
# A failed extract must leave the skills tree exactly as it was found
# ---------------------------------------------------------------------------

def test_rollback_recovers_cleanly_from_a_partial_extract(backup_env, monkeypatch):
    """An extract that dies part-way must restore the original tree exactly.

    ``shutil.move`` moves *into* an existing directory rather than replacing
    it, so debris from a half-finished extract buried the user's own skill one
    level deeper (``skills/alpha/alpha/``) while rollback still reported
    "state restored".
    """
    cb = backup_env["cb"]
    skills = backup_env["skills"]

    _write_skill(skills, "alpha", body="snapshot copy")
    assert cb.snapshot_skills(reason="before") is not None

    # Diverge from the snapshot so a real restore would be observable.
    _write_skill(skills, "alpha", body="current copy")
    _write_skill(skills, "beta", body="current only")

    real_open = tarfile.open

    class _DiesMidExtract:
        """Writes part of the archive, then fails like a full disk would."""

        def __init__(self, inner):
            self._inner = inner

        def getmembers(self):
            return self._inner.getmembers()

        def extractall(self, path, *args, **kwargs):
            partial = Path(path) / "alpha"
            partial.mkdir(parents=True, exist_ok=True)
            (partial / "SKILL.md").write_text("half written", encoding="utf-8")
            (Path(path) / "gamma").mkdir(parents=True, exist_ok=True)
            raise OSError(28, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()
            return False

    def _open(name, mode="r", *args, **kwargs):
        # Only the rollback's read is intercepted; the pre-rollback safety
        # snapshot opens for write and must keep working.
        handle = real_open(name, mode, *args, **kwargs)
        return _DiesMidExtract(handle) if mode.startswith("r") else handle

    monkeypatch.setattr(cb.tarfile, "open", _open)

    ok, msg, _ = cb.rollback()
    assert not ok

    assert not (skills / "alpha" / "alpha").exists(), \
        "staged skill was nested, not restored"
    present = sorted(
        p.name for p in skills.iterdir() if p.name not in cb._EXCLUDE_TOP_LEVEL
    )
    assert present == ["alpha", "beta"], f"tree not restored: {present}"
    assert "current copy" in (skills / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert "current only" in (skills / "beta" / "SKILL.md").read_text(encoding="utf-8")
