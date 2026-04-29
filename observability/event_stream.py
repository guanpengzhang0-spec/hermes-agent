"""Append-only SQLite-backed event log for hermes orchestration.

Design goals:
  * **Single-table schema** — one ``events`` table, ``kind`` column
    discriminates event type. Avoids the schema sprawl of automaton's
    22 specialised tables.
  * **Default-isolated database** — defaults to ``~/.hermes/events.db``
    so it never touches the existing ``state.db`` (90 MB, full of
    messages_fts indexes). Caller can opt-in to share a DB by passing
    an explicit ``db_path``.
  * **Never blocks the agent loop** — every public method swallows
    exceptions and logs them; only the constructor may raise.
  * **Thread-safe** — single connection, RLock-guarded, with
    ``check_same_thread=False``. Hermes' concurrent tool path can
    safely emit from worker threads.
  * **Generator query** — ``query()`` yields events lazily; large
    sessions don't materialize the whole table.

Schema::

    CREATE TABLE events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms         INTEGER NOT NULL,
        session_id    TEXT NOT NULL,
        kind          TEXT NOT NULL,
        actor         TEXT NOT NULL,
        payload_json  TEXT NOT NULL
    );
    CREATE INDEX idx_events_session_ts ON events (session_id, ts_ms);
    CREATE INDEX idx_events_kind_ts    ON events (kind, ts_ms);

Usage::

    stream = EventStream(session_id="abc-123")            # uses default path
    stream.emit("loop_block", actor="loop_detector",
                payload={"rule": "REPEAT_OP", "tool": "read_file"})
    for event in stream.replay("abc-123"):
        print(event.ts_ms, event.kind, event.payload)
    stream.close()
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Union

logger = logging.getLogger(__name__)

# Constraints on the ``kind`` column. Keep it boring: lowercase ascii,
# digits, underscores, max 64 chars. Non-conforming kinds are rejected
# at emit time.
_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ACTOR_MAX_LEN = 128


@dataclass(frozen=True)
class Event:
    """A single event row, materialized from the database."""

    id: int
    ts_ms: int
    session_id: str
    kind: str
    actor: str
    payload: dict[str, Any]


def _default_db_path() -> Path:
    """Default location: ``$HERMES_HOME/events.db``, fallback ``~/.hermes/events.db``.

    Honours ``HERMES_HOME`` so the conftest's per-test tempdir works
    transparently without test-specific wiring.
    """
    base = os.environ.get("HERMES_HOME")
    if base:
        return Path(base) / "events.db"
    return Path.home() / ".hermes" / "events.db"


def _json_safe(payload: Any) -> str:
    """Serialize payload, falling back to ``repr`` for non-JSON-safe values.

    Never raises — guarantees ``emit`` can always make it to disk.
    """
    try:
        return json.dumps(payload, ensure_ascii=False, default=repr)
    except (TypeError, ValueError) as exc:
        # Pathological payload (e.g. self-referential structure)
        logger.warning("event_stream: payload serialization failed (%s)", exc)
        return json.dumps({"__serialization_error__": repr(exc)})


class EventStream:
    """Append-only event log with session-scoped queries.

    Construction is the only operation that may raise. Every other
    public method is best-effort and logs failures rather than
    propagating them — the agent loop must never break because the
    observability layer is unhealthy.
    """

    SCHEMA_SQL: tuple[str, ...] = (
        """
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms        INTEGER NOT NULL,
            session_id   TEXT NOT NULL,
            kind         TEXT NOT NULL,
            actor        TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events (session_id, ts_ms)",
        "CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events (kind, ts_ms)",
    )

    def __init__(
        self,
        session_id: str,
        db_path: Optional[Union[str, os.PathLike]] = None,
        autoflush_every: int = 1,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not session_id or not isinstance(session_id, str):
            raise ValueError("session_id must be a non-empty string")
        if autoflush_every < 1:
            raise ValueError("autoflush_every must be >= 1")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be >= 0")

        self._session_id = session_id
        self._db_path: Path = (
            Path(db_path) if db_path is not None else _default_db_path()
        )
        self._autoflush_every = autoflush_every
        self._pending_since_flush = 0
        self._closed = False
        self._lock = threading.RLock()

        # Ensure parent dir exists for file-backed DBs (skip for :memory:)
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False because we synchronize via _lock
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we control commits explicitly
        )
        self._conn.row_factory = sqlite3.Row
        # WAL gives us non-blocking reads + safer crash recovery.
        # Best-effort: in-memory or unsupported FS may reject.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as exc:
            logger.warning("event_stream: PRAGMA setup partial (%s)", exc)

        for stmt in self.SCHEMA_SQL:
            self._conn.execute(stmt)

    # ─────────────────────────── public API ───────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def db_path(self) -> Path:
        return self._db_path

    def emit(
        self,
        kind: str,
        *,
        actor: str,
        payload: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Optional[int]:
        """Append an event. Returns the row id, or ``None`` on failure.

        ``session_id`` defaults to the stream's session; pass an explicit
        value for cross-session emits (rare — usually only for tests).

        Validation:
          * ``kind`` must match ``^[a-z][a-z0-9_]{0,63}$``
          * ``actor`` is truncated to 128 chars
          * ``payload`` is JSON-encoded with ``repr`` fallback

        Failure modes that yield ``None``:
          * Stream is closed
          * ``kind`` validation failed
          * SQLite error (db locked, disk full, ...)
        """
        if self._closed:
            logger.warning("event_stream: emit on closed stream (kind=%s)", kind)
            return None
        if not isinstance(kind, str) or not _KIND_PATTERN.match(kind):
            logger.warning("event_stream: invalid kind %r rejected", kind)
            return None
        actor = actor[:_ACTOR_MAX_LEN]

        ts_ms = int(time.time() * 1000)
        payload_str = _json_safe(payload if payload is not None else {})
        sid = session_id or self._session_id

        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO events (ts_ms, session_id, kind, actor, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ts_ms, sid, kind, actor, payload_str),
                )
                row_id = int(cur.lastrowid or 0)
                self._pending_since_flush += 1
                # WAL has its own auto-checkpoint cadence (1000 pages);
                # explicitly checkpointing on every emit was 100x slower
                # than autocommit alone. We still expose autoflush_every
                # as a knob for callers that want forced sync, but the
                # default (1) no longer triggers a checkpoint.
                if (
                    self._autoflush_every > 1
                    and self._pending_since_flush >= self._autoflush_every
                ):
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except sqlite3.Error:
                        pass
                    self._pending_since_flush = 0
                return row_id
            except sqlite3.Error as exc:
                logger.warning(
                    "event_stream: insert failed kind=%s err=%s", kind, exc
                )
                return None

    def query(
        self,
        *,
        session_id: Optional[str] = None,
        kind: Optional[str] = None,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        limit: int = 1000,
    ) -> Iterator[Event]:
        """Yield events matching the filters in (ts_ms ASC, id ASC) order.

        Generator-based — caller can break early without loading the
        full result set into memory. ``limit`` caps the SQL query, not
        the consumer's loop.

        Returns an empty iterator on closed stream or SQL error.
        """
        if self._closed:
            return iter(())
        if limit < 1:
            return iter(())

        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(since_ms))
        if until_ms is not None:
            clauses.append("ts_ms <= ?")
            params.append(int(until_ms))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, ts_ms, session_id, kind, actor, payload_json "
            f"FROM events{where} ORDER BY ts_ms ASC, id ASC LIMIT ?"
        )
        params.append(int(limit))

        with self._lock:
            try:
                rows = list(self._conn.execute(sql, params).fetchall())
            except sqlite3.Error as exc:
                logger.warning("event_stream: query failed err=%s", exc)
                return iter(())

        return (self._row_to_event(row) for row in rows)

    def replay(self, session_id: str, *, limit: int = 100_000) -> Iterator[Event]:
        """Yield every event for ``session_id`` in chronological order.

        Convenience wrapper around ``query`` for the common
        "show me everything that happened in this session" case.
        """
        return self.query(session_id=session_id, limit=limit)

    def count(
        self,
        *,
        session_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> int:
        """Number of events matching the optional filters. ``-1`` on error."""
        if self._closed:
            return 0
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT COUNT(*) AS n FROM events{where}"

        with self._lock:
            try:
                row = self._conn.execute(sql, params).fetchone()
                return int(row["n"]) if row is not None else 0
            except sqlite3.Error as exc:
                logger.warning("event_stream: count failed err=%s", exc)
                return -1

    def close(self) -> None:
        """Close the underlying SQLite connection. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning("event_stream: close failed err=%s", exc)

    def __enter__(self) -> "EventStream":
        return self

    def __exit__(self, *args: Any) -> None:
        del args
        self.close()

    # ─────────────────────────── helpers ───────────────────────────

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        try:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                payload = {"__non_dict_payload__": payload}
        except (json.JSONDecodeError, TypeError):
            payload = {"__corrupt_payload__": row["payload_json"]}
        return Event(
            id=int(row["id"]),
            ts_ms=int(row["ts_ms"]),
            session_id=str(row["session_id"]),
            kind=str(row["kind"]),
            actor=str(row["actor"]),
            payload=payload,
        )
