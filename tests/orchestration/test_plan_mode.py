"""Unit tests for ``orchestration.plan_mode``."""

from __future__ import annotations

import pytest

from orchestration.plan_mode import (
    PlanMode,
    PlanReviewResult,
    ReviewVerdict,
)
from orchestration.planner import PlannedTask, PlannerOutput
from orchestration.task_graph import Goal, TaskStatus


def _make_output(*, risky_title: bool = False, risky_tokens: int = 200) -> PlannerOutput:
    title = "drop table users" if risky_title else "write tests"
    return PlannerOutput(
        tasks=(
            PlannedTask(
                local_id="t0",
                title=title,
                description="x",
                depends_on=(),
                estimated_cost_tokens=risky_tokens,
            ),
        ),
        rationale="r",
        estimated_total_tokens=risky_tokens,
    )


def _make_goal(gid: str = "g1") -> Goal:
    return Goal(id=gid, title="ship feature", status=TaskStatus.PENDING, created_ms=1)


# ───────────────────────── auto-approve ─────────────────────────


def test_auto_approve_passes_safe_plan():
    pm = PlanMode(auto_approve=True)
    res = pm.review("g1", _make_output())
    assert res.verdict is ReviewVerdict.APPROVED
    assert res.auto_approved is True


def test_auto_approve_off_yields_pending():
    pm = PlanMode(auto_approve=False)
    res = pm.review("g1", _make_output())
    assert res.verdict is ReviewVerdict.PENDING
    assert res.auto_approved is False


def test_auto_approve_blocked_by_risky_title():
    pm = PlanMode(auto_approve=True, require_approval_for_risky=True)
    res = pm.review("g1", _make_output(risky_title=True))
    assert res.verdict is ReviewVerdict.PENDING


def test_auto_approve_blocked_by_high_token_estimate():
    pm = PlanMode(
        auto_approve=True,
        require_approval_for_risky=True,
        risky_token_threshold=1000,
    )
    res = pm.review("g1", _make_output(risky_tokens=2000))
    assert res.verdict is ReviewVerdict.PENDING


def test_risky_can_be_disabled():
    pm = PlanMode(auto_approve=True, require_approval_for_risky=False)
    res = pm.review("g1", _make_output(risky_title=True))
    assert res.verdict is ReviewVerdict.APPROVED


# ───────────────────────── manual settle ─────────────────────────


def test_approve_settles_pending():
    pm = PlanMode(auto_approve=False)
    pm.review("g1", _make_output())
    assert not pm.is_settled("g1")
    res = pm.approve("g1", feedback="lgtm")
    assert res.verdict is ReviewVerdict.APPROVED
    assert res.feedback == "lgtm"
    assert pm.is_settled("g1")


def test_reject_settles_pending():
    pm = PlanMode(auto_approve=False)
    pm.review("g1", _make_output())
    res = pm.reject("g1", feedback="too risky")
    assert res.verdict is ReviewVerdict.REJECTED
    assert res.feedback == "too risky"


def test_request_changes_settles_pending():
    pm = PlanMode(auto_approve=False)
    pm.review("g1", _make_output())
    res = pm.request_changes("g1", feedback="add more tests")
    assert res.verdict is ReviewVerdict.NEEDS_CHANGES


def test_get_returns_current_state():
    pm = PlanMode(auto_approve=False)
    assert pm.get("g1") is None
    pm.review("g1", _make_output())
    state = pm.get("g1")
    assert state is not None
    assert state.verdict is ReviewVerdict.PENDING


# ───────────────────────── rendering ─────────────────────────


def test_render_for_human_includes_key_fields():
    pm = PlanMode()
    output = PlannerOutput(
        tasks=(
            PlannedTask(
                local_id="t0",
                title="design API",
                description="REST surface",
                depends_on=(),
                estimated_cost_tokens=300,
            ),
            PlannedTask(
                local_id="t1",
                title="implement",
                description="",
                depends_on=("t0",),
                estimated_cost_tokens=800,
            ),
        ),
        rationale="iterative",
        estimated_total_tokens=1100,
    )
    md = pm.render_for_human(_make_goal(), output)
    assert "ship feature" in md
    assert "design API" in md
    assert "implement" in md
    assert "iterative" in md
    assert "1100" in md
    assert "approve" in md.lower()


# ───────────────────────── construction ─────────────────────────


def test_invalid_threshold_raises():
    with pytest.raises(ValueError):
        PlanMode(risky_token_threshold=-1)


def test_custom_risky_patterns():
    pm = PlanMode(risky_patterns=(r"\bbanana\b",))
    safe = pm.review("g1", _make_output())  # default safe
    assert safe.verdict is ReviewVerdict.APPROVED
    risky_output = PlannerOutput(
        tasks=(
            PlannedTask(
                local_id="t0",
                title="eat banana",
                description="",
                depends_on=(),
                estimated_cost_tokens=200,
            ),
        ),
        rationale="r",
        estimated_total_tokens=200,
    )
    risky_review = pm.review("g2", risky_output)
    assert risky_review.verdict is ReviewVerdict.PENDING


def test_review_result_is_frozen():
    res = PlanReviewResult(
        verdict=ReviewVerdict.APPROVED, feedback=None, auto_approved=True
    )
    with pytest.raises((AttributeError, Exception)):
        res.verdict = ReviewVerdict.REJECTED  # type: ignore[misc]
