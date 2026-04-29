"""Smoke tests for ``tools.orchestration_tool``.

The tool wires Bridge → TaskGraph → Planner → PlanMode → Orchestrator
together. Tests run the full lifecycle with a stub LLM/executor.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.orchestration_bridge import OrchestrationBridge
from tools.orchestration_tool import TOOL_SCHEMA, orchestration_tool


def _good_plan_json(n: int = 1) -> str:
    """Same shape Planner expects."""
    tasks = []
    prev = None
    for i in range(n):
        tasks.append(
            {
                "id": f"t{i}",
                "title": f"step {i}",
                "description": "",
                "depends_on": [prev] if prev else [],
                "estimated_cost_tokens": 200,
            }
        )
        prev = f"t{i}"
    return json.dumps({"tasks": tasks, "rationale": "ok"})


@pytest.fixture
def agent_with_orch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {
        "orchestration": {
            "task_graph": {"enabled": True},
            "orchestrator": {"enabled": True, "max_replans": 2},
            "planner": {"enabled": True},
            "plan_mode": {"auto_approve": True},
        },
        "observability": {"event_stream": {"enabled": True}},
    }
    bridge = OrchestrationBridge(session_id="s1", agent_cfg=cfg)
    # Inject a deterministic LLM that always returns a valid plan
    bridge.set_planner_llm(lambda _prompt: _good_plan_json(1))
    agent = SimpleNamespace(_orch_bridge=bridge, client=None, model=None)
    yield agent
    bridge.close()


def test_disabled_returns_marker():
    agent = SimpleNamespace(_orch_bridge=None)
    out = json.loads(orchestration_tool(agent=agent, action="submit_goal", title="x"))
    assert "error" in out
    assert "disabled" in out["error"]


def test_submit_goal_returns_id(agent_with_orch):
    out = json.loads(
        orchestration_tool(agent=agent_with_orch, action="submit_goal", title="g")
    )
    assert "goal_id" in out


def test_state_query(agent_with_orch):
    g = json.loads(
        orchestration_tool(agent=agent_with_orch, action="submit_goal", title="g")
    )
    s = json.loads(
        orchestration_tool(agent=agent_with_orch, action="state", goal_id=g["goal_id"])
    )
    assert s["phase"] == "idle"
    assert s["goal_id"] == g["goal_id"]


def test_unknown_goal_yields_error(agent_with_orch):
    out = json.loads(
        orchestration_tool(agent=agent_with_orch, action="state", goal_id="bogus")
    )
    assert "error" in out


def test_full_run_to_completion_with_stub_executor(agent_with_orch):
    """The default executor falls back to a stub when client is None,
    so the goal should still terminate."""
    g = json.loads(
        orchestration_tool(agent=agent_with_orch, action="submit_goal", title="g")
    )
    final = json.loads(
        orchestration_tool(
            agent=agent_with_orch,
            action="run_to_completion",
            goal_id=g["goal_id"],
            max_ticks=20,
        )
    )
    # Either complete or failed (both are terminal); should not be in-flight
    assert final["phase"] in ("complete", "failed")


def test_cancel_marks_failed(agent_with_orch):
    g = json.loads(
        orchestration_tool(agent=agent_with_orch, action="submit_goal", title="g")
    )
    out = json.loads(
        orchestration_tool(agent=agent_with_orch, action="cancel", goal_id=g["goal_id"])
    )
    assert out["phase"] == "failed"


def test_unknown_action(agent_with_orch):
    out = json.loads(orchestration_tool(agent=agent_with_orch, action="bogus"))
    assert "error" in out
    assert "unknown action" in out["error"]


def test_schema_function_name():
    assert TOOL_SCHEMA["function"]["name"] == "orchestration"
