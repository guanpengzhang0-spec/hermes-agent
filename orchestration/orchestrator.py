"""State-machine orchestrator for complex multi-step goals.

Wires together ``Planner`` + ``PlanMode`` + ``TaskGraph`` + (optional)
``EventStream`` into a tickable phase machine:

    IDLE → CLASSIFYING → PLANNING → PLAN_REVIEW → EXECUTING
                                                   │ (failure + retries left)
                                                   ▼
                                              REPLANNING ──┐
                                                           │
                                                  (loops back to PLAN_REVIEW)
                                                   │ (replans exhausted)
                                                   ▼
                                              COMPLETE / FAILED

The orchestrator is **opt-in** — hermes' ordinary single-turn ReAct
loop is unaffected unless the user explicitly invokes ``/plan`` (or a
tool calls ``submit_goal``).

Each ``tick()`` advances at most one phase. The caller is responsible
for calling ``tick()`` repeatedly until the phase reaches a terminal
state. This keeps the orchestrator non-blocking — callers can interleave
ticks with other work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from .plan_mode import PlanMode, ReviewVerdict
from .planner import Planner, PlannerInput, PlannerOutput
from .task_graph import Goal, TaskGraph, TaskNode, TaskStatus

logger = logging.getLogger(__name__)


# ─────────────────────────── public types ───────────────────────────


class Phase(str, Enum):
    IDLE = "idle"
    CLASSIFYING = "classifying"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    EXECUTING = "executing"
    REPLANNING = "replanning"
    COMPLETE = "complete"
    FAILED = "failed"


_TERMINAL_PHASES: frozenset[Phase] = frozenset({Phase.COMPLETE, Phase.FAILED})


@dataclass
class OrchestratorState:
    """Mutable state for one in-flight goal.

    Lives in the orchestrator's per-goal dict; persisted to TaskGraph
    via the ``goals`` table (status field). Replan count and last
    PlannerOutput live only in memory — restart loses them but the
    goal can be re-submitted.
    """

    phase: Phase
    goal_id: str
    replan_count: int = 0
    failed_task_id: Optional[str] = None
    failed_error: Optional[str] = None
    phase_started_ms: int = 0
    last_plan: Optional[PlannerOutput] = None
    available_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class TickResult:
    phase: Phase
    goal_id: str
    tasks_assigned: int
    tasks_completed: int
    tasks_failed: int
    notes: tuple[str, ...] = field(default_factory=tuple)


# Caller-supplied: actually run a single task, return its result text.
TaskExecutor = Callable[[Goal, TaskNode], Awaitable[str]]


# ─────────────────────────── Orchestrator ───────────────────────────


class Orchestrator:
    """Drives goals through the planning + execution lifecycle.

    The class is async because ``TaskExecutor`` runs the LLM/tools and
    is naturally async. Internal state mutations are protected by an
    asyncio lock so concurrent ticks for the same goal are serialized.
    """

    def __init__(
        self,
        task_graph: TaskGraph,
        planner: Planner,
        plan_mode: PlanMode,
        executor: TaskExecutor,
        *,
        max_replans: int = 3,
        phase_timeout_sec: int = 300,
        on_event: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> None:
        if max_replans < 0:
            raise ValueError("max_replans must be >= 0")
        if phase_timeout_sec < 1:
            raise ValueError("phase_timeout_sec must be >= 1")

        self._graph = task_graph
        self._planner = planner
        self._plan_mode = plan_mode
        self._executor = executor
        self._max_replans = max_replans
        self._phase_timeout_sec = phase_timeout_sec
        self._on_event = on_event

        self._states: dict[str, OrchestratorState] = {}
        self._lock = asyncio.Lock()

    # ─────────────────────────── public API ───────────────────────────

    async def submit_goal(
        self,
        title: str,
        *,
        description: str = "",
        available_tools: tuple[str, ...] = (),
    ) -> str:
        """Create the goal in TaskGraph and prepare the IDLE state."""
        goal = self._graph.create_goal(title)
        async with self._lock:
            self._states[goal.id] = OrchestratorState(
                phase=Phase.IDLE,
                goal_id=goal.id,
                phase_started_ms=int(time.time() * 1000),
                available_tools=available_tools,
            )
        # Stash description on the state for later access via classifier
        # (we don't have a separate description column on Goal).
        self._states[goal.id].failed_error = (
            None  # placeholder — keep fields consistent
        )
        if description:
            # Use last_plan slot opportunistically for description carry
            # — it's None at this point so harmless. (Cleaner alternative
            # would be a new field; keeping data-model surface small.)
            self._states[goal.id].failed_error = description
        self._emit("goal_submitted", {"goal_id": goal.id, "title": title})
        return goal.id

    async def tick(self, goal_id: str) -> TickResult:
        """Advance one phase. Idempotent if already terminal."""
        async with self._lock:
            state = self._states.get(goal_id)
            if state is None:
                raise KeyError(f"unknown goal_id: {goal_id!r}")

            if state.phase in _TERMINAL_PHASES:
                return self._make_tick_result(state, notes=("already terminal",))

            # Guard against runaway phase
            if self._phase_timed_out(state):
                state.phase = Phase.FAILED
                state.failed_error = (
                    state.failed_error or ""
                ) + f" [phase {state.phase.value} timed out]"
                self._graph.cancel_goal(goal_id)
                self._emit(
                    "phase_timeout",
                    {"goal_id": goal_id, "phase": state.phase.value},
                )
                return self._make_tick_result(state, notes=("phase timeout",))

        # Dispatch outside the lock so executors can run async work
        if state.phase is Phase.IDLE:
            return await self._handle_idle(state)
        if state.phase is Phase.CLASSIFYING:
            return await self._handle_classifying(state)
        if state.phase is Phase.PLANNING:
            return await self._handle_planning(state)
        if state.phase is Phase.PLAN_REVIEW:
            return await self._handle_plan_review(state)
        if state.phase is Phase.EXECUTING:
            return await self._handle_executing(state)
        if state.phase is Phase.REPLANNING:
            return await self._handle_replanning(state)
        # Should be unreachable due to terminal-guard above
        return self._make_tick_result(state)

    def get_state(self, goal_id: str) -> Optional[OrchestratorState]:
        return self._states.get(goal_id)

    def cancel(self, goal_id: str) -> None:
        state = self._states.get(goal_id)
        if state is None or state.phase in _TERMINAL_PHASES:
            return
        self._graph.cancel_goal(goal_id)
        state.phase = Phase.FAILED
        state.failed_error = "cancelled by caller"
        self._emit("goal_cancelled", {"goal_id": goal_id})

    async def run_to_completion(
        self, goal_id: str, *, max_ticks: int = 50
    ) -> OrchestratorState:
        """Convenience helper for tests / scripted runs."""
        for _ in range(max_ticks):
            await self.tick(goal_id)
            state = self._states[goal_id]
            if state.phase in _TERMINAL_PHASES:
                return state
        raise RuntimeError(
            f"goal {goal_id!r} did not terminate within {max_ticks} ticks"
        )

    # ─────────────────────────── phase handlers ───────────────────────────

    async def _handle_idle(self, state: OrchestratorState) -> TickResult:
        return self._transition(state, Phase.CLASSIFYING)

    async def _handle_classifying(
        self, state: OrchestratorState
    ) -> TickResult:
        # Lightweight: if the goal has any pre-existing tasks, skip
        # planning. Otherwise fall through to the planner.
        goal = self._graph.get_goal(state.goal_id)
        if goal is None:
            return self._fail(state, "goal disappeared during classification")
        if goal.tasks:
            return self._transition(state, Phase.EXECUTING)
        return self._transition(state, Phase.PLANNING)

    async def _handle_planning(self, state: OrchestratorState) -> TickResult:
        goal = self._graph.get_goal(state.goal_id)
        if goal is None:
            return self._fail(state, "goal disappeared during planning")

        try:
            plan = self._planner.plan(
                PlannerInput(
                    goal_title=goal.title,
                    goal_description=state.failed_error or "",
                    available_tools=state.available_tools,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(state, f"planner error: {exc}")

        state.last_plan = plan
        # Materialise plan into TaskGraph
        local_to_real: dict[str, str] = {}
        try:
            for kw, planned in zip(
                self._planner.to_task_nodes(plan, state.goal_id),
                plan.tasks,
            ):
                # Translate planner-local deps to TaskGraph IDs
                kw["depends_on"] = [
                    local_to_real[d] for d in kw["depends_on"]
                ]
                real_id = self._graph.add_task(state.goal_id, **kw)
                local_to_real[planned.local_id] = real_id
        except Exception as exc:  # noqa: BLE001
            return self._fail(state, f"task graph rejected plan: {exc}")

        return self._transition(state, Phase.PLAN_REVIEW)

    async def _handle_plan_review(
        self, state: OrchestratorState
    ) -> TickResult:
        if state.last_plan is None:
            return self._fail(state, "plan_review without a plan")
        # Only seed the review on first entry; afterwards check existing
        # state so the user's approve/reject is not overwritten.
        existing = self._plan_mode.get(state.goal_id)
        if existing is None or existing.verdict is ReviewVerdict.PENDING:
            if existing is None:
                review = self._plan_mode.review(state.goal_id, state.last_plan)
            else:
                review = existing
        else:
            review = existing
        if review.verdict is ReviewVerdict.APPROVED:
            return self._transition(state, Phase.EXECUTING)
        if review.verdict is ReviewVerdict.REJECTED:
            return self._fail(state, f"plan rejected: {review.feedback or ''}")
        if review.verdict is ReviewVerdict.NEEDS_CHANGES:
            if state.replan_count >= self._max_replans:
                return self._fail(state, "max replans reached during review")
            state.replan_count += 1
            return self._transition(state, Phase.REPLANNING)
        # PENDING — stay in PLAN_REVIEW; caller will tick again later
        return self._make_tick_result(state, notes=("awaiting reviewer",))

    async def _handle_executing(self, state: OrchestratorState) -> TickResult:
        goal = self._graph.get_goal(state.goal_id)
        if goal is None:
            return self._fail(state, "goal disappeared during execution")

        ready = self._graph.get_ready_tasks(state.goal_id)
        if not ready:
            # Either we're done or stuck waiting; let TaskGraph decide
            refreshed = self._graph.get_goal(state.goal_id)
            if refreshed is None:
                return self._fail(state, "goal vanished mid-execution")
            if refreshed.status is TaskStatus.COMPLETED:
                state.phase = Phase.COMPLETE
                self._emit("goal_complete", {"goal_id": state.goal_id})
                return self._make_tick_result(state)
            if refreshed.status is TaskStatus.FAILED:
                # Try to replan if budget allows
                if state.replan_count < self._max_replans:
                    state.replan_count += 1
                    return self._transition(state, Phase.REPLANNING)
                return self._fail(state, "all tasks failed and no replans left")
            return self._make_tick_result(state, notes=("waiting for ready tasks",))

        task = ready[0]
        self._graph.mark_started(task.id)
        try:
            result = await self._executor(goal, task)
        except Exception as exc:  # noqa: BLE001
            still_retriable = self._graph.mark_failed(
                task.id, error=str(exc)
            )
            if not still_retriable and state.replan_count < self._max_replans:
                state.replan_count += 1
                state.failed_task_id = task.id
                state.failed_error = str(exc)
                return self._transition(state, Phase.REPLANNING)
            return self._make_tick_result(
                state,
                tasks_failed=1,
                notes=(f"task {task.id} failed: {exc}",),
            )

        self._graph.mark_completed(task.id, result=str(result))
        return self._make_tick_result(state, tasks_completed=1)

    async def _handle_replanning(
        self, state: OrchestratorState
    ) -> TickResult:
        if state.last_plan is None:
            return self._fail(state, "replan with no prior plan")

        if state.failed_task_id is None:
            # No specific failed task → re-derive from graph
            goal = self._graph.get_goal(state.goal_id)
            if goal is None:
                return self._fail(state, "goal disappeared during replan")
            failed = next(
                (t for t in goal.tasks if t.status is TaskStatus.FAILED),
                None,
            )
            if failed is None:
                # Nothing to replan around → back to executing
                return self._transition(state, Phase.EXECUTING)
            state.failed_task_id = failed.id
            state.failed_error = failed.error or "unknown"

        failed_task = self._graph.get_task(state.failed_task_id)
        if failed_task is None:
            return self._fail(state, "failed task disappeared during replan")

        try:
            new_plan = self._planner.replan(
                state.last_plan,
                failed_task,
                error=state.failed_error or "unknown",
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(state, f"replanner error: {exc}")

        state.last_plan = new_plan
        # Cancel the failed task's open dependents — they are about to
        # be replaced by new tasks. (Already-COMPLETED tasks stay.)
        goal = self._graph.get_goal(state.goal_id)
        if goal:
            for t in goal.tasks:
                if t.status in (
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.BLOCKED,
                    TaskStatus.FAILED,
                ):
                    try:
                        self._graph.cancel_task(t.id)
                    except ValueError:
                        pass

        local_to_real: dict[str, str] = {}
        try:
            for kw, planned in zip(
                self._planner.to_task_nodes(new_plan, state.goal_id),
                new_plan.tasks,
            ):
                kw["depends_on"] = [local_to_real[d] for d in kw["depends_on"]]
                real_id = self._graph.add_task(state.goal_id, **kw)
                local_to_real[planned.local_id] = real_id
        except Exception as exc:  # noqa: BLE001
            return self._fail(state, f"task graph rejected replan: {exc}")

        state.failed_task_id = None
        state.failed_error = None
        return self._transition(state, Phase.PLAN_REVIEW)

    # ─────────────────────────── helpers ───────────────────────────

    def _transition(self, state: OrchestratorState, target: Phase) -> TickResult:
        prior = state.phase
        state.phase = target
        state.phase_started_ms = int(time.time() * 1000)
        self._emit(
            "phase_change",
            {
                "goal_id": state.goal_id,
                "from": prior.value,
                "to": target.value,
            },
        )
        return self._make_tick_result(state)

    def _fail(self, state: OrchestratorState, reason: str) -> TickResult:
        state.phase = Phase.FAILED
        state.failed_error = reason
        try:
            self._graph.cancel_goal(state.goal_id)
        except ValueError:
            pass
        self._emit("goal_failed", {"goal_id": state.goal_id, "reason": reason})
        return self._make_tick_result(state, notes=(reason,))

    def _phase_timed_out(self, state: OrchestratorState) -> bool:
        if state.phase_started_ms == 0:
            return False
        elapsed_sec = (time.time() * 1000 - state.phase_started_ms) / 1000.0
        return elapsed_sec > self._phase_timeout_sec

    def _make_tick_result(
        self,
        state: OrchestratorState,
        *,
        tasks_assigned: int = 0,
        tasks_completed: int = 0,
        tasks_failed: int = 0,
        notes: tuple[str, ...] = (),
    ) -> TickResult:
        return TickResult(
            phase=state.phase,
            goal_id=state.goal_id,
            tasks_assigned=tasks_assigned,
            tasks_completed=tasks_completed,
            tasks_failed=tasks_failed,
            notes=notes,
        )

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator: on_event failed (%s)", exc)
