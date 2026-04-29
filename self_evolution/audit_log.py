"""Append-only audit log for hermes self-modifications.

Whenever a self-evolution skill (loop-guard, constitution-audit, etc.)
edits a SKILL.md, config.yaml, or any other governance-sensitive file,
it MUST first record the change here. The audit log is the source of
truth for "what did the agent change about itself, when, and why".

Implementation:
  * Log entries flow into the existing ``EventStream`` (kind="audit_*"),
    leveraging its WAL durability and queryability. No second SQLite
    file to maintain.
  * Optionally creates a git tag in ``~/.hermes`` after each entry so
    the change is traceable in version control. Best-effort: failures
    are logged, never raised.
  * ``verify_integrity()`` cross-checks the audit log against current
    on-disk content via SHA256, flagging files that changed without an
    audit entry.

The module deliberately does NOT perform the modification itself —
callers (skills, CLI commands) make the file edit and then call
``record(...)``. Keeping these separate avoids smuggling business
logic into the audit subsystem.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────── public types ───────────────────────────


class AuditKind(str, Enum):
    """Categories of self-modification we audit.

    Each maps to a distinct EventStream kind so queries can filter.
    """

    SKILL_MODIFIED = "audit_skill_modified"
    CONFIG_CHANGED = "audit_config_changed"
    TOOL_INSTALLED = "audit_tool_installed"
    TOOL_REMOVED = "audit_tool_removed"
    SOUL_UPDATED = "audit_soul_updated"
    OTHER = "audit_other"


@dataclass(frozen=True)
class AuditEntry:
    """Immutable record of one self-modification."""

    kind: AuditKind
    actor: str               # "skill:loop-guard" / "user:cli" / "agent:tool" ...
    target_path: str         # absolute path to the file modified
    diff: str                # unified diff (empty for create/delete)
    content_hash: str        # sha256 of the new content (or "" for delete)
    rationale: str
    metadata: dict[str, Any] = field(default_factory=dict)


# Structural protocol for the EventStream we depend on, so we don't
# import the concrete class (avoids package-cycle risk and keeps tests
# trivial).
class _EventEmitter:
    def emit(
        self,
        kind: str,
        *,
        actor: str,
        payload: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Optional[int]: ...

    def query(
        self,
        *,
        session_id: Optional[str] = None,
        kind: Optional[str] = None,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        limit: int = 1000,
    ) -> Iterator[Any]: ...


# ─────────────────────────── helpers ───────────────────────────


def _sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─────────────────────────── AuditLog ───────────────────────────


class AuditLog:
    """Façade over EventStream for self-modification audit records."""

    def __init__(
        self,
        event_stream: _EventEmitter,
        *,
        enable_git_tag: bool = True,
        git_repo_path: Optional[str | os.PathLike] = None,
        actor_namespace: str = "audit",
    ) -> None:
        self._stream = event_stream
        self._enable_git_tag = enable_git_tag
        self._git_repo: Optional[Path] = (
            Path(git_repo_path).expanduser() if git_repo_path else None
        )
        self._actor_namespace = actor_namespace

    # ─────────────────────────── public API ───────────────────────────

    def record(self, entry: AuditEntry) -> Optional[int]:
        """Persist an audit entry. Returns the EventStream row id.

        Best-effort:
          * EventStream emit failure → returns None, logs a warning
          * Git tag failure → does not affect the return value
        """
        payload = {
            "actor": entry.actor,
            "target_path": entry.target_path,
            "diff": entry.diff,
            "content_hash": entry.content_hash,
            "rationale": entry.rationale,
            "metadata": entry.metadata,
        }
        actor = entry.actor or self._actor_namespace
        try:
            row_id = self._stream.emit(
                entry.kind.value, actor=actor, payload=payload
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit_log: emit failed (%s)", exc)
            return None

        if self._enable_git_tag and row_id is not None:
            self._best_effort_git_tag(entry, row_id)

        return row_id

    def query_recent(
        self, *, limit: int = 50, kind: Optional[AuditKind] = None
    ) -> list[AuditEntry]:
        """Fetch recent entries, newest first.

        Filters on a single ``kind`` if provided; otherwise returns
        across all audit kinds.
        """
        kinds = [kind] if kind else list(AuditKind)
        events: list[Any] = []
        for k in kinds:
            try:
                events.extend(
                    list(self._stream.query(kind=k.value, limit=limit))
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("audit_log: query failed (%s)", exc)
        # Sort newest first by ts_ms (Event has ts_ms field per EventStream)
        events.sort(key=lambda e: getattr(e, "ts_ms", 0), reverse=True)
        events = events[:limit]

        out: list[AuditEntry] = []
        for ev in events:
            payload = getattr(ev, "payload", {}) or {}
            try:
                out.append(
                    AuditEntry(
                        kind=AuditKind(getattr(ev, "kind", AuditKind.OTHER.value)),
                        actor=str(payload.get("actor", getattr(ev, "actor", ""))),
                        target_path=str(payload.get("target_path", "")),
                        diff=str(payload.get("diff", "")),
                        content_hash=str(payload.get("content_hash", "")),
                        rationale=str(payload.get("rationale", "")),
                        metadata=dict(payload.get("metadata", {}) or {}),
                    )
                )
            except (ValueError, KeyError):
                # Skip malformed entries (kind not in enum, etc.)
                continue
        return out

    def verify_integrity(self) -> dict[str, list[str]]:
        """Cross-check on-disk SHA256 against the most recent audit per
        target_path. Returns a dict::

            {
              "matches":     [...paths matching their last audit...],
              "mismatches":  [...paths that changed without a new audit...],
              "missing":     [...paths recorded in audits but no longer exist...]
            }
        """
        latest_by_path: dict[str, AuditEntry] = {}
        for entry in self.query_recent(limit=10_000):
            if entry.target_path and entry.target_path not in latest_by_path:
                latest_by_path[entry.target_path] = entry

        matches: list[str] = []
        mismatches: list[str] = []
        missing: list[str] = []
        for path_str, entry in latest_by_path.items():
            path = Path(path_str)
            if not path.exists():
                missing.append(path_str)
                continue
            disk_hash = _sha256_file(path)
            if disk_hash == entry.content_hash:
                matches.append(path_str)
            else:
                mismatches.append(path_str)
        return {
            "matches": matches,
            "mismatches": mismatches,
            "missing": missing,
        }

    # ─────────────────────────── convenience factories ───────────────────────────

    @staticmethod
    def make_entry_for_text(
        kind: AuditKind,
        *,
        actor: str,
        target_path: str | os.PathLike,
        new_text: str,
        old_text: str = "",
        rationale: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AuditEntry:
        """Build an AuditEntry from raw text. Computes a tiny line-level
        diff and the new content hash."""
        diff = _simple_diff(old_text, new_text)
        return AuditEntry(
            kind=kind,
            actor=actor,
            target_path=str(target_path),
            diff=diff,
            content_hash=_sha256_text(new_text),
            rationale=rationale,
            metadata=metadata or {},
        )

    # ─────────────────────────── git tag ───────────────────────────

    def _best_effort_git_tag(
        self, entry: AuditEntry, row_id: int
    ) -> None:
        if self._git_repo is None or not self._git_repo.exists():
            return
        if shutil.which("git") is None:
            return
        tag = f"audit/{entry.kind.value}/{row_id}"
        msg = f"{entry.actor}: {entry.rationale[:120]}"
        try:
            subprocess.run(
                ["git", "tag", "-a", tag, "-m", msg],
                cwd=str(self._git_repo),
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("audit_log: git tag failed (%s)", exc)


# ─────────────────────────── tiny diff helper ───────────────────────────


def _simple_diff(old: str, new: str) -> str:
    """Minimal +/- diff. We avoid importing difflib to keep the module's
    import surface small (this runs on every self-mod)."""
    if old == new:
        return ""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    out: list[str] = []
    # Simplest-possible heuristic: emit every line removed from old then
    # every line added to new. Good enough for audit display; the
    # content_hash is what we check against, not the diff.
    if old_lines:
        out.extend(f"- {ln}" for ln in old_lines)
    if new_lines:
        out.extend(f"+ {ln}" for ln in new_lines)
    return "\n".join(out)
