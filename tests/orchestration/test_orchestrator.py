"""Unit tests for ``orchestration.orchestrator``."""

from __future__ import annotations

import asyncio
import json

import pytest

from orchestration.orchestrator import (
    Orchestrator,
    Phase,
)
from orchestration.plan_mode import PlanMode
from orchestration.planner import Planner
from orchestration.task_graph import Goal, TaskGraph, TaskNode


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def graph():
    g = TaskGraph(db_path=":memory:")
    yield g
    g.close()


def _good_plan(n: int = 2) -> str:
    """LLM response with N sequential tasks."""
    tasks = []
    prev = None
    for i in range(n):
        tasks.append(
            {
                "id": f"t{i}",
                "title": f"step {i}",
                "description": f"do {i}",
                "depends_on": [prev] if prev else [],
                "estimated_cost_tokens": 200,
            }
        )
        prev = f"t{i}"
    return json.dumps({"tasks": tasks, "rationale": "linear"})


def _ok_executor():
    """Executor that always succeeds."""
    async def run(_goal: Goal, task: TaskNode) -> str:
        await asyncio.sleep(0)  # yield
        return f"done {task.title}"
    return run


def _fail_then_ok_executor():
    """Fails the first call, then succeeds."""
    calls = [0]

    async def run(_goal: Goal, task: TaskNode) -> str:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("kaboom")
        return f"done {task.title}"
    return run


def _always_fail_executor():
    async def run(_goal: Goal, task: TaskNode) -> str:
        del task
        raise RuntimeError("permanent failure")
    return run


# ─────────────────────────── happy path ───────────────────────────


@pytest.mark.asyncio
async def test_full_lifecycle_completes(graph):
    planner = Planner(lambda _: _good_plan(2))
    plan_mode = PlanMode(auto_approve=True)
    orch = Orchestrator(graph, planner, plan_mode, _ok_executor())

    gid = await orch.submit_goal("ship feature")
    final = await orch.run_to_completion(gid, max_ticks=20)
    assert final.phase is Phase.COMPLETE
    goal = graph.get_goal(gid)
    assert goal is not None
    assert all(t.status.value == "completed" for t in goal.tasks)


@pytest.mark.asyncio
async def test_phase_emits_in_order(graph):
    events: list[tuple[str, dict]] = []
    planner = Planner(lambda _: _good_plan(1))
    pm = PlanMode(auto_approve=True)
    orch = Orchestrator(
        graph, planner, pm, _ok_executor(), on_event=lambda k, p: events.append((k, p))
    )

    gid = await orch.submit_goal("g")
    await orch.run_to_completion(gid)
    kinds = [k for k, _ in events]
    assert kinds[0] == "goal_submitted"
    assert "phase_change" in kinds
    assert kinds[-1] == "goal_complete"


# ─────────────────────────── replan path ───────────────────────────


@pytest.mark.asyncio
async def test_task_retry_succeeds_within_task_max_retries(graph):
    """Executor fails once, succeeds second time. Task-level retries
    handle this — no plan-level replan needed."""
    planner = Planner(lambda _: _good_plan(1), retry_on_invalid_json=0)
    orch = Orchestrator(
        graph, planner, PlanMode(), _fail_then_ok_executor(), max_replans=2
    )
    gid = await orch.submit_goal("g")
    final = await orch.run_to_completion(gid, max_ticks=30)
    assert final.phase is Phase.COMPLETE
    # Task-level retry counter incremented; plan-level replan_count stays 0
    goal = graph.get_goal(gid)
    assert goal is not None
    assert goal.tasks[0].retries >= 1


@pytest.mark.asyncio
async def test_replan_exhausted_yields_failed(graph):
    # Only one plan ever returned; executor always fails.
    planner = Planner(
        lambda _: _good_plan(1),
        retry_on_invalid_json=0,
    )
    orch = Orchestrator(
        graph,
        planner,
        PlanMode(),
        _always_fail_executor(),
        max_replans=1,
    )
    gid = await orch.submit_goal("g")
    final = await orch.run_to_completion(gid, max_ticks=30)
    assert final.phase is Phase.FAILED


