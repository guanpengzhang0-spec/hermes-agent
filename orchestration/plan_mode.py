"""Plan-review mode controller.

Sits between ``Planner`` and ``Orchestrator`` execution. Two operating
modes:

  * **auto-approve** (default) — plans pass through untouched
  * **manual** — plan rendered to markdown for a human reviewer who
    must call ``approve(...)``, ``reject(...)``, or ``request_changes(...)``

Optionally enforces "risky tasks must be approved" — tasks whose
estimated cost exceeds a threshold or whose title matches a configurable
deny-list pattern force a manual review even when ``auto_approve=True``.

Stateful: holds the most recent pending review per goal so async
control flow can wait on it. State is in-memory only; if the runtime
restarts mid-review, the goal must be re-submitted.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .planner import PlannerOutput
from .task_graph import Goal


class ReviewVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CHANGES = "needs_changes"
    PENDING = "pending"


@dataclass(frozen=True)
class PlanReviewResult:
    verdict: ReviewVerdict
    feedback: Optional[str]
    auto_approved: bool


class PlanMode:
    """Synchronous plan reviewer.

    The Orchestrator calls ``review(goal_id, output)`` to either get an
    immediate verdict (auto path) or register a pending review (manual
    path). For manual reviews, an external surface (CLI / web) calls
    ``approve / reject / request_changes`` to settle it.
    """

    DEFAULT_RISKY_PATTERNS: tuple[str, ...] = (
        r"\bdrop\s+(table|database)\b",
        r"\brm\s+-rf\b",
        r"\bdelete\s+all\b",
        r"\btransfer\s+(funds?|money|credits?)\b",
        r"\bsudo\b",
    )

    def __init__(
        self,
        *,
        auto_approve: bool = True,
        require_approval_for_risky: bool = True,
        risky_token_threshold: int = 5_000,
        risky_patterns: Optional[tuple[str, ...]] = None,
    ) -> None:
        if risky_token_threshold < 0:
            raise ValueError("risky_token_threshold must be >= 0")
        self._auto_approve = auto_approve
        self._require_approval_for_risky = require_approval_for_risky
        self._risky_token_threshold = risky_token_threshold
        patterns = (
            risky_patterns
            if risky_patterns is not None
            else self.DEFAULT_RISKY_PATTERNS
        )
        self._risky_re = [
            re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns
        ]

        self._lock = threading.RLock()
        # goal_id → pending review result (None when settled)
        self._pending: dict[str, PlanReviewResult] = {}

    # ─────────────────────────── public API ───────────────────────────

    def review(self, goal_id: str, output: PlannerOutput) -> PlanReviewResult:
        """Decide whether the plan can proceed.

        Returns immediately with APPROVED when auto-approve is on AND
        no risky tasks are present. Otherwise returns PENDING and records
        the goal so an external reviewer can settle it.
        """
        risky = self._is_risky(output)
        if self._auto_approve and not (
            risky and self._require_approval_for_risky
        ):
            result = PlanReviewResult(
                verdict=ReviewVerdict.APPROVED,
                feedback=None,
                auto_approved=True,
            )
            with self._lock:
                self._pending[goal_id] = result
            return result

        # Manual path
        result = PlanReviewResult(
            verdict=ReviewVerdict.PENDING,
            feedback=(
                "auto-approve disabled by config — awaiting reviewer"
                if not self._auto_approve
                else "risky task detected — awaiting reviewer"
            ),
            auto_approved=False,
        )
        with self._lock:
            self._pending[goal_id] = result
        return result

    def approve(self, goal_id: str, *, feedback: str = "") -> PlanReviewResult:
        return self._settle(
            goal_id,
            PlanReviewResult(
                verdict=ReviewVerdict.APPROVED,
                feedback=feedback or None,
                auto_approved=False,
            ),
        )

    def reject(self, goal_id: str, *, feedback: str = "") -> PlanReviewResult:
        return self._settle(
            goal_id,
            PlanReviewResult(
                verdict=ReviewVerdict.REJECTED,
                feedback=feedback or None,
                auto_approved=False,
            ),
        )

    def request_changes(
        self, goal_id: str, *, feedback: str = ""
    ) -> PlanReviewResult:
        return self._settle(
            goal_id,
            PlanReviewResult(
                verdict=ReviewVerdict.NEEDS_CHANGES,
                feedback=feedback or None,
                auto_approved=False,
            ),
        )

    def get(self, goal_id: str) -> Optional[PlanReviewResult]:
        with self._lock:
            return self._pending.get(goal_id)

    def is_settled(self, goal_id: str) -> bool:
        result = self.get(goal_id)
        return result is not None and result.verdict is not ReviewVerdict.PENDING

    def render_for_human(self, goal: Goal, output: PlannerOutput) -> str:
        """Markdown view for the CLI / web reviewer surface."""
        lines: list[str] = [
            f"# Plan review: {goal.title}",
            "",
            f"**Goal id**: `{goal.id}`",
            f"**Plan rationale**: {output.rationale}",
            f"**Estimated total**: ~{output.estimated_total_tokens} tokens",
            "",
            "## Tasks",
            "",
        ]
        for i, t in enumerate(output.tasks, 1):
            deps = ", ".join(t.depends_on) if t.depends_on else "_(none)_"
            lines.append(
                f"{i}. **{t.title}** (`{t.local_id}`, ~{t.estimated_cost_tokens} tok)"
            )
            if t.description:
                lines.append(f"   - {t.description}")
            lines.append(f"   - depends on: {deps}")
        lines.extend(
            [
                "",
                "## Verdict",
                "",
                "Reply with one of:",
                "  - `approve` — proceed to execution",
                "  - `reject` — abandon this goal",
                "  - `revise <feedback>` — request a re-plan with notes",
            ]
        )
        return "\n".join(lines)

    # ─────────────────────────── internals ───────────────────────────

    def _settle(
        self, goal_id: str, result: PlanReviewResult
    ) -> PlanReviewResult:
        with self._lock:
            self._pending[goal_id] = result
        return result

    def _is_risky(self, output: PlannerOutput) -> bool:
        if any(
            t.estimated_cost_tokens >= self._risky_token_threshold
            for t in output.tasks
        ):
            return True
        for t in output.tasks:
            text = f"{t.title}\n{t.description}"
            for pat in self._risky_re:
                if pat.search(text):
                    return True
        return False
