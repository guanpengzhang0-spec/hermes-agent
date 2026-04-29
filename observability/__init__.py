"""Hermes observability package.

Modules:
  - event_stream : append-only SQLite-backed event log shared across
                   orchestration / loop_detector / health_monitor /
                   audit_log subsystems.

All modules in this package are designed to be **non-blocking** for the
main agent loop: failures are logged but never raised. They are
opt-in via ``config.yaml`` ``observability.<module>.enabled``.
"""

from .event_stream import Event, EventStream

__all__ = ["Event", "EventStream"]
