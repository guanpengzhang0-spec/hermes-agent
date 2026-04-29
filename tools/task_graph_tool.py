"""LLM-callable tool surface for ``orchestration.task_graph.TaskGraph``.

Single tool entry point with an ``action`` parameter so the OpenAI
function schema stays compact:

    task_graph(action="create_goal", title="ship feature X")
    task_graph(action="add_task", goal_id="...", title="design API",
               depends_on=["..."], estimated_cost_tokens=300)
    task_graph(action="ready", goal_id="...")
    task_graph(action="mark_started", task_id="...")
    task_graph(action="mark_completed", task_id="...", result="...", tokens=42)
    task_graph(action="mark_failed", task_id="...", error="...")
    task_graph(action="progress", goal_id="...")
    task_graph(action="active_goals")
    task_graph(action="cancel_goal", goal_id="...")

Returns JSON-serializable dicts. Errors come back as
``{"error": "<message>"}`` so the LLM can react instead of crashing
the turn.

The tool grabs the ``TaskGraph`` instance from the agent's
``OrchestrationBridge``. When the bridge is missing or TaskGraph is
disabled, every action returns the same disabled marker — the LLM
learns to stop calling it.
"""

from __future__ import annotations

import json
from typing import Any, Optional


_DISABLED_RESPONSE = {
    "error": "task_graph is disabled. Set "
    "orchestration.task_graph.enabled=true in ~/.hermes/config.yaml."
}


# ─────────────────────────── tool dispatch ───────────────────────────


def task_graph_tool(*, agent: Any = None, action: str = "", **kwargs: Any) -> str:
    """Dispatch a TaskGraph action. Always returns a JSON string.

    Args:
      agent: AIAgent instance. Used to reach the OrchestrationBridge.
      action: one of {create_goal, add_task, ready, mark_started,
        mark_completed, mark_failed, progress, active_goals, cancel_goal,
        get_goal, get_task, attention}.
      **kwargs: action-specific args.
    """
    bridge = getattr(agent, "_orch_bridge", None) if agent is not None else None
    tg = getattr(bridge, "task_graph", None) if bridge is not None else None
    if tg is None:
        return json.dumps(_DISABLED_RESPONSE)

    action = (action or "").strip().lower()
    handler = _DISPATCH.get(action)
    if handler is None:
        return _err(
            f"unknown action {action!r}. Valid: {sorted(_DISPATCH.keys())}"
        )
    try:
        return handler(tg, kwargs)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"internal error: {exc}")


# ─────────────────────────── per-action handlers ───────────────────────────


def _create_goal(tg: Any, args: dict) -> str:
    title = _required(args, "title", str)
    goal = tg.create_goal(title)
    return json.dumps({"goal_id": goal.id, "status": goal.status.value})


def _add_task(tg: Any, args: dict) -> str:
    goal_id = _required(args, "goal_id", str)
    title = _required(args, "title", str)
    description = str(args.get("description", ""))
    depends_on = list(args.get("depends_on", []) or [])
    est = int(args.get("estimated_cost_tokens", 0) or 0)
    max_retries = int(args.get("max_retries", 2))
    task_id = tg.add_task(
        goal_id,
        title=title,
        description=description,
        depends_on=depends_on,
        estimated_cost_tokens=est,
        max_retries=max_retries,
    )
    return json.dumps({"task_id": task_id})


def _ready(tg: Any, args: dict) -> str:
    goal_id = _required(args, "goal_id", str)
    ready = tg.get_ready_tasks(goal_id)
    return json.dumps(
        {
            "ready": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "depends_on": list(t.depends_on),
                    "retries": t.retries,
                    "estimated_cost_tokens": t.estimated_cost_tokens,
                }
                for t in ready
            ]
        }
    )


def _mark_started(tg: Any, args: dict) -> str:
    task_id = _required(args, "task_id", str)
    tg.mark_started(task_id)
    return json.dumps({"task_id": task_id, "status": "running"})


def _mark_completed(tg: Any, args: dict) -> str:
    task_id = _required(args, "task_id", str)
    result = str(args.get("result", ""))
    tokens = int(args.get("tokens", 0) or 0)
    tg.mark_completed(task_id, result=result, tokens=tokens)
    return json.dumps({"task_id": task_id, "status": "completed"})


def _mark_failed(tg: Any, args: dict) -> str:
    task_id = _required(args, "task_id", str)
    error = str(args.get("error", "unspecified"))
    retriable = tg.mark_failed(task_id, error=error)
    return json.dumps({"task_id": task_id, "retriable": bool(retriable)})


def _cancel_goal(tg: Any, args: dict) -> str:
    goal_id = _required(args, "goal_id", str)
    tg.cancel_goal(goal_id)
    return json.dumps({"goal_id": goal_id, "status": "cancelled"})


def _progress(tg: Any, args: dict) -> str:
    goal_id = _required(args, "goal_id", str)
    return json.dumps(tg.goal_progress(goal_id))


