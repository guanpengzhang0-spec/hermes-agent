"""LLM-callable tool surface for the multi-phase Orchestrator.

Single tool entry point with an ``action`` parameter:

    orchestration(action="submit_goal", title="ship feature X")
    orchestration(action="tick", goal_id="...")
    orchestration(action="state", goal_id="...")
    orchestration(action="run_to_completion", goal_id="...", max_ticks=20)
    orchestration(action="approve", goal_id="...", feedback="lgtm")
    orchestration(action="reject", goal_id="...", feedback="too risky")
    orchestration(action="cancel", goal_id="...")

The Orchestrator needs a *task executor* — a function that takes
``(goal, task)`` and returns the task's result. We supply a default
executor that prompts the agent's LLM with a per-task instruction. If
the agent provides its own executor (via
``bridge.set_planner_llm`` having been called), that LLM is used.

When the orchestrator subsystem is disabled, every action returns the
disabled marker so the LLM stops calling.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


_DISABLED_RESPONSE = {
    "error": (
        "orchestration is disabled. Required config: "
        "orchestration.task_graph.enabled=true, "
        "orchestration.orchestrator.enabled=true, "
        "orchestration.planner.enabled=true."
    )
}


def orchestration_tool(*, agent: Any = None, action: str = "", **kwargs: Any) -> str:
    bridge = getattr(agent, "_orch_bridge", None) if agent is not None else None
    if bridge is None or getattr(bridge, "task_graph", None) is None:
        return json.dumps(_DISABLED_RESPONSE)

    # Ensure the planner LLM is wired before we touch the orchestrator
    if getattr(bridge, "_planner", None) is None:
        try:
            bridge.set_planner_llm(_make_default_llm(agent))
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"error": f"could not initialize planner LLM: {exc}"}
            )

    executor = _make_default_executor(agent)
    orch = bridge.get_orchestrator(executor)
    if orch is None:
        return json.dumps(_DISABLED_RESPONSE)

    action = (action or "").strip().lower()
    handler = _DISPATCH.get(action)
    if handler is None:
        return json.dumps(
            {"error": f"unknown action {action!r}. Valid: {sorted(_DISPATCH.keys())}"}
        )

    try:
        return handler(orch, bridge, kwargs)
    except KeyError as exc:
        return json.dumps({"error": f"unknown goal/task: {exc}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"internal: {exc}"})


# ─────────────────────────── per-action handlers ───────────────────────────


def _submit_goal(orch: Any, bridge: Any, args: dict) -> str:
    del bridge
    title = str(args.get("title", "")).strip()
    if not title:
        return json.dumps({"error": "missing required arg title"})
    description = str(args.get("description", ""))
    tools = tuple(args.get("available_tools", []) or [])
    goal_id = _run(orch.submit_goal(
        title, description=description, available_tools=tools
    ))
    return json.dumps({"goal_id": goal_id})


def _tick(orch: Any, bridge: Any, args: dict) -> str:
    del bridge
    goal_id = _required_str(args, "goal_id")
    res = _run(orch.tick(goal_id))
    return json.dumps(_tick_result_to_dict(res))


def _state(orch: Any, bridge: Any, args: dict) -> str:
    del bridge
    goal_id = _required_str(args, "goal_id")
    state = orch.get_state(goal_id)
    if state is None:
        return json.dumps({"error": f"unknown goal_id: {goal_id!r}"})
    return json.dumps(
        {
            "goal_id": state.goal_id,
            "phase": state.phase.value,
            "replan_count": state.replan_count,
            "failed_task_id": state.failed_task_id,
            "failed_error": state.failed_error,
        }
    )


def _run_to_completion(orch: Any, bridge: Any, args: dict) -> str:
    del bridge
    goal_id = _required_str(args, "goal_id")
    max_ticks = int(args.get("max_ticks", 50) or 50)
    state = _run(orch.run_to_completion(goal_id, max_ticks=max_ticks))
    return json.dumps(
        {
            "goal_id": state.goal_id,
            "phase": state.phase.value,
            "replan_count": state.replan_count,
            "failed_error": state.failed_error,
        }
    )


def _approve(orch: Any, bridge: Any, args: dict) -> str:
    del orch
    goal_id = _required_str(args, "goal_id")
    pm = bridge.get_plan_mode()
    if pm is None:
        return json.dumps({"error": "plan_mode unavailable"})
    res = pm.approve(goal_id, feedback=str(args.get("feedback", "")))
    return json.dumps({"goal_id": goal_id, "verdict": res.verdict.value})


def _reject(orch: Any, bridge: Any, args: dict) -> str:
    del orch
    goal_id = _required_str(args, "goal_id")
    pm = bridge.get_plan_mode()
    if pm is None:
        return json.dumps({"error": "plan_mode unavailable"})
    res = pm.reject(goal_id, feedback=str(args.get("feedback", "")))
    return json.dumps({"goal_id": goal_id, "verdict": res.verdict.value})


def _request_changes(orch: Any, bridge: Any, args: dict) -> str:
    del orch
    goal_id = _required_str(args, "goal_id")
    pm = bridge.get_plan_mode()
    if pm is None:
        return json.dumps({"error": "plan_mode unavailable"})
    res = pm.request_changes(goal_id, feedback=str(args.get("feedback", "")))
    return json.dumps({"goal_id": goal_id, "verdict": res.verdict.value})


def _cancel(orch: Any, bridge: Any, args: dict) -> str:
    del bridge
    goal_id = _required_str(args, "goal_id")
    orch.cancel(goal_id)
    return json.dumps({"goal_id": goal_id, "phase": "failed"})


_DISPATCH = {
    "submit_goal": _submit_goal,
    "tick": _tick,
    "state": _state,
    "run_to_completion": _run_to_completion,
    "approve": _approve,
    "reject": _reject,
    "request_changes": _request_changes,
    "cancel": _cancel,
}


# ─────────────────────────── helpers ───────────────────────────


def _required_str(args: dict, key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"missing required arg {key!r}")
    return val.strip()


def _tick_result_to_dict(res: Any) -> dict[str, Any]:
    return {
        "phase": res.phase.value,
        "goal_id": res.goal_id,
        "tasks_assigned": res.tasks_assigned,
        "tasks_completed": res.tasks_completed,
        "tasks_failed": res.tasks_failed,
        "notes": list(res.notes),
    }


def _run(awaitable: Any) -> Any:
    """Run an async coroutine from sync code. Uses the current loop
    if one exists (e.g. pytest-asyncio), otherwise spawns one."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Run the coroutine on a fresh loop in another thread —
            # asyncio.run from inside a running loop raises.
            import threading
            result_holder: dict[str, Any] = {}

            def runner() -> None:
                new_loop = asyncio.new_event_loop()
                try:
                    result_holder["v"] = new_loop.run_until_complete(awaitable)
                except BaseException as exc:  # noqa: BLE001
                    result_holder["e"] = exc
                finally:
                    new_loop.close()

            t = threading.Thread(target=runner)
            t.start()
            t.join()
            if "e" in result_holder:
                raise result_holder["e"]
            return result_holder.get("v")
        return loop.run_until_complete(awaitable)
    except RuntimeError:
        return asyncio.run(awaitable)


