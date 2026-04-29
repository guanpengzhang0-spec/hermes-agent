"""DAG-based task graph for the orchestrator.

Borrowed in spirit from automaton's ``src/orchestration/task-graph.ts``
but trimmed to hermes' actual needs (no funding / agent assignment /
colony lifecycle — those belong to automaton's multi-agent design).

Responsibilities:
  * Persist ``Goal`` and ``TaskNode`` records in an isolated SQLite db
  * Compute DAG operations: cycle detection, ready-task query
  * Track retries and accumulated token cost
  * Expose a snapshot for ``orchestration.attention`` to render

Concurrency:
  * Single SQLite connection guarded by an RLock
  * ``check_same_thread=False`` so workers can update task state
  * WAL mode for non-blocking reads

Storage isolation:
  * Defaults to ``$HERMES_HOME/tasks.db`` — never touches ``state.db``
    or ``events.db``. Caller may pass an explicit ``db_path`` to share.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

logger = logging.getLogger(__name__)


# ─────────────────────────── Public types ───────────────────────────


class TaskStatus(str, Enum):
    """Lifecycle of a single task. Used by both Goal and TaskNode."""

    PENDING = "pending"        # not yet runnable (waiting on deps)
    READY = "ready"            # deps satisfied, waiting for executor
    RUNNING = "running"        # executor has claimed it
    COMPLETED = "completed"
    FAILED = "failed"          # exceeded max_retries
    BLOCKED = "blocked"        # blocked on a dependency that failed
    CANCELLED = "cancelled"


# Statuses that count a Goal as "still in flight"
_ACTIVE_GOAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}
)
_TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


@dataclass(frozen=True)
class TaskNode:
    """Immutable snapshot of a task row. Mutations go through TaskGraph."""

    id: str
    goal_id: str
    title: str
    description: str
    status: TaskStatus
    depends_on: tuple[str, ...]
    estimated_cost_tokens: int = 0
    actual_cost_tokens: int = 0
    retries: int = 0
    max_retries: int = 2
    result: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Goal:
    """Immutable snapshot of a goal + its tasks."""

    id: str
    title: str
    status: TaskStatus
    created_ms: int
    tasks: tuple[TaskNode, ...] = field(default_factory=tuple)


# Event hook signature: (kind, payload) — receiver decides what to do
EventHook = Callable[[str, dict[str, Any]], None]


# ─────────────────────────── ID generation ───────────────────────────


def _ulid_like() -> str:
    """26-char roughly-sortable ID without external deps.

    Format: ``<13-hex-ms-timestamp>_<10-hex-random>``. Lexicographic
    sort closely matches creation time. Plenty of entropy for our scale.
    """
    return f"{int(time.time() * 1000):013x}_{uuid.uuid4().hex[:10]}"


# ─────────────────────────── path helpers ───────────────────────────


def _default_db_path() -> Path:
    base = os.environ.get("HERMES_HOME")
    if base:
        return Path(base) / "tasks.db"
    return Path.home() / ".hermes" / "tasks.db"


# ─────────────────────────── TaskGraph ───────────────────────────


class TaskGraph:
    """Persistent DAG of goals and tasks with cycle / readiness logic.

    Public methods either return new immutable snapshots or update
    state and emit hook events. The class itself is thread-safe via a
    single RLock.
    """

    SCHEMA_SQL: tuple[str, ...] = (
        """
        CREATE TABLE IF NOT EXISTS goals (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            status      TEXT NOT NULL,
            created_ms  INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id                    TEXT PRIMARY KEY,
            goal_id               TEXT NOT NULL,
            title                 TEXT NOT NULL,
            description           TEXT NOT NULL,
            status                TEXT NOT NULL,
            depends_on_json       TEXT NOT NULL,
            estimated_cost_tokens INTEGER NOT NULL DEFAULT 0,
            actual_cost_tokens    INTEGER NOT NULL DEFAULT 0,
            retries               INTEGER NOT NULL DEFAULT 0,
            max_retries           INTEGER NOT NULL DEFAULT 2,
            result                TEXT,
            error                 TEXT,
            FOREIGN KEY (goal_id) REFERENCES goals (id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tasks_goal_status ON tasks (goal_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_goals_status ON goals (status)",
    )

    def __init__(
        self,
        db_path: Optional[Union[str, os.PathLike]] = None,
        on_event: Optional[EventHook] = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be >= 0")

        self._db_path: Path = (
            Path(db_path) if db_path is not None else _default_db_path()
        )
        self._on_event = on_event
        self._closed = False
        self._lock = threading.RLock()

        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as exc:
            logger.warning("task_graph: PRAGMA setup partial (%s)", exc)

        for stmt in self.SCHEMA_SQL:
            self._conn.execute(stmt)

    # ─────────────────────────── creation ───────────────────────────

    def create_goal(self, title: str) -> Goal:
        """Insert a new ``Goal`` with status PENDING and no tasks."""
        if not isinstance(title, str) or not title.strip():
            raise ValueError("goal title must be a non-empty string")

        goal_id = _ulid_like()
        created_ms = int(time.time() * 1000)
        with self._lock:
            self._require_open()
            self._conn.execute(
                "INSERT INTO goals (id, title, status, created_ms) "
                "VALUES (?, ?, ?, ?)",
                (goal_id, title.strip(), TaskStatus.PENDING.value, created_ms),
            )
        goal = Goal(
            id=goal_id,
            title=title.strip(),
            status=TaskStatus.PENDING,
            created_ms=created_ms,
            tasks=(),
        )
        self._emit("goal_created", {"goal_id": goal_id, "title": title.strip()})
        return goal

    def add_task(
        self,
        goal_id: str,
        *,
        title: str,
        description: str = "",
        depends_on: Iterable[str] = (),
        estimated_cost_tokens: int = 0,
        max_retries: int = 2,
    ) -> str:
        """Insert a new task and return its id.

        Raises ``ValueError`` if the goal doesn't exist, any dep is
        unknown, or adding this task would create a cycle.
        """
        if not isinstance(title, str) or not title.strip():
            raise ValueError("task title must be a non-empty string")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if estimated_cost_tokens < 0:
            raise ValueError("estimated_cost_tokens must be >= 0")

        deps_tuple = tuple(dict.fromkeys(d for d in depends_on if d))
        task_id = _ulid_like()

        with self._lock:
            self._require_open()
            if not self._goal_exists(goal_id):
                raise ValueError(f"unknown goal_id: {goal_id!r}")
            existing_ids = {row["id"] for row in self._conn.execute(
                "SELECT id FROM tasks WHERE goal_id = ?", (goal_id,)
            )}
            unknown_deps = set(deps_tuple) - existing_ids
            if unknown_deps:
                raise ValueError(
                    f"unknown dependency task id(s): {sorted(unknown_deps)}"
                )

            # Hypothetically insert and check for cycles before commit
            self._conn.execute(
                "INSERT INTO tasks (id, goal_id, title, description, status, "
                "depends_on_json, estimated_cost_tokens, max_retries) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    goal_id,
                    title.strip(),
                    description,
                    TaskStatus.PENDING.value,
                    json.dumps(list(deps_tuple)),
                    int(estimated_cost_tokens),
                    int(max_retries),
                ),
            )
            cycles = self._detect_cycles_locked(goal_id)
            if cycles:
                # roll back our insert
                self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                raise ValueError(
                    f"adding task would create cycle(s): {cycles}"
                )

        self._emit(
            "task_added",
            {
                "goal_id": goal_id,
                "task_id": task_id,
                "title": title.strip(),
                "depends_on": list(deps_tuple),
            },
        )
        return task_id

    # ─────────────────────────── reads ───────────────────────────

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT id, title, status, created_ms FROM goals WHERE id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                return None
            tasks = tuple(self._load_tasks(goal_id))
        return self._row_to_goal(row, tasks)

    def get_task(self, task_id: str) -> Optional[TaskNode]:
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT id, goal_id, title, description, status, "
                "depends_on_json, estimated_cost_tokens, actual_cost_tokens, "
                "retries, max_retries, result, error "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list_active_goals(self) -> list[Goal]:
        active_values = [s.value for s in _ACTIVE_GOAL_STATUSES]
        placeholders = ",".join("?" * len(active_values))
        with self._lock:
            self._require_open()
            rows = list(self._conn.execute(
                f"SELECT id, title, status, created_ms FROM goals "
                f"WHERE status IN ({placeholders}) ORDER BY created_ms ASC",
                active_values,
            ))
        return [
            self._row_to_goal(row, tuple(self._load_tasks(row["id"])))
            for row in rows
        ]

    def get_ready_tasks(self, goal_id: str) -> list[TaskNode]:
        """Tasks in PENDING/READY whose dependencies are all COMPLETED."""
        tasks = self._load_tasks(goal_id)
        completed_ids: set[str] = {
            t.id for t in tasks if t.status is TaskStatus.COMPLETED
        }
        ready: list[TaskNode] = []
        for t in tasks:
            if t.status not in (TaskStatus.PENDING, TaskStatus.READY):
                continue
            if all(dep in completed_ids for dep in t.depends_on):
                ready.append(t)
        return ready

    # ─────────────────────────── DAG checks ───────────────────────────

    def detect_cycles(self, goal_id: str) -> list[list[str]]:
        """Return all simple cycles in the goal's task DAG."""
        with self._lock:
            self._require_open()
            return self._detect_cycles_locked(goal_id)

    def _detect_cycles_locked(self, goal_id: str) -> list[list[str]]:
        """Three-color DFS. Caller holds the lock."""
        nodes: dict[str, list[str]] = {}
        for row in self._conn.execute(
            "SELECT id, depends_on_json FROM tasks WHERE goal_id = ?",
            (goal_id,),
        ):
            try:
                deps = json.loads(row["depends_on_json"])
                if not isinstance(deps, list):
                    deps = []
            except (json.JSONDecodeError, TypeError):
                deps = []
            nodes[row["id"]] = [str(d) for d in deps if d in nodes or True]

        # Filter dep references to actually-existing nodes
        existing = set(nodes.keys())
        for nid in nodes:
            nodes[nid] = [d for d in nodes[nid] if d in existing]

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in nodes}
        cycles: list[list[str]] = []

        def dfs(start: str) -> None:
            stack: list[tuple[str, int]] = [(start, 0)]
            path: list[str] = []
            while stack:
                node, idx = stack[-1]
                if idx == 0:
                    if color[node] == GRAY:
                        # cycle detected — extract from path
                        try:
                            cut = path.index(node)
                            cycles.append(path[cut:] + [node])
                        except ValueError:
                            pass
                        stack.pop()
                        continue
                    if color[node] == BLACK:
                        stack.pop()
                        continue
                    color[node] = GRAY
                    path.append(node)
                deps = nodes[node]
                if idx < len(deps):
                    stack[-1] = (node, idx + 1)
                    dep = deps[idx]
                    if color.get(dep, BLACK) != BLACK:
                        stack.append((dep, 0))
                    continue
                color[node] = BLACK
                if path and path[-1] == node:
                    path.pop()
                stack.pop()

        for nid in nodes:
            if color[nid] == WHITE:
                dfs(nid)

        # de-duplicate by sorted-tuple identity
        seen: set[tuple[str, ...]] = set()
        unique: list[list[str]] = []
        for cycle in cycles:
            key = tuple(sorted(cycle))
            if key in seen:
                continue
            seen.add(key)
            unique.append(cycle)
        return unique

    # ─────────────────────────── state mutations ───────────────────────────

    def mark_started(self, task_id: str) -> None:
        with self._lock:
            self._require_open()
            self._update_task_status(
                task_id,
                allowed_from={TaskStatus.PENDING, TaskStatus.READY},
                new_status=TaskStatus.RUNNING,
            )
        self._emit("task_started", {"task_id": task_id})

    def mark_completed(
        self, task_id: str, *, result: str = "", tokens: int = 0
    ) -> None:
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        with self._lock:
            self._require_open()
            self._update_task_status(
                task_id,
                allowed_from={TaskStatus.RUNNING, TaskStatus.READY, TaskStatus.PENDING},
                new_status=TaskStatus.COMPLETED,
                extra_set={
                    "result": result,
                    "actual_cost_tokens_inc": int(tokens),
                },
            )
            goal_id = self._goal_of_task_locked(task_id)
            # Cascade: dependents whose deps are now satisfied move PENDING→READY
            self._refresh_ready(goal_id)
        # Emit task event BEFORE checking goal closure so order is logical
        self._emit(
            "task_completed",
            {"task_id": task_id, "tokens": int(tokens), "result_size": len(result)},
        )
        with self._lock:
            self._require_open()
            self._maybe_close_goal(goal_id)

    def mark_failed(self, task_id: str, *, error: str) -> bool:
        """Increment retries; if at max → terminal FAILED, return False.

        Returns True when the task is still retriable (status reset to
        PENDING / READY based on dependency state).
        """
        with self._lock:
            self._require_open()
            row = self._conn.execute(
                "SELECT goal_id, retries, max_retries FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown task_id: {task_id!r}")
            goal_id = row["goal_id"]
            new_retries = int(row["retries"]) + 1
            if new_retries > int(row["max_retries"]):
                self._conn.execute(
                    "UPDATE tasks SET status = ?, retries = ?, error = ? WHERE id = ?",
                    (TaskStatus.FAILED.value, new_retries, error, task_id),
                )
                self._mark_dependents_blocked(task_id)
                self._maybe_close_goal(goal_id)
                self._emit(
                    "task_failed",
                    {"task_id": task_id, "retries": new_retries, "error": error},
                )
                return False

            # Reset to PENDING; refresh_ready will promote to READY if deps OK
            self._conn.execute(
                "UPDATE tasks SET status = ?, retries = ?, error = ? WHERE id = ?",
                (TaskStatus.PENDING.value, new_retries, error, task_id),
            )
            self._refresh_ready(goal_id)
        self._emit(
            "task_retried",
            {"task_id": task_id, "retries": new_retries, "error": error},
        )
        return True

    def cancel_task(self, task_id: str) -> None:
        with self._lock:
            self._require_open()
            self._update_task_status(
                task_id,
                allowed_from={
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.BLOCKED,
                },
                new_status=TaskStatus.CANCELLED,
            )
        self._emit("task_cancelled", {"task_id": task_id})

    def cancel_goal(self, goal_id: str) -> None:
        with self._lock:
            self._require_open()
            if not self._goal_exists(goal_id):
                raise ValueError(f"unknown goal_id: {goal_id!r}")
            self._conn.execute(
                "UPDATE goals SET status = ? WHERE id = ?",
                (TaskStatus.CANCELLED.value, goal_id),
            )
            self._conn.execute(
                "UPDATE tasks SET status = ? WHERE goal_id = ? "
                "AND status NOT IN (?, ?, ?)",
                (
                    TaskStatus.CANCELLED.value,
                    goal_id,
                    TaskStatus.COMPLETED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                ),
            )
        self._emit("goal_cancelled", {"goal_id": goal_id})

    # ─────────────────────────── progress / view ───────────────────────────

    def goal_progress(self, goal_id: str) -> dict[str, Any]:
        """Return aggregate counters and token totals for a goal."""
        tasks = self._load_tasks(goal_id)
        counts: dict[str, int] = {s.value: 0 for s in TaskStatus}
        est = act = 0
        for t in tasks:
            counts[t.status.value] += 1
            est += t.estimated_cost_tokens
            act += t.actual_cost_tokens
        return {
            "total": len(tasks),
            **counts,
            "estimated_cost_tokens": est,
            "actual_cost_tokens": act,
        }

    def to_attention_format(self, goal_id: str) -> list[dict[str, Any]]:
        """Convert a goal's tasks into the dict shape that
        ``orchestration.attention.format_attention_block`` expects.

        Mapping:
          id          → ``id``
          title       → ``content``  (for one-line display)
          status      → ``status``
          estimated_cost_tokens / actual_cost_tokens → preserved
        """
        tasks = self._load_tasks(goal_id)
        return [
            {
                "id": t.id,
                "content": t.title,
                "status": t.status.value,
                "estimated_cost_tokens": t.estimated_cost_tokens,
                "actual_cost_tokens": t.actual_cost_tokens,
            }
            for t in tasks
        ]

    # ─────────────────────────── lifecycle ───────────────────────────

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning("task_graph: close failed err=%s", exc)

    def __enter__(self) -> "TaskGraph":
        return self

    def __exit__(self, *args: Any) -> None:
        del args
        self.close()

    # ─────────────────────────── internals ───────────────────────────

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("TaskGraph is closed")

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception as exc:  # noqa: BLE001
            # Hooks must never break the graph
            logger.warning("task_graph: on_event hook failed (%s)", exc)

    def _goal_exists(self, goal_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM goals WHERE id = ?", (goal_id,)
        ).fetchone() is not None

    def _goal_of_task_locked(self, task_id: str) -> str:
        row = self._conn.execute(
            "SELECT goal_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown task_id: {task_id!r}")
        return str(row["goal_id"])

    def _load_tasks(self, goal_id: str) -> list[TaskNode]:
        with self._lock:
            self._require_open()
            rows = list(self._conn.execute(
                "SELECT id, goal_id, title, description, status, "
                "depends_on_json, estimated_cost_tokens, actual_cost_tokens, "
                "retries, max_retries, result, error "
                "FROM tasks WHERE goal_id = ? ORDER BY id ASC",
                (goal_id,),
            ))
        return [self._row_to_task(row) for row in rows]

    def _update_task_status(
        self,
        task_id: str,
        *,
        allowed_from: set[TaskStatus],
        new_status: TaskStatus,
        extra_set: Optional[dict[str, Any]] = None,
    ) -> None:
        row = self._conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown task_id: {task_id!r}")
        current = TaskStatus(row["status"])
        if current not in allowed_from:
            raise ValueError(
                f"illegal transition {current.value} → {new_status.value} "
                f"for task {task_id!r}"
            )

        sets = ["status = ?"]
        params: list[Any] = [new_status.value]
        if extra_set:
            for k, v in extra_set.items():
                if k == "actual_cost_tokens_inc":
                    sets.append("actual_cost_tokens = actual_cost_tokens + ?")
                    params.append(int(v))
                elif k == "result":
                    sets.append("result = ?")
                    params.append(v)
                elif k == "error":
                    sets.append("error = ?")
                    params.append(v)
                else:
                    raise ValueError(f"unknown extra_set key: {k!r}")
        params.append(task_id)
        self._conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
        )

    def _refresh_ready(self, goal_id: str) -> None:
        """Promote PENDING tasks to READY when their deps complete."""
        tasks = self._load_tasks_locked(goal_id)
        completed: set[str] = {
            t.id for t in tasks if t.status is TaskStatus.COMPLETED
        }
        for t in tasks:
            if t.status is not TaskStatus.PENDING:
                continue
            if all(dep in completed for dep in t.depends_on):
                self._conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?",
                    (TaskStatus.READY.value, t.id),
                )

    def _load_tasks_locked(self, goal_id: str) -> list[TaskNode]:
        """Same as _load_tasks but assumes the lock is already held."""
        rows = list(self._conn.execute(
            "SELECT id, goal_id, title, description, status, "
            "depends_on_json, estimated_cost_tokens, actual_cost_tokens, "
            "retries, max_retries, result, error "
            "FROM tasks WHERE goal_id = ? ORDER BY id ASC",
            (goal_id,),
        ))
        return [self._row_to_task(row) for row in rows]

    def _mark_dependents_blocked(self, failed_task_id: str) -> None:
        """When a task FAILs terminally, its non-terminal dependents go BLOCKED."""
        # Walk all tasks in the same goal to find dependents
        row = self._conn.execute(
            "SELECT goal_id FROM tasks WHERE id = ?", (failed_task_id,)
        ).fetchone()
        if row is None:
            return
        goal_id = row["goal_id"]
        for t in self._load_tasks_locked(goal_id):
            if t.status in _TERMINAL_TASK_STATUSES:
                continue
            if failed_task_id in t.depends_on:
                self._conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?",
                    (TaskStatus.BLOCKED.value, t.id),
                )

    def _maybe_close_goal(self, goal_id: str) -> None:
        """Move goal to COMPLETED / FAILED when no actionable tasks remain."""
        tasks = self._load_tasks_locked(goal_id)
        if not tasks:
            return
        if all(t.status is TaskStatus.COMPLETED for t in tasks):
            self._conn.execute(
                "UPDATE goals SET status = ? WHERE id = ?",
                (TaskStatus.COMPLETED.value, goal_id),
            )
            self._emit("goal_completed", {"goal_id": goal_id})
            return
        # If any task is FAILED or BLOCKED and nothing actionable remains
        actionable = any(
            t.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING)
            for t in tasks
        )
        has_failure = any(
            t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED) for t in tasks
        )
        if not actionable and has_failure:
            self._conn.execute(
                "UPDATE goals SET status = ? WHERE id = ?",
                (TaskStatus.FAILED.value, goal_id),
            )
            self._emit("goal_failed", {"goal_id": goal_id})

    @staticmethod
    def _row_to_goal(row: sqlite3.Row, tasks: tuple[TaskNode, ...]) -> Goal:
        return Goal(
            id=str(row["id"]),
            title=str(row["title"]),
            status=TaskStatus(row["status"]),
            created_ms=int(row["created_ms"]),
            tasks=tasks,
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskNode:
        try:
            deps_raw = json.loads(row["depends_on_json"])
            if not isinstance(deps_raw, list):
                deps_raw = []
        except (json.JSONDecodeError, TypeError):
            deps_raw = []
        return TaskNode(
            id=str(row["id"]),
            goal_id=str(row["goal_id"]),
            title=str(row["title"]),
            description=str(row["description"]),
            status=TaskStatus(row["status"]),
            depends_on=tuple(str(d) for d in deps_raw),
            estimated_cost_tokens=int(row["estimated_cost_tokens"]),
            actual_cost_tokens=int(row["actual_cost_tokens"]),
            retries=int(row["retries"]),
            max_retries=int(row["max_retries"]),
            result=row["result"],
            error=row["error"],
        )
