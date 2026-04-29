"""Hermes orchestration package.

Contains modules borrowed and localized from Conway-Research/automaton:
  - loop_detector  : tool-call loop / stuck-pattern / idle-only detection
  - injection_defense : input sanitization (planned)
  - attention      : Manus-style todo block injection (planned)
  - health_monitor : aggregated health snapshot (planned)
  - task_graph     : DAG-based task graph (planned)
  - orchestrator   : multi-phase state machine (planned)
  - planner        : LLM-driven goal decomposition (planned)
  - plan_mode      : plan review controller (planned)

All modules are opt-in via config.yaml ``orchestration.<module>.enabled``.
Default is OFF — importing this package has no side effects on the agent loop.
"""

from .attention import (
    DEFAULT_MAX_TODO_TOKENS,
    format_attention_block,
    inject_attention_block,
    render_todo_store,
)
from .health_monitor import (
    HealthMonitor,
    HealthSnapshot,
    HealthStatus,
)
from .injection_defense import (
    InjectionDefense,
    InjectionRisk,
    InjectionScanResult,
    TrustLevel,
)
from .loop_detector import (
    LoopAction,
    LoopCheckResult,
    LoopDetector,
)
from .orchestrator import Orchestrator, OrchestratorState, Phase, TickResult
from .plan_mode import PlanMode, PlanReviewResult, ReviewVerdict
from .planner import (
    LLMCaller,
    PlannedTask,
    Planner,
    PlannerInput,
    PlannerOutput,
)
from .task_graph import (
    Goal,
    TaskGraph,
    TaskNode,
    TaskStatus,
)

__all__ = [
    "DEFAULT_MAX_TODO_TOKENS",
    "Goal",
    "HealthMonitor",
    "HealthSnapshot",
    "HealthStatus",
    "InjectionDefense",
    "InjectionRisk",
    "InjectionScanResult",
    "LLMCaller",
    "LoopAction",
    "LoopCheckResult",
    "LoopDetector",
    "Orchestrator",
    "OrchestratorState",
    "Phase",
    "PlanMode",
    "PlanReviewResult",
    "PlannedTask",
    "Planner",
    "PlannerInput",
    "PlannerOutput",
    "ReviewVerdict",
    "TaskGraph",
    "TaskNode",
    "TaskStatus",
    "TickResult",
    "TrustLevel",
    "format_attention_block",
    "inject_attention_block",
    "render_todo_store",
]