def _make_default_llm(agent: Any):
    """Build a simple text-in / text-out callable backed by the agent's
    primary chat model.

    Falls back to a no-op stub when the agent has no usable client —
    useful for tests, pyright, and disabled-config runs.
    """

    def call(prompt: str) -> str:
        try:
            client = getattr(agent, "client", None)
            model = getattr(agent, "model", None)
            if client is None or not model:
                return _stub_planner_response()
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            return resp.choices[0].message.content or _stub_planner_response()
        except Exception:
            return _stub_planner_response()

    return call


def _stub_planner_response() -> str:
    """Single-task fallback plan so the orchestrator can advance without
    a real LLM (used in tests / disabled scenarios)."""
    return json.dumps(
        {
            "tasks": [
                {
                    "id": "single",
                    "title": "execute the goal directly",
                    "description": "no LLM available — single-task fallback",
                    "depends_on": [],
                    "estimated_cost_tokens": 200,
                }
            ],
            "rationale": "fallback: planner LLM unavailable",
        }
    )


def _make_default_executor(agent: Any):
    """Build an async executor that turns each task into a sub-prompt
    and asks the agent's chat model. Returns the response text.

    A real production executor would re-enter the agent's main
    conversation loop — we keep this minimal so the tool works
    out-of-the-box for simple goals.
    """

    async def execute(goal: Any, task: Any) -> str:
        prompt = (
            f"Goal: {goal.title}\n\n"
            f"Task: {task.title}\n"
            f"Description: {task.description or '(none)'}\n\n"
            "Perform this task and return a concise result."
        )
        try:
            client = getattr(agent, "client", None)
            model = getattr(agent, "model", None)
            if client is None or not model:
                return f"[stub] would have done: {task.title}"

            def _call() -> str:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
                return resp.choices[0].message.content or ""

            return await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            return f"[error] {exc}"

    return execute


# ─────────────────────────── tool schema ───────────────────────────


TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "orchestration",
        "description": (
            "Multi-phase goal orchestrator. Use for complex tasks "
            "(>3 distinct steps, parallelisable, needs planning). "
            "Workflow: submit_goal → run_to_completion (auto-runs the "
            "planner + plan_review + executor + replanner). State-machine "
            "phases are: idle → classifying → planning → plan_review → "
            "executing → (replanning) → complete/failed."
        ),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_DISPATCH.keys()),
                },
                "title": {"type": "string", "description": "Goal title (for submit_goal)."},
                "description": {"type": "string"},
                "available_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names the planner may use.",
                },
                "goal_id": {"type": "string"},
                "feedback": {
                    "type": "string",
                    "description": "Reviewer feedback for approve/reject/request_changes.",
                },
                "max_ticks": {
                    "type": "integer",
                    "description": "Cap for run_to_completion (default 50).",
                },
            },
        },
    },
}


# ─────────────────────────── registry hook ───────────────────────────


def _check_orchestration_requirements() -> bool:
    return True


from tools.registry import registry  # noqa: E402

registry.register(
    name="orchestration",
    toolset="orchestration",
    schema=TOOL_SCHEMA["function"],
    handler=lambda args, **kw: orchestration_tool(
        agent=kw.get("agent"), **(args or {})
    ),
    check_fn=_check_orchestration_requirements,
    emoji="🎼",
)
