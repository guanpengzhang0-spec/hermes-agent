"""Aggregated health monitor for the agent main loop.

Read-only observer over four signals:
  * API call success rate / latency (sliding window)
  * Idle-turn streak (no mutating tools)
  * Token throughput (tokens/second over the window)
  * Loop-detector decisions (count of WARN/BLOCK/HALT)

Computes a ``HealthSnapshot`` with a recommended ``suggested_action``
that the caller (run_conversation) decides whether to apply.

The monitor is **stateful** but **purely additive** — it never reaches
back into the agent or aborts anything. That separation keeps the
decision-making explicit at the call site.

Thread-safety: every record / evaluate method acquires an internal
RLock so concurrent tool calls and API call observers don't corrupt
the window.

Storage: in-memory only. Optionally emits a ``health_snapshot`` event
to an attached EventStream when ``evaluate()`` is called and the
status changes — this is how trends get persisted for later debugging.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class HealthStatus(str, Enum):
    """Coarse health categorization. Ordered from healthy to broken."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    HALTED = "halted"


@dataclass(frozen=True)
class HealthSnapshot:
    """Point-in-time health view. Immutable so callers can keep a
    reference around for logging without worrying about mutation."""

    status: HealthStatus
    metrics: dict[str, float]
    issues: tuple[str, ...]
    suggested_action: str  # "continue" | "compress" | "throttle" | "force_sleep" | "halt"


@dataclass
class _ApiCallRecord:
    success: bool
    latency_ms: float
    tokens: int
    ts: float


