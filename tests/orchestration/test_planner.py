"""Unit tests for ``orchestration.planner``."""

from __future__ import annotations

import json

import pytest

from orchestration.planner import (
    Planner,
    PlannerInput,
    PlannerOutput,
)


def _good_response(n_tasks: int = 2) -> str:
    """Build a valid LLM response with N sequential tasks."""
    tasks = []
    prev = None
    for i in range(n_tasks):
        deps = [prev] if prev else []
        tasks.append(
            {
                "id": f"t{i}",
                "title": f"step {i}",
                "description": f"do step {i}",
                "depends_on": deps,
                "estimated_cost_tokens": 200,
            }
        )
        prev = f"t{i}"
    return json.dumps({"tasks": tasks, "rationale": "linear chain"})


def _make_llm(response: str):
    return lambda _prompt: response


# ───────────────────────── happy path ─────────────────────────


def test_plan_returns_validated_output():
    p = Planner(_make_llm(_good_response(3)))
    inp = PlannerInput(goal_title="ship feature")
    out = p.plan(inp)
    assert isinstance(out, PlannerOutput)
    assert len(out.tasks) == 3
    assert out.tasks[0].local_id == "t0"
    assert out.tasks[1].depends_on == ("t0",)
    assert out.estimated_total_tokens == 600
    assert "linear" in out.rationale


def test_plan_strips_markdown_fences():
    raw = "```json\n" + _good_response(2) + "\n```"
    p = Planner(_make_llm(raw))
    out = p.plan(PlannerInput(goal_title="x"))
    assert len(out.tasks) == 2


def test_to_task_nodes_returns_kwargs_lists():
    p = Planner(_make_llm(_good_response(2)))
    out = p.plan(PlannerInput(goal_title="x"))
    kwargs_list = p.to_task_nodes(out, goal_id="g1")
    assert len(kwargs_list) == 2
    assert all("title" in k for k in kwargs_list)
    assert all("depends_on" in k for k in kwargs_list)
    assert kwargs_list[1]["depends_on"] == ["t0"]


# ───────────────────────── input handling ─────────────────────────


def test_constraints_and_tools_appear_in_prompt():
    captured: list[str] = []

    def llm(prompt: str) -> str:
        captured.append(prompt)
        return _good_response(1)

    p = Planner(llm)
    p.plan(
        PlannerInput(
            goal_title="x",
            available_tools=("write_file", "exec"),
            constraints={"deadline": "2026-05-01"},
        )
    )
    assert "write_file" in captured[0]
    assert "exec" in captured[0]
    assert "deadline" in captured[0]


# ───────────────────────── validation ─────────────────────────


def test_invalid_json_triggers_retry_then_fails():
    attempts = [0]

    def llm(prompt: str) -> str:
        attempts[0] += 1
        return "not-json{"

    p = Planner(llm, retry_on_invalid_json=2)
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        p.plan(PlannerInput(goal_title="x"))
    assert attempts[0] == 3


def test_retry_succeeds_on_second_attempt():
    responses = iter(["bad", _good_response(1)])
    p = Planner(lambda _prompt: next(responses), retry_on_invalid_json=2)
    out = p.plan(PlannerInput(goal_title="x"))
    assert len(out.tasks) == 1


def test_too_many_tasks_rejected():
    p = Planner(_make_llm(_good_response(20)), max_tasks_per_plan=10)
    with pytest.raises(RuntimeError, match="max is 10"):
        p.plan(PlannerInput(goal_title="x"))


def test_zero_tasks_rejected():
    payload = json.dumps({"tasks": [], "rationale": "empty"})
    p = Planner(_make_llm(payload), retry_on_invalid_json=0)
    with pytest.raises(RuntimeError, match="at least 1 task"):
        p.plan(PlannerInput(goal_title="x"))


def test_duplicate_id_rejected():
    payload = json.dumps(
        {
            "tasks": [
                {"id": "a", "title": "A", "depends_on": [], "estimated_cost_tokens": 200},
                {"id": "a", "title": "B", "depends_on": [], "estimated_cost_tokens": 200},
            ],
            "rationale": "x",
        }
    )
    p = Planner(_make_llm(payload), retry_on_invalid_json=0)
    with pytest.raises(RuntimeError, match="duplicate task id"):
        p.plan(PlannerInput(goal_title="x"))


def test_forward_ref_rejected():
    payload = json.dumps(
        {
            "tasks": [
                {
                    "id": "a",
                    "title": "A",
                    "depends_on": ["b"],
                    "estimated_cost_tokens": 200,
                },
                {
                    "id": "b",
                    "title": "B",
                    "depends_on": [],
                    "estimated_cost_tokens": 200,
                },
            ],
            "rationale": "x",
        }
    )
    p = Planner(_make_llm(payload), retry_on_invalid_json=0)
    with pytest.raises(RuntimeError, match="forward refs not allowed"):
        p.plan(PlannerInput(goal_title="x"))


def test_token_estimate_outside_bounds_rejected():
    payload = json.dumps(
        {
            "tasks": [
                {
                    "id": "a",
                    "title": "A",
                    "depends_on": [],
                    "estimated_cost_tokens": 99999,
                }
            ],
            "rationale": "x",
        }
    )
    p = Planner(_make_llm(payload), retry_on_invalid_json=0)
    with pytest.raises(RuntimeError, match="outside"):
        p.plan(PlannerInput(goal_title="x"))


def test_missing_title_rejected():
    payload = json.dumps(
        {
            "tasks": [
                {
                    "id": "a",
                    "title": "",
                    "depends_on": [],
                    "estimated_cost_tokens": 200,
                }
            ],
            "rationale": "x",
        }
    )
    p = Planner(_make_llm(payload), retry_on_invalid_json=0)
    with pytest.raises(RuntimeError, match="missing non-empty 'title'"):
        p.plan(PlannerInput(goal_title="x"))


# ───────────────────────── replan ─────────────────────────


def test_replan_includes_failure_context():
    captured: list[str] = []

    def llm(prompt: str) -> str:
        captured.append(prompt)
        return _good_response(1)

    p = Planner(llm)
    original = p.plan(PlannerInput(goal_title="orig"))

    from orchestration.task_graph import TaskNode, TaskStatus

    failed_task = TaskNode(
        id="t0",
        goal_id="g1",
        title="step 0",
        description="x",
        status=TaskStatus.FAILED,
        depends_on=(),
        estimated_cost_tokens=200,
    )
    p.replan(original, failed_task, error="API timeout")

    # second prompt is the replan
    replan_prompt = captured[-1]
    assert "API timeout" in replan_prompt
    assert "REPLAN" in replan_prompt


# ───────────────────────── construction ─────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_tasks_per_plan": 0},
        {"retry_on_invalid_json": -1},
        {"min_task_tokens": -1},
        {"max_task_tokens": 50, "min_task_tokens": 100},
    ],
)
def test_invalid_constructor_raises(kwargs):
    with pytest.raises(ValueError):
        Planner(_make_llm("x"), **kwargs)
