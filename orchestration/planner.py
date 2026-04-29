"""LLM-driven goal decomposition.

Given a high-level goal, the planner asks the LLM to break it into a
DAG of concrete tasks. The output is strict JSON, validated for:
  * each task id is unique within the plan
  * every ``depends_on`` entry references a task in the same plan
  * total task count ≤ ``max_tasks_per_plan``
  * estimated cost is within sane bounds

If the LLM returns invalid JSON, the planner retries up to
``retry_on_invalid_json`` times with a corrective hint appended.

Replan path: when execution of an existing plan fails, ``replan(...)``
is called. Already-completed tasks are preserved verbatim; the planner
is asked to redesign just the failed task and any not-yet-started tasks.

The planner does NOT touch ``TaskGraph`` directly — it returns a
``PlannerOutput`` that the caller (Orchestrator) inserts into the graph.
This keeps the planner side-effect-free and easy to unit-test.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .task_graph import TaskNode, TaskStatus

logger = logging.getLogger(__name__)


# ─────────────────────────── public types ───────────────────────────


@dataclass(frozen=True)
class PlannerInput:
    """Single source of truth for what the planner sees."""

    goal_title: str
    goal_description: str = ""
    available_tools: tuple[str, ...] = ()
    constraints: dict[str, str] = field(default_factory=dict)
    prior_failure: Optional[str] = None  # populated only on replan


@dataclass(frozen=True)
class PlannedTask:
    """Lightweight pre-DAG task. Caller converts to TaskNode."""

    local_id: str               # planner-assigned id, unique within plan
    title: str
    description: str
    depends_on: tuple[str, ...]
    estimated_cost_tokens: int


@dataclass(frozen=True)
class PlannerOutput:
    """What ``plan(...)`` and ``replan(...)`` return."""

    tasks: tuple[PlannedTask, ...]
    rationale: str
    estimated_total_tokens: int


# Caller-injected: takes a prompt, returns the LLM's raw response text.
LLMCaller = Callable[[str], str]


# ─────────────────────────── prompt scaffolds ───────────────────────────


_SYSTEM_INSTRUCTION = """\
You are a planning subsystem. Given a goal, output a JSON object with
this exact schema:

{{
  "tasks": [
    {{
      "id": "string-unique-within-plan",
      "title": "short imperative phrase",
      "description": "1-3 sentences",
      "depends_on": ["other-task-id", ...],
      "estimated_cost_tokens": 100
    }}
  ],
  "rationale": "1 paragraph explaining the decomposition"
}}

Rules:
  * Output ONLY the JSON object. No prose before or after.
  * Each `id` must be unique within `tasks`.
  * Every entry in `depends_on` must reference an `id` that appears
    in `tasks` (no forward references — list deps after their providers).
  * The graph must have NO cycles.
  * Keep `estimated_cost_tokens` between 100 and 5000 per task.
  * Aim for 2 to {max_tasks} tasks.
"""


_USER_TEMPLATE = """\
Goal title: {goal_title}

Goal description:
{goal_description}

Available tools:
{tools}

