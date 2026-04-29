"""Unit tests for ``orchestration.health_monitor``."""

from __future__ import annotations

import pytest

from orchestration.health_monitor import (
    HealthMonitor,
    HealthSnapshot,
    HealthStatus,
)


def test_initial_state_is_healthy():
    h = HealthMonitor()
    snap = h.evaluate()
    assert snap.status is HealthStatus.HEALTHY
    assert snap.suggested_action == "continue"
    assert snap.issues == ()


def test_high_error_rate_degraded():
    h = HealthMonitor(api_error_rate_threshold=0.5)
    for _ in range(2):
        h.record_api_call(success=True, latency_ms=100, tokens=100)
    for _ in range(2):
        h.record_api_call(success=False, latency_ms=200, tokens=0)
    snap = h.evaluate()
    assert snap.status is HealthStatus.DEGRADED
    assert any("api_error_rate" in i for i in snap.issues)


def test_extreme_error_rate_critical():
    h = HealthMonitor(api_error_rate_threshold=0.5)
    for _ in range(8):
        h.record_api_call(success=False, latency_ms=200, tokens=0)
    h.record_api_call(success=True, latency_ms=200, tokens=10)
    snap = h.evaluate()
    assert snap.status is HealthStatus.CRITICAL


def test_idle_turn_streak_degraded():
    h = HealthMonitor(idle_turn_threshold=3)
    h.record_idle_turn()
    h.record_idle_turn()
    snap = h.evaluate()
    assert snap.status is HealthStatus.HEALTHY
    h.record_idle_turn()
    snap = h.evaluate()
    assert snap.status is HealthStatus.DEGRADED
    assert any("idle_turns" in i for i in snap.issues)


def test_mutation_turn_resets_idle_counter():
    h = HealthMonitor(idle_turn_threshold=3)
    h.record_idle_turn()
    h.record_idle_turn()
    h.record_mutation_turn()
    h.record_idle_turn()
    snap = h.evaluate()
    assert snap.status is HealthStatus.HEALTHY


def test_loop_block_critical():
    h = HealthMonitor(loop_block_critical_count=3)
    for _ in range(3):
        h.record_loop_check("block")
    snap = h.evaluate()
    assert snap.status is HealthStatus.CRITICAL
    assert snap.suggested_action == "force_sleep"


def test_loop_halt_yields_halted_status():
    h = HealthMonitor()
    h.record_loop_check("halt")
    snap = h.evaluate()
    assert snap.status is HealthStatus.HALTED
    assert snap.suggested_action == "halt"


def test_high_latency_critical():
    h = HealthMonitor(latency_p95_critical_ms=5000)
    for _ in range(20):
        h.record_api_call(success=True, latency_ms=10_000, tokens=100)
    snap = h.evaluate()
    assert snap.status is HealthStatus.CRITICAL


def test_metrics_shape():
    h = HealthMonitor()
    h.record_api_call(success=True, latency_ms=50, tokens=200)
    snap = h.evaluate()
    expected_keys = {
        "api_calls", "api_error_rate", "api_failures",
        "latency_p95_ms", "latency_mean_ms",
        "token_total", "token_throughput_per_sec",
        "consecutive_idle_turns",
        "loop_warn_count", "loop_block_count", "loop_halt_count",
        "window_age_sec",
    }
    assert set(snap.metrics.keys()) == expected_keys


def test_event_stream_emit_on_status_change():
    emits: list = []

    class StubStream:
        def emit(self, kind: str, *, actor: str, payload=None, session_id=None):
            emits.append((kind, actor, payload))
            return 1

    h = HealthMonitor(StubStream(), idle_turn_threshold=1)
    h.evaluate()  # initial healthy
    h.record_idle_turn()
    h.evaluate()  # degraded — emits
    h.evaluate()  # still degraded — does NOT emit again
    kinds = [e[0] for e in emits]
    actors = [e[1] for e in emits]
    assert "health_snapshot" in kinds
    assert all(a == "health_monitor" for a in actors)
    # Initial healthy + one degraded transition = at most 2 events
    assert len(emits) <= 2


def test_event_stream_emit_failure_swallowed():
    class BrokenStream:
        def emit(self, kind, *, actor, payload=None, session_id=None):
            raise RuntimeError("disk full")

    h = HealthMonitor(BrokenStream(), idle_turn_threshold=1)
    h.record_idle_turn()
    snap = h.evaluate()  # must not raise
    assert snap.status is HealthStatus.DEGRADED


def test_reset_window_clears_state():
    h = HealthMonitor()
    h.record_api_call(success=False, latency_ms=200, tokens=0)
    h.record_idle_turn()
    h.reset_window()
    snap = h.evaluate()
    assert snap.status is HealthStatus.HEALTHY
    assert snap.metrics["api_calls"] == 0
    assert snap.metrics["consecutive_idle_turns"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_error_rate_threshold": 0.0},
        {"api_error_rate_threshold": 1.5},
        {"idle_turn_threshold": 0},
        {"token_throughput_floor": -1.0},
        {"window_size": 0},
        {"loop_block_critical_count": 0},
    ],
)
def test_invalid_config_raises(kwargs):
    with pytest.raises(ValueError):
        HealthMonitor(**kwargs)


def test_snapshot_introspection():
    h = HealthMonitor()
    snap = h.snapshot()
    assert "config" in snap and "state" in snap
    assert snap["state"]["api_calls_recorded"] == 0


def test_health_snapshot_is_frozen():
    h = HealthMonitor()
    snap = h.evaluate()
    assert isinstance(snap, HealthSnapshot)
    with pytest.raises((AttributeError, Exception)):
        snap.status = HealthStatus.CRITICAL  # type: ignore[misc]
