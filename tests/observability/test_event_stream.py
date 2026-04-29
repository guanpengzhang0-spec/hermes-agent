"""Unit tests for ``observability.event_stream``.

Coverage targets:
  * emit + immediate readback
  * cross-session isolation in queries
  * kind / since_ms / until_ms / limit filters
  * replay returns chronological order
  * non-JSON-serializable payload falls back to repr (does not raise)
  * invalid kind rejected (returns None, no row written)
  * close() makes emit return None and is idempotent
  * concurrent emits from many threads do not lose rows
  * default db_path honours HERMES_HOME
  * persistence across instances on the same file path
  * count() with and without filters
  * payload edge cases (datetime, Path, nested dict)
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

import pytest

from observability.event_stream import Event, EventStream


# ────────────────────────── basic emit / read ──────────────────────────


def test_emit_and_readback_via_replay():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        rid = stream.emit(
            "loop_block", actor="loop_detector", payload={"rule": "REPEAT_OP"}
        )
        assert isinstance(rid, int) and rid > 0

        events = list(stream.replay("s1"))
        assert len(events) == 1
        assert isinstance(events[0], Event)
        assert events[0].kind == "loop_block"
        assert events[0].actor == "loop_detector"
        assert events[0].payload == {"rule": "REPEAT_OP"}
        assert events[0].session_id == "s1"
        assert events[0].id == rid


def test_replay_chronological_order(tmp_path: Path):
    db = tmp_path / "events.db"
    with EventStream(session_id="s1", db_path=db) as stream:
        ids = [
            stream.emit("step", actor="a", payload={"i": i}) for i in range(5)
        ]
        events = list(stream.replay("s1"))
    assert [e.payload["i"] for e in events] == [0, 1, 2, 3, 4]
    assert [e.id for e in events] == ids


def test_count_without_and_with_filter():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        for _ in range(3):
            stream.emit("a", actor="x", payload={})
        for _ in range(2):
            stream.emit("b", actor="x", payload={})
        assert stream.count() == 5
        assert stream.count(kind="a") == 3
        assert stream.count(kind="b") == 2
        assert stream.count(session_id="other") == 0


# ────────────────────────── filters ──────────────────────────


def test_session_isolation():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        stream.emit("k", actor="x", payload={"who": "s1"})
        stream.emit("k", actor="x", payload={"who": "s2"}, session_id="s2")

        assert [e.payload["who"] for e in stream.replay("s1")] == ["s1"]
        assert [e.payload["who"] for e in stream.replay("s2")] == ["s2"]


def test_kind_filter():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        stream.emit("a", actor="x", payload={})
        stream.emit("b", actor="x", payload={})
        stream.emit("a", actor="x", payload={})
        events = list(stream.query(kind="a"))
        assert len(events) == 2
        assert all(e.kind == "a" for e in events)


def test_since_until_filter(tmp_path: Path):
    """Use raw SQL inserts with explicit ts_ms so we control timing."""
    db = tmp_path / "ev.db"
    with EventStream(session_id="s1", db_path=db) as stream:
        # bypass emit() to set explicit ts_ms via SQL
        stream._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO events (ts_ms, session_id, kind, actor, payload_json) "
            "VALUES (1000, 's1', 'a', 'x', '{}')"
        )
        stream._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO events (ts_ms, session_id, kind, actor, payload_json) "
            "VALUES (2000, 's1', 'a', 'x', '{}')"
        )
        stream._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO events (ts_ms, session_id, kind, actor, payload_json) "
            "VALUES (3000, 's1', 'a', 'x', '{}')"
        )

        assert [e.ts_ms for e in stream.query(since_ms=2000)] == [2000, 3000]
        assert [e.ts_ms for e in stream.query(until_ms=2000)] == [1000, 2000]
        assert [e.ts_ms for e in stream.query(since_ms=1500, until_ms=2500)] == [
            2000
        ]


def test_limit_caps_returned_rows():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        for i in range(10):
            stream.emit("step", actor="x", payload={"i": i})
        events = list(stream.query(limit=3))
        assert len(events) == 3


def test_query_returns_empty_iter_when_zero_limit():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        stream.emit("k", actor="x", payload={})
        assert list(stream.query(limit=0)) == []


# ────────────────────────── payload edge cases ──────────────────────────


def test_non_json_payload_falls_back_to_repr():
    """datetime / Path / custom objects must not crash emit()."""
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        rid = stream.emit(
            "audit",
            actor="x",
            payload={
                "when": datetime(2026, 4, 29, 12, 0, 0),
                "where": Path("/tmp/x"),
            },
        )
        assert rid is not None
        events = list(stream.replay("s1"))
        assert len(events) == 1
        # repr() representation is preserved verbatim
        assert "datetime.datetime" in events[0].payload["when"]


def test_nested_dict_payload_preserved():
    payload = {"outer": {"inner": [1, 2, 3], "flag": True}}
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        stream.emit("k", actor="x", payload=payload)
        events = list(stream.replay("s1"))
    assert events[0].payload == payload


def test_self_referential_payload_does_not_crash():
    bad: dict = {}
    bad["self"] = bad
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        rid = stream.emit("k", actor="x", payload=bad)
        # emit returns successfully with serialization-error sentinel
        assert rid is not None
        events = list(stream.replay("s1"))
        assert "__serialization_error__" in events[0].payload


def test_none_payload_becomes_empty_dict():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        rid = stream.emit("k", actor="x")
        assert rid is not None
        events = list(stream.replay("s1"))
        assert events[0].payload == {}


# ────────────────────────── validation ──────────────────────────


@pytest.mark.parametrize(
    "kind",
    [
        "",
        "Has-Hyphens",
        "UPPER",
        "0starts_with_digit",
        "has spaces",
        "x" * 65,
        None,
    ],
)
def test_invalid_kind_returns_none(kind):
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        rid = stream.emit(kind, actor="x", payload={})  # type: ignore[arg-type]
        assert rid is None
        assert stream.count() == 0


def test_actor_truncated_to_max_length():
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        stream.emit("k", actor="A" * 500, payload={})
        events = list(stream.replay("s1"))
        assert len(events[0].actor) == 128


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"session_id": None},
        {"session_id": "s1", "autoflush_every": 0},
        {"session_id": "s1", "busy_timeout_ms": -1},
    ],
)
def test_invalid_constructor_raises(kwargs):
    with pytest.raises(ValueError):
        EventStream(db_path=":memory:", **kwargs)  # type: ignore[arg-type]


# ────────────────────────── lifecycle ──────────────────────────


def test_close_is_idempotent_and_blocks_emit():
    stream = EventStream(session_id="s1", db_path=":memory:")
    stream.close()
    stream.close()  # no raise
    assert stream.emit("k", actor="x", payload={}) is None
    assert list(stream.query()) == []
    assert stream.count() == 0


def test_persistence_across_instances(tmp_path: Path):
    db = tmp_path / "persist.db"
    with EventStream(session_id="s1", db_path=db) as a:
        a.emit("k", actor="x", payload={"v": 1})
    with EventStream(session_id="s1", db_path=db) as b:
        events = list(b.replay("s1"))
    assert len(events) == 1
    assert events[0].payload == {"v": 1}


def test_default_db_path_honours_hermes_home(tmp_path: Path, monkeypatch):
    """conftest already sets HERMES_HOME to a tempdir; verify it's used."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with EventStream(session_id="s1") as stream:
        assert stream.db_path == tmp_path / "events.db"
        stream.emit("k", actor="x", payload={})
        assert (tmp_path / "events.db").exists()


