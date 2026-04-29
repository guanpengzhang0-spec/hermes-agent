"""Unit tests for ``self_evolution.audit_log``.

Covers:
  * record() emits to EventStream with correct kind/actor/payload
  * record() best-effort: emit failure → returns None, no raise
  * query_recent returns AuditEntry objects newest-first
  * query_recent honours kind filter
  * make_entry_for_text computes diff + hash
  * verify_integrity classifies matches / mismatches / missing
  * git tag is best-effort (skipped when git unavailable)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from observability.event_stream import EventStream
from self_evolution.audit_log import (
    AuditEntry,
    AuditKind,
    AuditLog,
)


# ─────────────────────── fixtures ───────────────────────


@pytest.fixture
def stream(tmp_path: Path):
    db = tmp_path / "events.db"
    s = EventStream(session_id="audit-test", db_path=db)
    yield s
    s.close()


@pytest.fixture
def audit(stream):
    return AuditLog(stream, enable_git_tag=False)


# ─────────────────────── record ───────────────────────


def test_record_writes_event(audit, stream):
    entry = AuditEntry(
        kind=AuditKind.SKILL_MODIFIED,
        actor="skill:loop-guard",
        target_path="/tmp/SKILL.md",
        diff="- old\n+ new",
        content_hash="deadbeef",
        rationale="tweak threshold",
    )
    rid = audit.record(entry)
    assert isinstance(rid, int) and rid > 0

    events = list(stream.replay("audit-test"))
    assert len(events) == 1
    e = events[0]
    assert e.kind == AuditKind.SKILL_MODIFIED.value
    assert e.actor == "skill:loop-guard"
    assert e.payload["target_path"] == "/tmp/SKILL.md"
    assert e.payload["rationale"] == "tweak threshold"


def test_record_with_broken_stream_returns_none():
    class BrokenStream:
        def emit(self, *a, **kw):  # noqa: ANN001
            raise RuntimeError("disk full")

        def query(self, *a, **kw):  # noqa: ANN001
            return iter(())

    audit = AuditLog(BrokenStream(), enable_git_tag=False)
    entry = AuditEntry(
        kind=AuditKind.OTHER,
        actor="x",
        target_path="/tmp/x",
        diff="",
        content_hash="abc",
        rationale="r",
    )
    assert audit.record(entry) is None


def test_record_with_metadata_preserved(audit, stream):
    entry = AuditEntry(
        kind=AuditKind.CONFIG_CHANGED,
        actor="cli",
        target_path="/tmp/cfg.yaml",
        diff="",
        content_hash="abc",
        rationale="enable feature",
        metadata={"feature": "loop_detector", "threshold": 3},
    )
    audit.record(entry)
    events = list(stream.replay("audit-test"))
    assert events[0].payload["metadata"] == {
        "feature": "loop_detector",
        "threshold": 3,
    }


# ─────────────────────── query_recent ───────────────────────


def test_query_recent_returns_audit_entries(audit):
    for i in range(3):
        audit.record(
            AuditEntry(
                kind=AuditKind.SKILL_MODIFIED,
                actor=f"a{i}",
                target_path=f"/tmp/{i}",
                diff="",
                content_hash=f"hash{i}",
                rationale=f"r{i}",
            )
        )
    entries = audit.query_recent(limit=10)
    assert len(entries) == 3
    assert all(isinstance(e, AuditEntry) for e in entries)
    # newest first
    assert entries[0].actor == "a2"
    assert entries[-1].actor == "a0"


def test_query_recent_kind_filter(audit):
    audit.record(
        AuditEntry(
            kind=AuditKind.SKILL_MODIFIED,
            actor="a",
            target_path="/tmp/skill",
            diff="",
            content_hash="h",
            rationale="r",
        )
    )
    audit.record(
        AuditEntry(
            kind=AuditKind.CONFIG_CHANGED,
            actor="a",
            target_path="/tmp/cfg",
            diff="",
            content_hash="h",
            rationale="r",
        )
    )
    only_skills = audit.query_recent(kind=AuditKind.SKILL_MODIFIED)
    assert len(only_skills) == 1
    assert only_skills[0].kind is AuditKind.SKILL_MODIFIED


# ─────────────────────── make_entry_for_text ───────────────────────


def test_make_entry_for_text_computes_diff_and_hash():
    entry = AuditLog.make_entry_for_text(
        AuditKind.SKILL_MODIFIED,
        actor="skill:x",
        target_path="/tmp/SKILL.md",
        new_text="line 1\nline 2",
        old_text="line 1",
        rationale="add a line",
    )
    assert entry.kind is AuditKind.SKILL_MODIFIED
    assert "+ line 2" in entry.diff
    assert "- line 1" in entry.diff
    # hash is sha256 of new text
    import hashlib
    expected = hashlib.sha256("line 1\nline 2".encode()).hexdigest()
    assert entry.content_hash == expected


def test_make_entry_no_change_yields_empty_diff():
    entry = AuditLog.make_entry_for_text(
        AuditKind.OTHER,
        actor="x",
        target_path="/tmp/x",
        new_text="same",
        old_text="same",
        rationale="no-op",
    )
    assert entry.diff == ""


# ─────────────────────── verify_integrity ───────────────────────


def test_verify_integrity_matches(tmp_path: Path, audit):
    f = tmp_path / "tracked.txt"
    f.write_text("hello world")
    entry = AuditLog.make_entry_for_text(
        AuditKind.OTHER,
        actor="x",
        target_path=f,
        new_text="hello world",
        rationale="initial",
    )
    audit.record(entry)
    # verify_integrity reads from disk; recompute via _sha256_file:
    result = audit.verify_integrity()
    assert str(f) in result["matches"]
    assert str(f) not in result["mismatches"]


def test_verify_integrity_mismatch(tmp_path: Path, audit):
    f = tmp_path / "tracked.txt"
    f.write_text("v1")
    entry = AuditLog.make_entry_for_text(
        AuditKind.OTHER,
        actor="x",
        target_path=f,
        new_text="v1",
        rationale="initial",
    )
    audit.record(entry)
    # Mutate without recording an audit
    f.write_text("v2-tampered")
    result = audit.verify_integrity()
    assert str(f) in result["mismatches"]


def test_verify_integrity_missing(tmp_path: Path, audit):
    f = tmp_path / "tracked.txt"
    f.write_text("v1")
    entry = AuditLog.make_entry_for_text(
        AuditKind.OTHER,
        actor="x",
        target_path=f,
        new_text="v1",
        rationale="initial",
    )
    audit.record(entry)
    f.unlink()
    result = audit.verify_integrity()
    assert str(f) in result["missing"]


# ─────────────────────── git tag (best-effort) ───────────────────────


def test_git_tag_creates_tag_when_repo_exists(tmp_path: Path, stream):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"],
        check=True, capture_output=True,
    )
    f = tmp_path / "f.txt"
    f.write_text("x")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    audit = AuditLog(stream, enable_git_tag=True, git_repo_path=tmp_path)
    audit.record(
        AuditEntry(
            kind=AuditKind.OTHER,
            actor="test",
            target_path=str(f),
            diff="",
            content_hash="h",
            rationale="test tag",
        )
    )
    tags = subprocess.run(
        ["git", "-C", str(tmp_path), "tag"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "audit/audit_other/" in tags


def test_git_tag_silently_skipped_when_repo_missing(tmp_path: Path, stream):
    audit = AuditLog(
        stream, enable_git_tag=True, git_repo_path=tmp_path / "no-such-repo"
    )
    rid = audit.record(
        AuditEntry(
            kind=AuditKind.OTHER,
            actor="x",
            target_path="/tmp/x",
            diff="",
            content_hash="h",
            rationale="r",
        )
    )
    assert rid is not None  # event still recorded


# ─────────────────────── frozen ───────────────────────


def test_audit_entry_is_frozen():
    e = AuditEntry(
        kind=AuditKind.OTHER,
        actor="x",
        target_path="/tmp/x",
        diff="",
        content_hash="h",
        rationale="r",
    )
    with pytest.raises((AttributeError, Exception)):
        e.actor = "y"  # type: ignore[misc]