Constraints:
{constraints}
{prior_failure_block}
"""


_RETRY_HINT = (
    "\n\nYour previous response was not valid JSON matching the schema. "
    "Specifically: {error}. Output ONLY the JSON object."
)


# ─────────────────────────── Planner ───────────────────────────


class Planner:
    """JSON-strict LLM planner with retry + DAG validation.

    Stateless aside from constructor args.
    """

    def __init__(
        self,
        llm: LLMCaller,
        *,
        max_tasks_per_plan: int = 10,
        retry_on_invalid_json: int = 2,
        min_task_tokens: int = 100,
        max_task_tokens: int = 5000,
    ) -> None:
        if max_tasks_per_plan < 1:
            raise ValueError("max_tasks_per_plan must be >= 1")
        if retry_on_invalid_json < 0:
            raise ValueError("retry_on_invalid_json must be >= 0")
        if min_task_tokens < 0 or max_task_tokens < min_task_tokens:
            raise ValueError("invalid token bounds")

        self._llm = llm
        self._max_tasks = max_tasks_per_plan
        self._retries = retry_on_invalid_json
        self._min_tokens = min_task_tokens
        self._max_tokens = max_task_tokens

    # ─────────────────────────── public API ───────────────────────────

    def plan(self, inp: PlannerInput) -> PlannerOutput:
        prompt = self._build_prompt(inp)
        return self._call_with_retries(prompt)

    def replan(
        self,
        original: PlannerOutput,
        failed_task: TaskNode,
        error: str,
    ) -> PlannerOutput:
        """Re-plan, preserving completed tasks from ``original``.

        Tasks already at COMPLETED status are pinned; the LLM is told
        about them and asked to redesign just the failed task and its
        dependents.
        """
        completed_titles: list[str] = []
        for t in original.tasks:
            if (
                t.local_id == failed_task.id
                or t.local_id in failed_task.depends_on
            ):
                continue
            completed_titles.append(f"  - {t.title}")

        completed_note = (
            "\n\nAlready completed (do not redo):\n" + "\n".join(completed_titles)
            if completed_titles
            else ""
        )
        new_input = PlannerInput(
            goal_title=f"REPLAN: {failed_task.title}",
            goal_description=(
                f"The previous attempt at this task failed with:\n{error}\n"
                f"Design an alternative approach.{completed_note}"
            ),
            available_tools=(),
            constraints={"max_tasks": str(self._max_tasks)},
            prior_failure=error,
        )
        return self._call_with_retries(self._build_prompt(new_input))

    def to_task_nodes(
        self, output: PlannerOutput, goal_id: str
    ) -> list[dict[str, Any]]:
        """Convert PlannerOutput → kwargs lists ready for
        ``TaskGraph.add_task(goal_id, **kwargs)``.

        ``goal_id`` is accepted purely as a caller-side reminder of which
        goal these kwargs target — the planner itself stays goal-agnostic
        so the same ``PlannerOutput`` could be replayed elsewhere.

        The Orchestrator uses this to materialise the plan.
        """
        del goal_id  # signal: intentionally unused
        return [
            {
                "title": t.title,
                "description": t.description,
                "depends_on": list(t.depends_on),
                "estimated_cost_tokens": t.estimated_cost_tokens,
            }
            for t in output.tasks
        ]

    # ─────────────────────────── internals ───────────────────────────

    def _build_prompt(self, inp: PlannerInput) -> str:
        tools_block = (
            "\n".join(f"  - {t}" for t in inp.available_tools)
            if inp.available_tools
            else "  (no tools listed — assume general capability)"
        )
        constraints_block = (
            "\n".join(f"  - {k}: {v}" for k, v in inp.constraints.items())
            if inp.constraints
            else "  (none)"
        )
        prior_failure_block = (
            f"\nPrior failure context:\n{inp.prior_failure}\n"
            if inp.prior_failure
            else ""
        )
        return (
            _SYSTEM_INSTRUCTION.format(max_tasks=self._max_tasks)
            + "\n\n---\n\n"
            + _USER_TEMPLATE.format(
                goal_title=inp.goal_title,
                goal_description=inp.goal_description or "(none)",
                tools=tools_block,
                constraints=constraints_block,
                prior_failure_block=prior_failure_block,
            )
        )

    def _call_with_retries(self, prompt: str) -> PlannerOutput:
        last_error: Optional[str] = None
        attempt_prompt = prompt
        for attempt in range(self._retries + 1):
            raw = self._llm(attempt_prompt)
            try:
                return self._parse_and_validate(raw)
            except _PlannerValidationError as exc:
                last_error = str(exc)
                logger.info(
                    "planner: attempt %d failed validation: %s",
                    attempt + 1,
                    last_error,
                )
                attempt_prompt = prompt + _RETRY_HINT.format(error=last_error)
        raise RuntimeError(
            f"Planner failed after {self._retries + 1} attempts: {last_error}"
        )

    def _parse_and_validate(self, raw: str) -> PlannerOutput:
        # Trim possible markdown code fences
        text = raw.strip()
        # Drop ```json ... ``` or ``` ... ``` fencing
        fence_match = re.match(r"^```(?:json)?\s*(.*?)```$", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _PlannerValidationError(f"invalid JSON: {exc.msg}")

        if not isinstance(parsed, dict):
            raise _PlannerValidationError("top level must be an object")
        if "tasks" not in parsed or not isinstance(parsed["tasks"], list):
            raise _PlannerValidationError("missing or non-list 'tasks'")

        tasks_raw = parsed["tasks"]
        rationale = str(parsed.get("rationale", "")).strip()

        if len(tasks_raw) < 1:
            raise _PlannerValidationError("plan must have at least 1 task")
        if len(tasks_raw) > self._max_tasks:
            raise _PlannerValidationError(
                f"plan has {len(tasks_raw)} tasks, max is {self._max_tasks}"
            )

        seen_ids: set[str] = set()
        planned: list[PlannedTask] = []
        for i, raw_task in enumerate(tasks_raw):
            if not isinstance(raw_task, dict):
                raise _PlannerValidationError(f"task #{i} must be an object")
            tid = str(raw_task.get("id", "")).strip()
            if not tid:
                raise _PlannerValidationError(f"task #{i} missing 'id'")
            if tid in seen_ids:
                raise _PlannerValidationError(f"duplicate task id: {tid!r}")
            seen_ids.add(tid)

            title = str(raw_task.get("title", "")).strip()
            if not title:
                raise _PlannerValidationError(
                    f"task {tid!r} missing non-empty 'title'"
                )

            description = str(raw_task.get("description", "")).strip()

            deps_raw = raw_task.get("depends_on", []) or []
            if not isinstance(deps_raw, list):
                raise _PlannerValidationError(
                    f"task {tid!r} depends_on must be a list"
                )
            deps = tuple(str(d).strip() for d in deps_raw if str(d).strip())
            for d in deps:
                if d not in seen_ids:
                    raise _PlannerValidationError(
                        f"task {tid!r} depends on unknown id {d!r} "
                        "(forward refs not allowed; list dependencies first)"
                    )

            try:
                est = int(raw_task.get("estimated_cost_tokens", 0))
            except (TypeError, ValueError):
                raise _PlannerValidationError(
                    f"task {tid!r} estimated_cost_tokens not an int"
                )
            if not (self._min_tokens <= est <= self._max_tokens):
                raise _PlannerValidationError(
                    f"task {tid!r} estimated_cost_tokens={est} "
                    f"outside [{self._min_tokens}, {self._max_tokens}]"
                )

            planned.append(
                PlannedTask(
                    local_id=tid,
                    title=title,
                    description=description,
                    depends_on=deps,
                    estimated_cost_tokens=est,
                )
            )

        # Cycle check (forward-ref ban already prevents this, but belt+braces)
        if _has_cycle(planned):
            raise _PlannerValidationError("plan contains a dependency cycle")

        total_tokens = sum(t.estimated_cost_tokens for t in planned)
        return PlannerOutput(
            tasks=tuple(planned),
            rationale=rationale,
            estimated_total_tokens=total_tokens,
        )


# ─────────────────────────── helpers ───────────────────────────


class _PlannerValidationError(Exception):
    """Internal — converted to retry / RuntimeError at the call boundary."""


def _has_cycle(tasks: list[PlannedTask]) -> bool:
    """DFS three-color cycle detection on the planned DAG."""
    graph = {t.local_id: list(t.depends_on) for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in graph}

    def visit(node: str) -> bool:
        if color[node] == GRAY:
            return True
        if color[node] == BLACK:
            return False
        color[node] = GRAY
        for dep in graph.get(node, []):
            if dep in color and visit(dep):
                return True
        color[node] = BLACK
        return False

    return any(visit(n) for n in graph)


# Keep TaskStatus referenced so users importing from this module see it
# alongside (avoids the "unused import" warning on the upstream type).
__all__ = [
    "LLMCaller",
    "PlannedTask",
    "Planner",
    "PlannerInput",
    "PlannerOutput",
    "TaskStatus",
]
