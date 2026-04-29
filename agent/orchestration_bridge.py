"""Bridge between AIAgent and the new orchestration / observability modules.

Centralizes all the wiring so ``run_agent.py`` only needs single-line
calls at hook points. If this file is removed, hermes falls back to
the original behavior (the hook calls become no-ops via the bridge's
``None`` defaults).

Modules wired here (default OFF — controlled by ``config.yaml``):
  * orchestration.loop_detector     — tool-call loop detection
  * orchestration.injection_defense — input sanitization
  * orchestration.attention         — Manus-style todo injection
  * orchestration.health_monitor    — aggregated health snapshot
  * observability.event_stream      — append-only audit log

The bridge does NOT wire Orchestrator / Planner / PlanMode / TaskGraph /
AuditLog — those modules need their own CLI / tool surfaces (added in
a follow-up commit).

Failure mode: every public method swallows exceptions and logs them.
The agent loop must NEVER break because the orchestration layer is
unhealthy. The bridge is purely additive; if a module is misconfigured,
the only effect is that its checks become no-ops.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OrchestrationBridge:
    """Holds optional instances of orchestration modules + provides
    single-call hooks for the agent main loop.

    All instance attributes default to ``None``. Each public hook
    checks for ``None`` and returns a no-op default if the module is
    disabled. This means callers never need to know whether a module
    is enabled — they just call the hook.
    """

    def __init__(self, session_id: str, agent_cfg: Optional[dict] = None) -> None:
        self.session_id = session_id or f"session_{int(time.time())}"
        self.cfg = agent_cfg or {}

        # Module instances — populated by _build()
        self.event_stream: Any = None
        self.loop_detector: Any = None
        self.injection_defense: Any = None
        self.health_monitor: Any = None
        self.task_graph: Any = None
        self.audit_log: Any = None
        # Orchestrator/Planner/PlanMode are constructed lazily on first
        # /plan invocation because they need an executor closure.
        self._planner: Any = None
        self._plan_mode: Any = None
        self._orchestrator: Any = None
        # attention is stateless module functions; nothing to instantiate

        self._attention_max_tokens: int = 2000
        self._attention_include_metrics: bool = False

        self._build()

    # ─────────────────────────── construction ───────────────────────────

    def _build(self) -> None:
        sub_cfg = self._sub_cfg()

        # 1. EventStream first — others may depend on it
        if self._enabled(sub_cfg, "observability.event_stream"):
            try:
                from observability.event_stream import EventStream

                self.event_stream = EventStream(session_id=self.session_id)
                logger.info(
                    "orchestration_bridge: event_stream enabled (db=%s)",
                    self.event_stream.db_path,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orchestration_bridge: event_stream init failed (%s)", exc
                )
                self.event_stream = None

        # 2. LoopDetector
        if self._enabled(sub_cfg, "orchestration.loop_detector"):
            try:
                from orchestration.loop_detector import LoopDetector

                ld_cfg = self._dotted(sub_cfg, "orchestration.loop_detector")
                self.loop_detector = LoopDetector(
                    max_identical_calls=int(
                        ld_cfg.get("max_identical_calls", 3)
                    ),
                    max_pattern_repeats=int(
                        ld_cfg.get("max_pattern_repeats", 3)
                    ),
                    max_idle_only_turns=int(
                        ld_cfg.get("max_idle_only_turns", 3)
                    ),
                    window_size=int(ld_cfg.get("window_size", 10)),
                    stagnation_run=int(ld_cfg.get("stagnation_run", 5)),
                    idle_only_tools=set(
                        ld_cfg.get("idle_only_tools", []) or []
                    ),
                )
                logger.info("orchestration_bridge: loop_detector enabled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orchestration_bridge: loop_detector init failed (%s)", exc
                )

        # 3. InjectionDefense
        if self._enabled(sub_cfg, "orchestration.injection_defense"):
            try:
                from orchestration.injection_defense import (
                    InjectionDefense,
                    InjectionRisk,
                )

                id_cfg = self._dotted(sub_cfg, "orchestration.injection_defense")
                threshold_str = str(
                    id_cfg.get("block_threshold", "high")
                ).lower()
                threshold = InjectionRisk(threshold_str)
                self.injection_defense = InjectionDefense(
                    rules_disabled=frozenset(
                        id_cfg.get("rules_disabled", []) or []
                    ),
                    max_input_chars=int(id_cfg.get("max_input_chars", 50_000)),
                    block_threshold=threshold,
                )
                logger.info("orchestration_bridge: injection_defense enabled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orchestration_bridge: injection_defense init failed (%s)",
                    exc,
                )

        # 4. HealthMonitor
        if self._enabled(sub_cfg, "orchestration.health_monitor"):
            try:
                from orchestration.health_monitor import HealthMonitor

                hm_cfg = self._dotted(sub_cfg, "orchestration.health_monitor")
                self.health_monitor = HealthMonitor(
                    self.event_stream,
                    api_error_rate_threshold=float(
                        hm_cfg.get("api_error_rate_threshold", 0.5)
                    ),
                    idle_turn_threshold=int(
                        hm_cfg.get("idle_turn_threshold", 5)
                    ),
                    token_throughput_floor=float(
                        hm_cfg.get("token_throughput_floor", 10.0)
                    ),
                    window_size=int(hm_cfg.get("window_size", 30)),
                )
                logger.info("orchestration_bridge: health_monitor enabled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orchestration_bridge: health_monitor init failed (%s)", exc
                )

        # 5. Attention (stateless — just config knobs)
        if self._enabled(sub_cfg, "orchestration.attention"):
            att_cfg = self._dotted(sub_cfg, "orchestration.attention")
            self._attention_max_tokens = int(
                att_cfg.get("max_tokens", 2000)
            )
            self._attention_include_metrics = bool(
                att_cfg.get("include_metrics", False)
            )

        # 6. TaskGraph (DAG persistence). Forwards events to event_stream
        # via the on_event hook when both are enabled.
        if self._enabled(sub_cfg, "orchestration.task_graph"):
            try:
                from orchestration.task_graph import TaskGraph

                def _tg_event(kind: str, payload: dict) -> None:
                    self._emit(f"task_{kind}", actor="task_graph", payload=payload)

                self.task_graph = TaskGraph(on_event=_tg_event)
                logger.info("orchestration_bridge: task_graph enabled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orchestration_bridge: task_graph init failed (%s)", exc
                )

        # 7. AuditLog (requires event_stream)
        if self._enabled(sub_cfg, "self_evolution.audit_log"):
            if self.event_stream is None:
                logger.warning(
                    "orchestration_bridge: audit_log requires event_stream — skipping"
                )
            else:
                try:
                    from self_evolution.audit_log import AuditLog

                    al_cfg = self._dotted(sub_cfg, "self_evolution.audit_log")
                    self.audit_log = AuditLog(
                        self.event_stream,
                        enable_git_tag=bool(al_cfg.get("enable_git_tag", True)),
                        git_repo_path=al_cfg.get("git_repo_path") or None,
                    )
                    logger.info("orchestration_bridge: audit_log enabled")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "orchestration_bridge: audit_log init failed (%s)", exc
                    )

    # ─────────────────────────── hooks for run_agent ───────────────────────────

    def reset(self) -> None:
        """Called from AIAgent.reset_session_state."""
        if self.loop_detector is not None:
            try:
                self.loop_detector.reset()
            except Exception as exc:  # noqa: BLE001
                logger.warning("bridge.reset loop_detector (%s)", exc)
        if self.health_monitor is not None:
            try:
                self.health_monitor.reset_window()
            except Exception as exc:  # noqa: BLE001
                logger.warning("bridge.reset health_monitor (%s)", exc)

    def pre_tool_call(self, name: str, args: str) -> Optional[str]:
        """Called BEFORE executing a tool. Return non-None to BLOCK
        the tool call; the returned string is the reason to inject
        as the tool's result.
        """
        if self.loop_detector is None:
            return None
        try:
            decision = self.loop_detector.record_tool_call(name, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge.pre_tool_call (%s)", exc)
            return None
        if getattr(decision, "action", None) is None:
            return None
        action_value = (
            decision.action.value
            if hasattr(decision.action, "value")
            else str(decision.action)
        )
        if action_value == "block":
            self._emit(
                "loop_block",
                actor="loop_detector",
                payload={
                    "tool": name,
                    "rule": decision.rule_hit,
                    "reason": decision.reason,
                },
            )
            if self.health_monitor is not None:
                try:
                    self.health_monitor.record_loop_check("block")
                except Exception:  # noqa: BLE001
                    pass
            return decision.reason
        return None

    def post_tool_call(
        self, name: str, result_text: str, success: bool = True
    ) -> Optional[str]:
        """Called AFTER a tool runs. Returns non-None reason if a
        STAGNATION block fires (caller should still let this turn
        finish, but inject the reason into the next turn).
        """
        if self.loop_detector is None:
            return None
        try:
            decision = self.loop_detector.record_tool_result(
                name,
                result_length=len(result_text or ""),
                success=success,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge.post_tool_call (%s)", exc)
            return None
        action_value = (
            decision.action.value
            if hasattr(decision.action, "value")
            else str(decision.action)
        )
        if action_value == "block":
            self._emit(
                "stagnation_block",
                actor="loop_detector",
                payload={"tool": name, "reason": decision.reason},
            )
            return decision.reason
        return None

    def end_turn(self) -> Optional[tuple[str, bool]]:
        """Called at end of each agent turn. Returns ``(reason, halt)``
        when LoopDetector emits WARN/HALT; ``halt=True`` means caller
        should terminate the conversation.
        """
        if self.loop_detector is None:
            return None
        try:
            decision = self.loop_detector.end_turn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge.end_turn loop_detector (%s)", exc)
            return None
        action_value = (
            decision.action.value
            if hasattr(decision.action, "value")
            else str(decision.action)
        )
        if action_value in ("warn", "halt", "block"):
            self._emit(
                f"loop_{action_value}",
                actor="loop_detector",
                payload={"rule": decision.rule_hit, "reason": decision.reason},
            )
            if self.health_monitor is not None:
                try:
                    self.health_monitor.record_loop_check(action_value)
                except Exception:  # noqa: BLE001
                    pass
            return (decision.reason, action_value == "halt")
        return None

    def record_idle_turn(self) -> None:
        if self.health_monitor is None:
            return
        try:
            self.health_monitor.record_idle_turn()
        except Exception:  # noqa: BLE001
            pass

    def record_mutation_turn(self) -> None:
        if self.health_monitor is None:
            return
        try:
            self.health_monitor.record_mutation_turn()
        except Exception:  # noqa: BLE001
            pass

    def record_api_call(
        self, *, success: bool, latency_ms: float, tokens: int
    ) -> None:
        if self.health_monitor is None:
            return
        try:
            self.health_monitor.record_api_call(
                success=success, latency_ms=latency_ms, tokens=tokens
            )
        except Exception:  # noqa: BLE001
            pass

    def evaluate_health(self) -> Optional[Any]:
        """Returns HealthSnapshot or None when disabled."""
        if self.health_monitor is None:
            return None
        try:
            return self.health_monitor.evaluate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge.evaluate_health (%s)", exc)
            return None

    # ─────────── attention ───────────

    def attention_block(self, todo_store: Any) -> Optional[str]:
        """Drop-in replacement for ``todo_store.format_for_injection()``.

        Returns the rendered Active Goals & Tasks block, or ``None``
        when there's nothing to inject. Honours the configured token
        budget. When the attention module is disabled, falls back to
        the legacy ``format_for_injection`` so behavior is unchanged.
        """
        if not getattr(self, "_attention_max_tokens", None) or not self._is_attention_enabled():
            try:
                return todo_store.format_for_injection()
            except Exception as exc:  # noqa: BLE001
                logger.warning("bridge.attention legacy fallback (%s)", exc)
                return None
        try:
            from orchestration.attention import render_todo_store

            return render_todo_store(
                todo_store,
                max_tokens=self._attention_max_tokens,
                include_metrics=self._attention_include_metrics,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge.attention render failed (%s)", exc)
            try:
                return todo_store.format_for_injection()
            except Exception:
                return None

    # ─────────── injection defense ───────────

    def scan_input(
        self, text: str, *, trust: str = "user"
    ) -> Optional[dict]:
        """Scan an input string. Returns a dict with keys:

            { "blocked": bool, "risk": str, "matched": list,
              "sanitized": str, "excerpt": str | None }

        Or ``None`` when the defense is disabled (caller passes text
        through unchanged in that case).
        """
        if self.injection_defense is None:
            return None
        try:
            from orchestration.injection_defense import TrustLevel

            trust_enum = TrustLevel(trust.lower())
            result = self.injection_defense.scan(text or "", trust_enum)
            blocked = self.injection_defense.should_block(result)
            if blocked:
                self._emit(
                    "injection_block",
                    actor="injection_defense",
                    payload={
                        "trust": trust,
                        "risk": result.risk.value,
                        "matched": list(result.matched_rules),
                        "excerpt": result.raw_excerpt,
                    },
                )
            return {
                "blocked": blocked,
                "risk": result.risk.value,
                "matched": list(result.matched_rules),
                "sanitized": result.sanitized,
                "excerpt": result.raw_excerpt,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge.scan_input (%s)", exc)
            return None

    # ─────────── orchestrator (lazy construction) ───────────

    def get_orchestrator(self, executor) -> Optional[Any]:
        """Return a lazily-constructed Orchestrator wired to TaskGraph,
        Planner, and PlanMode. Returns ``None`` when the necessary
        modules are not all enabled.

        ``executor`` is an async callable ``(goal, task) -> str`` that
        the caller supplies (it's typically a closure over the agent
        that runs each task as a sub-conversation).
        """
        if self._orchestrator is not None:
            return self._orchestrator
        if self.task_graph is None:
            return None
        sub_cfg = self._sub_cfg()
        if not self._enabled(sub_cfg, "orchestration.orchestrator"):
            return None
        try:
            from orchestration.orchestrator import Orchestrator
            from orchestration.plan_mode import PlanMode
            from orchestration.planner import Planner

            # Planner needs the agent's LLM injected first
            if self._planner is None:
                logger.warning(
                    "orchestration_bridge: planner needs an LLMCaller — "
                    "call bridge.set_planner_llm(llm) first"
                )
                return None
            if self._plan_mode is None:
                pm_cfg = self._dotted(sub_cfg, "orchestration.plan_mode")
                self._plan_mode = PlanMode(
                    auto_approve=bool(pm_cfg.get("auto_approve", True)),
                    require_approval_for_risky=bool(
                        pm_cfg.get("require_approval_for_risky", True)
                    ),
                )
            orch_cfg = self._dotted(sub_cfg, "orchestration.orchestrator")

            def _orch_event(kind: str, payload: dict) -> None:
                self._emit(f"orch_{kind}", actor="orchestrator", payload=payload)

            self._orchestrator = Orchestrator(
                self.task_graph,
                self._planner,
                self._plan_mode,
                executor,
                max_replans=int(orch_cfg.get("max_replans", 3)),
                phase_timeout_sec=int(orch_cfg.get("phase_timeout_sec", 300)),
                on_event=_orch_event,
            )
            logger.info("orchestration_bridge: orchestrator constructed")
            return self._orchestrator
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "orchestration_bridge: orchestrator construction failed (%s)",
                exc,
            )
            return None

    def set_planner_llm(self, llm) -> None:
        """Inject the LLM caller used by the Planner. Must be called
        before ``get_orchestrator`` returns a non-None orchestrator.

        ``llm`` is a callable ``(prompt: str) -> str``.
        """
        sub_cfg = self._sub_cfg()
        if not self._enabled(sub_cfg, "orchestration.planner"):
            return
        try:
            from orchestration.planner import Planner

            pl_cfg = self._dotted(sub_cfg, "orchestration.planner")
            self._planner = Planner(
                llm,
                max_tasks_per_plan=int(pl_cfg.get("max_tasks_per_plan", 10)),
                retry_on_invalid_json=int(
                    pl_cfg.get("retry_on_invalid_json", 2)
                ),
            )
            logger.info("orchestration_bridge: planner constructed")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "orchestration_bridge: planner construction failed (%s)", exc
            )

    def get_plan_mode(self) -> Optional[Any]:
        """Return the PlanMode instance for external approve/reject
        calls. Returns None if Orchestrator was never built."""
        return self._plan_mode

    # ─────────── lifecycle ───────────

    def close(self) -> None:
        """Called from AIAgent.close()."""
        if self.task_graph is not None:
            try:
                self.task_graph.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("bridge.close task_graph (%s)", exc)
            self.task_graph = None
        if self.event_stream is not None:
            try:
                self.event_stream.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("bridge.close event_stream (%s)", exc)
            self.event_stream = None

    # ─────────── helpers ───────────

    def _emit(
        self, kind: str, *, actor: str, payload: dict[str, Any]
    ) -> None:
        if self.event_stream is None:
            return
        try:
            self.event_stream.emit(kind, actor=actor, payload=payload)
        except Exception:  # noqa: BLE001
            pass

    def _is_attention_enabled(self) -> bool:
        return self._enabled(self._sub_cfg(), "orchestration.attention")

    def _sub_cfg(self) -> dict:
        return dict(self.cfg) if isinstance(self.cfg, dict) else {}

    @staticmethod
    def _enabled(cfg: dict, dotted_key: str) -> bool:
        node = OrchestrationBridge._dotted(cfg, dotted_key)
        return bool(node.get("enabled", False))

    @staticmethod
    def _dotted(cfg: dict, dotted_key: str) -> dict:
        node: Any = cfg
        for part in dotted_key.split("."):
            if not isinstance(node, dict):
                return {}
            node = node.get(part, {})
        return node if isinstance(node, dict) else {}