# Lightweight protocol to avoid hard import dependency on EventStream.
# Anything with .emit(kind, *, actor, payload, ...) works.
class _EventEmitter:
    """Structural type — actual hermes EventStream satisfies this."""

    def emit(
        self,
        kind: str,
        *,
        actor: str,
        payload: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Optional[int]: ...


class HealthMonitor:
    """Sliding-window health aggregator.

    Default thresholds are deliberately conservative — DEGRADED is hit
    quickly so the caller can react (compress, switch model) before
    things actually break. Tune via constructor.
    """

    def __init__(
        self,
        event_stream: Optional[_EventEmitter] = None,
        *,
        api_error_rate_threshold: float = 0.5,
        idle_turn_threshold: int = 5,
        token_throughput_floor: float = 10.0,
        latency_p95_critical_ms: float = 60_000.0,
        loop_block_critical_count: int = 3,
        window_size: int = 30,
        actor: str = "health_monitor",
    ) -> None:
        if not 0.0 < api_error_rate_threshold <= 1.0:
            raise ValueError("api_error_rate_threshold must be in (0, 1]")
        if idle_turn_threshold < 1:
            raise ValueError("idle_turn_threshold must be >= 1")
        if token_throughput_floor < 0:
            raise ValueError("token_throughput_floor must be >= 0")
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        if loop_block_critical_count < 1:
            raise ValueError("loop_block_critical_count must be >= 1")

        self._event_stream = event_stream
        self._api_error_rate_threshold = api_error_rate_threshold
        self._idle_turn_threshold = idle_turn_threshold
        self._token_throughput_floor = token_throughput_floor
        self._latency_p95_critical_ms = latency_p95_critical_ms
        self._loop_block_critical_count = loop_block_critical_count
        self._window_size = window_size
        self._actor = actor

        self._lock = threading.RLock()
        self._api_calls: deque[_ApiCallRecord] = deque(maxlen=window_size)
        self._loop_actions: deque[str] = deque(maxlen=window_size)
        self._consecutive_idle_turns: int = 0
        self._last_status: Optional[HealthStatus] = None
        self._window_start_ts: float = time.time()

    # ─────────────────────────── recording ───────────────────────────

    def record_api_call(
        self, *, success: bool, latency_ms: float, tokens: int
    ) -> None:
        with self._lock:
            self._api_calls.append(
                _ApiCallRecord(
                    success=bool(success),
                    latency_ms=max(0.0, float(latency_ms)),
                    tokens=max(0, int(tokens)),
                    ts=time.time(),
                )
            )

    def record_loop_check(self, action: str) -> None:
        with self._lock:
            self._loop_actions.append(action.lower())

    def record_idle_turn(self) -> None:
        with self._lock:
            self._consecutive_idle_turns += 1

    def record_mutation_turn(self) -> None:
        with self._lock:
            self._consecutive_idle_turns = 0

    def reset_window(self) -> None:
        with self._lock:
            self._api_calls.clear()
            self._loop_actions.clear()
            self._consecutive_idle_turns = 0
            self._window_start_ts = time.time()
            self._last_status = None

    # ─────────────────────────── evaluation ───────────────────────────

    def evaluate(self) -> HealthSnapshot:
        with self._lock:
            metrics = self._compute_metrics_locked()
            issues: list[str] = []
            critical = False
            degraded = False

            err_rate = metrics["api_error_rate"]
            if metrics["api_calls"] > 0:
                if err_rate >= self._api_error_rate_threshold:
                    issues.append(
                        f"api_error_rate={err_rate:.2f} >= "
                        f"{self._api_error_rate_threshold:.2f}"
                    )
                    if err_rate >= min(1.0, self._api_error_rate_threshold + 0.3):
                        critical = True
                    else:
                        degraded = True

            if metrics["latency_p95_ms"] >= self._latency_p95_critical_ms:
                issues.append(
                    f"latency_p95_ms={metrics['latency_p95_ms']:.0f} >= "
                    f"{self._latency_p95_critical_ms:.0f}"
                )
                critical = True

            if metrics["consecutive_idle_turns"] >= self._idle_turn_threshold:
                issues.append(
                    f"idle_turns={int(metrics['consecutive_idle_turns'])} "
                    f">= {self._idle_turn_threshold}"
                )
                degraded = True

            if (
                metrics["api_calls"] >= 3
                and metrics["token_throughput_per_sec"] > 0
                and metrics["token_throughput_per_sec"]
                < self._token_throughput_floor
            ):
                issues.append(
                    f"throughput={metrics['token_throughput_per_sec']:.1f} "
                    f"< {self._token_throughput_floor:.1f}"
                )
                degraded = True

            if metrics["loop_block_count"] >= self._loop_block_critical_count:
                issues.append(
                    f"loop_blocks={int(metrics['loop_block_count'])} "
                    f">= {self._loop_block_critical_count}"
                )
                critical = True

            if metrics["loop_halt_count"] > 0:
                issues.append(
                    f"loop_halts={int(metrics['loop_halt_count'])}"
                )
                # HALT is an explicit caller signal — it's already terminal
                status = HealthStatus.HALTED
                action = "halt"
            elif critical:
                status = HealthStatus.CRITICAL
                action = self._suggest_critical_action(metrics)
            elif degraded:
                status = HealthStatus.DEGRADED
                action = self._suggest_degraded_action(metrics)
            else:
                status = HealthStatus.HEALTHY
                action = "continue"

            snapshot = HealthSnapshot(
                status=status,
                metrics=metrics,
                issues=tuple(issues),
                suggested_action=action,
            )

            # Emit on status change
            if self._event_stream is not None and status != self._last_status:
                try:
                    self._event_stream.emit(
                        "health_snapshot",
                        actor=self._actor,
                        payload={
                            "status": status.value,
                            "previous": (
                                self._last_status.value if self._last_status else None
                            ),
                            "metrics": metrics,
                            "issues": list(issues),
                            "suggested_action": action,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
            self._last_status = status

        return snapshot

    # ─────────────────────────── internals ───────────────────────────

    def _compute_metrics_locked(self) -> dict[str, float]:
        n_calls = len(self._api_calls)
        successes = sum(1 for r in self._api_calls if r.success)
        failures = n_calls - successes
        err_rate = (failures / n_calls) if n_calls else 0.0

        latencies = sorted(r.latency_ms for r in self._api_calls)
        if latencies:
            idx = max(0, int(len(latencies) * 0.95) - 1)
            p95 = latencies[idx]
            mean = sum(latencies) / len(latencies)
        else:
            p95 = 0.0
            mean = 0.0

        total_tokens = sum(r.tokens for r in self._api_calls)
        elapsed = max(1e-3, time.time() - self._window_start_ts)
        throughput = total_tokens / elapsed

        loop_block = sum(
            1 for a in self._loop_actions if a in ("block", "halt")
        )
        loop_warn = sum(1 for a in self._loop_actions if a == "warn")
        loop_halt = sum(1 for a in self._loop_actions if a == "halt")

        return {
            "api_calls": float(n_calls),
            "api_error_rate": float(err_rate),
            "api_failures": float(failures),
            "latency_p95_ms": float(p95),
            "latency_mean_ms": float(mean),
            "token_total": float(total_tokens),
            "token_throughput_per_sec": float(throughput),
            "consecutive_idle_turns": float(self._consecutive_idle_turns),
            "loop_warn_count": float(loop_warn),
            "loop_block_count": float(loop_block),
            "loop_halt_count": float(loop_halt),
            "window_age_sec": float(elapsed),
        }

    @staticmethod
    def _suggest_degraded_action(metrics: dict[str, float]) -> str:
        # Idle dominant → poke the model
        if metrics["consecutive_idle_turns"] >= 3:
            return "force_action"
        # Low throughput likely = compaction backlog
        if metrics["token_throughput_per_sec"] < 5 and metrics["api_calls"] >= 3:
            return "compress"
        return "throttle"

    @staticmethod
    def _suggest_critical_action(metrics: dict[str, float]) -> str:
        if metrics["loop_block_count"] >= 3:
            return "force_sleep"
        if metrics["api_error_rate"] >= 0.8:
            return "rotate_credentials"
        return "force_sleep"

    # ─────────────────────────── introspection ───────────────────────────

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "config": {
                    "api_error_rate_threshold": self._api_error_rate_threshold,
                    "idle_turn_threshold": self._idle_turn_threshold,
                    "token_throughput_floor": self._token_throughput_floor,
                    "latency_p95_critical_ms": self._latency_p95_critical_ms,
                    "loop_block_critical_count": self._loop_block_critical_count,
                    "window_size": self._window_size,
                },
                "state": {
                    "api_calls_recorded": len(self._api_calls),
                    "loop_actions_recorded": len(self._loop_actions),
                    "consecutive_idle_turns": self._consecutive_idle_turns,
                    "window_age_sec": time.time() - self._window_start_ts,
                    "last_status": (
                        self._last_status.value if self._last_status else None
                    ),
                },
            }