def test_parent_dir_auto_created(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c" / "ev.db"
    with EventStream(session_id="s1", db_path=nested) as stream:
        stream.emit("k", actor="x", payload={})
    assert nested.exists()


# ────────────────────────── concurrency ──────────────────────────


def test_concurrent_emits_do_not_lose_rows(tmp_path: Path):
    db = tmp_path / "concurrent.db"
    n_threads = 8
    n_per_thread = 50
    errors: list[BaseException] = []

    with EventStream(session_id="s1", db_path=db) as stream:

        def worker(idx: int) -> None:
            try:
                for i in range(n_per_thread):
                    rid = stream.emit(
                        "step", actor=f"w{idx}", payload={"thread": idx, "i": i}
                    )
                    assert rid is not None
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert stream.count() == n_threads * n_per_thread


def test_busy_timeout_setting_does_not_crash():
    """Smoke test: extreme busy_timeout should still construct."""
    with EventStream(session_id="s1", db_path=":memory:", busy_timeout_ms=0) as s:
        assert s.emit("k", actor="x", payload={}) is not None


# ────────────────────────── corruption resilience ──────────────────────────


def test_corrupt_payload_json_returned_with_marker(tmp_path: Path):
    """If something writes garbage into payload_json, query should not raise."""
    db = tmp_path / "corrupt.db"
    with EventStream(session_id="s1", db_path=db) as stream:
        stream._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO events (ts_ms, session_id, kind, actor, payload_json) "
            "VALUES (1, 's1', 'k', 'x', 'not-json{')"
        )
        events = list(stream.replay("s1"))
    assert len(events) == 1
    assert "__corrupt_payload__" in events[0].payload


# ────────────────────────── HERMES_HOME isolation sanity ──────────────────────────


def test_db_path_explicit_overrides_hermes_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/should/not/be/used")
    explicit = tmp_path / "explicit.db"
    with EventStream(session_id="s1", db_path=explicit) as stream:
        assert stream.db_path == explicit
    assert explicit.exists()
    assert not Path("/should/not/be/used/events.db").exists()


# ────────────────────────── property: stream type ──────────────────────────


def test_session_id_property():
    with EventStream(session_id="abc-123", db_path=":memory:") as stream:
        assert stream.session_id == "abc-123"


def test_event_dataclass_is_frozen():
    """Event must be immutable so callers can't mutate query results."""
    with EventStream(session_id="s1", db_path=":memory:") as stream:
        stream.emit("k", actor="x", payload={"v": 1})
        event = next(iter(stream.replay("s1")))
    with pytest.raises((AttributeError, Exception)):
        event.kind = "mutated"  # type: ignore[misc]