# ─────────────────────────── plan_mode interaction ───────────────────────────


@pytest.mark.asyncio
async def test_pending_review_keeps_state_in_plan_review(graph):
    planner = Planner(lambda _: _good_plan(1))
    pm = PlanMode(auto_approve=False)  # all reviews are PENDING
    orch = Orchestrator(graph, planner, pm, _ok_executor())
    gid = await orch.submit_goal("g")

    # Tick until PLAN_REVIEW
    for _ in range(5):
        await orch.tick(gid)
        if orch.get_state(gid).phase is Phase.PLAN_REVIEW:  # type: ignore[union-attr]
            break

    # One more tick — should stay in PLAN_REVIEW
    res = await orch.tick(gid)
    assert res.phase is Phase.PLAN_REVIEW

    # Now approve and continue
    pm.approve(gid)
    final = await orch.run_to_completion(gid, max_ticks=20)
    assert final.phase is Phase.COMPLETE


@pytest.mark.asyncio
async def test_rejected_plan_yields_failed(graph):
    planner = Planner(lambda _: _good_plan(1))
    pm = PlanMode(auto_approve=False)
    orch = Orchestrator(graph, planner, pm, _ok_executor())
    gid = await orch.submit_goal("g")
    # Advance to PLAN_REVIEW
    for _ in range(5):
        await orch.tick(gid)
        state = orch.get_state(gid)
        if state and state.phase is Phase.PLAN_REVIEW:
            break
    pm.reject(gid, feedback="no")
    res = await orch.tick(gid)
    assert res.phase is Phase.FAILED


# ─────────────────────────── lifecycle hygiene ───────────────────────────


@pytest.mark.asyncio
async def test_terminal_tick_is_idempotent(graph):
    planner = Planner(lambda _: _good_plan(1))
    orch = Orchestrator(graph, planner, PlanMode(), _ok_executor())
    gid = await orch.submit_goal("g")
    await orch.run_to_completion(gid)
    # extra ticks should not raise or change state
    res = await orch.tick(gid)
    assert res.phase is Phase.COMPLETE
    assert "already terminal" in res.notes


@pytest.mark.asyncio
async def test_unknown_goal_raises(graph):
    orch = Orchestrator(
        graph, Planner(lambda _: _good_plan(1)), PlanMode(), _ok_executor()
    )
    with pytest.raises(KeyError):
        await orch.tick("nope")


@pytest.mark.asyncio
async def test_cancel_marks_failed(graph):
    planner = Planner(lambda _: _good_plan(1))
    orch = Orchestrator(graph, planner, PlanMode(), _ok_executor())
    gid = await orch.submit_goal("g")
    orch.cancel(gid)
    state = orch.get_state(gid)
    assert state is not None and state.phase is Phase.FAILED
    assert state.failed_error == "cancelled by caller"


@pytest.mark.asyncio
async def test_planner_error_yields_failed(graph):
    def bad_llm(_: str) -> str:
        return "not-json"
    planner = Planner(bad_llm, retry_on_invalid_json=0)
    orch = Orchestrator(graph, planner, PlanMode(), _ok_executor())
    gid = await orch.submit_goal("g")
    final = await orch.run_to_completion(gid, max_ticks=20)
    assert final.phase is Phase.FAILED


# ─────────────────────────── construction ───────────────────────────


def test_invalid_construction(graph):
    p = Planner(lambda _: _good_plan(1))
    pm = PlanMode()
    with pytest.raises(ValueError):
        Orchestrator(graph, p, pm, _ok_executor(), max_replans=-1)
    with pytest.raises(ValueError):
        Orchestrator(graph, p, pm, _ok_executor(), phase_timeout_sec=0)


@pytest.mark.asyncio
async def test_event_hook_exception_does_not_break(graph):
    def bad_hook(*_: object) -> None:
        raise RuntimeError("hook crashed")

    planner = Planner(lambda _: _good_plan(1))
    orch = Orchestrator(
        graph, planner, PlanMode(), _ok_executor(), on_event=bad_hook
    )
    gid = await orch.submit_goal("g")
    final = await orch.run_to_completion(gid)
    assert final.phase is Phase.COMPLETE
