"""Smoke tests for ``tools.audit_log_tool``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.orchestration_bridge import OrchestrationBridge
from tools.audit_log_tool import TOOL_SCHEMA, audit_log_tool


@pytest.fixture
def agent_with_audit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bridge = OrchestrationBridge(
        session_id="s1",
        agent_cfg={
            "observability": {"event_stream": {"enabled": True}},
            "self_evolution": {"audit_log": {"enabled": True, "enable_git_tag": False}},
        },
    )
    agent = SimpleNamespace(_orch_bridge=bridge)
    yield agent
    bridge.close()


def test_disabled_returns_marker():
    agent = SimpleNamespace(_orch_bridge=None)
    out = json.loads(audit_log_tool(agent=agent, action="recent"))
    assert "error" in out
    assert "disabled" in out["error"]


def test_record_and_recent(agent_with_audit, tmp_path: Path):
    target = tmp_path / "skill.md"
    target.write_text("v1")
    rec = json.loads(
        audit_log_tool(
            agent=agent_with_audit,
            action="record",
            kind="skill_modified",
            actor="skill:test",
            target_path=str(target),
            new_text="v1",
            old_text="",
            rationale="initial",
        )
    )
    assert "audit_id" in rec
    assert rec["target_path"] == str(target)

    recent = json.loads(
        audit_log_tool(agent=agent_with_audit, action="recent", limit=5)
    )
    assert len(recent["entries"]) == 1
    assert recent["entries"][0]["actor"] == "skill:test"


def test_record_missing_required_fields(agent_with_audit):
    out = json.loads(
        audit_log_tool(agent=agent_with_audit, action="record", kind="skill_modified")
    )
    assert "error" in out


def test_unknown_kind_rejected(agent_with_audit):
    out = json.loads(
        audit_log_tool(
            agent=agent_with_audit,
            action="record",
            kind="bogus_kind",
            target_path="/tmp/x",
            rationale="r",
            new_text="x",
        )
    )
    assert "error" in out


def test_verify_returns_three_buckets(agent_with_audit, tmp_path: Path):
    target = tmp_path / "f.txt"
    target.write_text("hello")
    audit_log_tool(
        agent=agent_with_audit,
        action="record",
        kind="other",
        actor="t",
        target_path=str(target),
        new_text="hello",
        rationale="r",
    )
    out = json.loads(audit_log_tool(agent=agent_with_audit, action="verify"))
    assert "matches" in out
    assert "mismatches" in out
    assert "missing" in out
    assert str(target) in out["matches"]


def test_recent_kind_filter(agent_with_audit):
    audit_log_tool(
        agent=agent_with_audit,
        action="record",
        kind="skill_modified",
        actor="x",
        target_path="/tmp/skill",
        new_text="x",
        rationale="r",
    )
    audit_log_tool(
        agent=agent_with_audit,
        action="record",
        kind="config_changed",
        actor="x",
        target_path="/tmp/cfg",
        new_text="x",
        rationale="r",
    )
    only_skills = json.loads(
        audit_log_tool(
            agent=agent_with_audit,
            action="recent",
            kind="skill_modified",
            limit=10,
        )
    )
    assert len(only_skills["entries"]) == 1


def test_schema_function_name():
    assert TOOL_SCHEMA["function"]["name"] == "audit_log"
