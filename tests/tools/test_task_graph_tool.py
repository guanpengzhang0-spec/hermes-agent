"""Smoke tests for ``tools.task_graph_tool``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.orchestration_bridge import OrchestrationBridge
from tools.task_graph_tool import TOOL_SCHEMA, task_graph_tool


@pytest.fixture
def agent_with_bridge(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bridge = OrchestrationBridge(
        session_id="s1",
        agent_cfg={
            "orchestration": {"task_graph": {"enabled": True}},
            "observability": {"event_stream": {"enabled": True}},
        },
    )
    agent = SimpleNamespace(_orch_bridge=bridge)
    yield agent
    bridge.close()


def test_disabled_returns_marker():
    """No bridge → disabled marker."""
    agent = SimpleNamespace(_orch_bridge=None)
    out = json.loads(task_graph_tool(agent=agent, action="active_goals"))
    assert "error" in out
    assert "disabled" in out["error"]


def test_unknown_action(agent_with_bridge):
    out = json.loads(
        task_graph_tool(agent=agent_with_bridge, action="bogus")
    )
    assert "error" in out
    assert "unknown action" in out["error"]


def test_create_goal_returns_id(agent_with_bridge):
    out = json.loads(
        task_graph_tool(agent=agent_with_bridge, action="create_goal", title="g1")
    )
    assert "goal_id" in out
    assert out["status"] == "pending"


def test_full_lifecycle(agent_with_bridge):
    g = json.loads(
        task_graph_tool(agent=agent_with_bridge, action="create_goal", title="g")
    )
    a = json.loads(
        task_graph_tool(
            agent=agent_with_bridge,
            action="add_task",
            goal_id=g["goal_id"],
            title="A",
        )
    )
    assert "task_id" in a

    # Mark started + completed
    task_graph_tool(
        agent=agent_with_bridge,
        action="mark_started",
        task_id=a["task_id"],
    )
    task_graph_tool(
        agent=agent_with_bridge,
        action="mark_completed",
        task_id=a["task_id"],
        result="ok",
        tokens=42,
    )
    progress = json.loads(
        task_graph_tool(
            agent=agent_with_bridge,
            action="progress",
            goal_id=g["goal_id"],
        )
    )
    assert progress["completed"] == 1
    assert progress["actual_cost_tokens"] == 42


def test_add_task_with_unknown_dep_returns_error(agent_with_bridge):
    g = json.loads(
        task_graph_tool(agent=agent_with_bridge, action="create_goal", title="g")
    )
    out = json.loads(
        task_graph_tool(
            agent=agent_with_bridge,
            action="add_task",
            goal_id=g["goal_id"],
            title="x",
            depends_on=["nope"],
        )
    )
    assert "error" in out


def test_active_goals_listing(agent_with_bridge):
    json.loads(
        task_graph_tool(agent=agent_with_bridge, action="create_goal", title="g1")
    )
    json.loads(
        task_graph_tool(agent=agent_with_bridge, action="create_goal", title="g2")
    )
    out = json.loads(
        task_graph_tool(agent=agent_with_bridge, action="active_goals")
    )
    assert len(out["goals"]) == 2


def test_attention_format_compatible(agent_with_bridge):
    g = json.loads(
        task_graph_tool(agent=agent_with_bridge, action="create_goal", title="g")
    )
    json.loads(
        task_graph_tool(
            agent=agent_with_bridge,
            action="add_task",
            goal_id=g["goal_id"],
            title="A",
            estimated_cost_tokens=200,
        )
    )
    att = json.loads(
        task_graph_tool(
            agent=agent_with_bridge,
            action="attention",
            goal_id=g["goal_id"],
        )
    )
    assert "items" in att
    # items should be consumable by orchestration.attention.format_attention_block
    from orchestration.attention import format_attention_block

    block = format_attention_block(att["items"], include_metrics=True)
    assert "A" in block


def test_required_arg_validation(agent_with_bridge):
    out = json.loads(
        task_graph_tool(agent=agent_with_bridge, action="create_goal")
    )
    assert "error" in out


def test_schema_has_correct_function_name():
    assert TOOL_SCHEMA["type"] == "function"
    assert TOOL_SCHEMA["function"]["name"] == "task_graph"
    assert "action" in TOOL_SCHEMA["function"]["parameters"]["required"]