def _active_goals(tg: Any, args: dict) -> str:
    del args
    goals = tg.list_active_goals()
    return json.dumps(
        {
            "goals": [
                {
                    "id": g.id,
                    "title": g.title,
                    "status": g.status.value,
                    "task_count": len(g.tasks),
                }
                for g in goals
            ]
        }
    )


def _get_goal(tg: Any, args: dict) -> str:
    goal_id = _required(args, "goal_id", str)
    goal = tg.get_goal(goal_id)
    if goal is None:
        return _err(f"unknown goal_id: {goal_id!r}")
    return json.dumps(
        {
            "id": goal.id,
            "title": goal.title,
            "status": goal.status.value,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value,
                    "depends_on": list(t.depends_on),
                    "retries": t.retries,
                    "estimated_cost_tokens": t.estimated_cost_tokens,
                    "actual_cost_tokens": t.actual_cost_tokens,
                    "result": t.result,
                    "error": t.error,
                }
                for t in goal.tasks
            ],
        }
    )


def _get_task(tg: Any, args: dict) -> str:
    task_id = _required(args, "task_id", str)
    task = tg.get_task(task_id)
    if task is None:
        return _err(f"unknown task_id: {task_id!r}")
    return json.dumps(
        {
            "id": task.id,
            "goal_id": task.goal_id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "depends_on": list(task.depends_on),
            "retries": task.retries,
            "max_retries": task.max_retries,
            "estimated_cost_tokens": task.estimated_cost_tokens,
            "actual_cost_tokens": task.actual_cost_tokens,
            "result": task.result,
            "error": task.error,
        }
    )


def _attention(tg: Any, args: dict) -> str:
    """Return the attention-formatted task list for a goal — ready to
    inject into the prompt verbatim by the agent."""
    goal_id = _required(args, "goal_id", str)
    items = tg.to_attention_format(goal_id)
    return json.dumps({"items": items})


_DISPATCH = {
    "create_goal": _create_goal,
    "add_task": _add_task,
    "ready": _ready,
    "mark_started": _mark_started,
    "mark_completed": _mark_completed,
    "mark_failed": _mark_failed,
    "cancel_goal": _cancel_goal,
    "progress": _progress,
    "active_goals": _active_goals,
    "get_goal": _get_goal,
    "get_task": _get_task,
    "attention": _attention,
}


# ─────────────────────────── helpers ───────────────────────────


def _required(args: dict, key: str, kind: type) -> Any:
    if key not in args or args[key] in (None, ""):
        raise ValueError(f"missing required arg {key!r}")
    val = args[key]
    if not isinstance(val, kind):
        try:
            val = kind(val)
        except Exception:
            raise ValueError(f"arg {key!r} must be {kind.__name__}")
    return val


def _err(message: str) -> str:
    return json.dumps({"error": message})


# ─────────────────────────── tool schema (OpenAI function format) ───────────────────────────


TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task_graph",
        "description": (
            "Persistent DAG of goals and tasks. Use to break a complex "
            "user request into trackable subtasks with dependencies, then "
            "iterate on them. Each call dispatches one action via the "
            "`action` parameter. State persists across turns."
        ),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_DISPATCH.keys()),
                    "description": "Which TaskGraph operation to perform.",
                },
                "title": {
                    "type": "string",
                    "description": "Goal or task title (for create_goal / add_task).",
                },
                "description": {
                    "type": "string",
                    "description": "Long-form task description (add_task).",
                },
                "goal_id": {
                    "type": "string",
                    "description": "Returned by create_goal.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Returned by add_task.",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task IDs that must complete first.",
                },
                "estimated_cost_tokens": {
                    "type": "integer",
                    "description": "Rough token-cost estimate for budgeting.",
                },
                "max_retries": {
                    "type": "integer",
                    "description": "How many times to retry on failure (default 2).",
                },
                "result": {
                    "type": "string",
                    "description": "Result text for mark_completed.",
                },
                "tokens": {
                    "type": "integer",
                    "description": "Actual tokens spent (mark_completed).",
                },
                "error": {
                    "type": "string",
                    "description": "Error message for mark_failed.",
                },
            },
        },
    },
}


# Optional callable for callers that want to suppress the schema entirely.
def get_schema(enabled: bool = True) -> Optional[dict[str, Any]]:
    return TOOL_SCHEMA if enabled else None


# ─────────────────────────── registry hook ───────────────────────────
#
# Schema is published through the standard registry so the LLM sees the
# tool. The handler here is a stub that is only reachable when the
# registry's generic dispatch path runs without an agent reference —
# ``run_agent.py:_invoke_tool`` short-circuits this tool name to call
# ``task_graph_tool(agent=self, ...)`` with the bridge in scope.
def _check_task_graph_requirements() -> bool:
    """Schema is always discoverable; runtime gating happens via the
    bridge/config inside the handler itself."""
    return True


from tools.registry import registry  # noqa: E402

registry.register(
    name="task_graph",
    toolset="orchestration",
    schema=TOOL_SCHEMA["function"],
    handler=lambda args, **kw: task_graph_tool(
        agent=kw.get("agent"), **(args or {})
    ),
    check_fn=_check_task_graph_requirements,
    emoji="🧩",
)
