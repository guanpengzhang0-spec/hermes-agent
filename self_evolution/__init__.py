"""Hermes self-evolution package.

Houses subsystems that observe / record / govern the agent's
self-modification operations (skill installation, config edits,
SOUL.md updates). Companion to ``~/.hermes/skills/self-evolution/``.

Modules:
  - audit_log : append-only audit of self-modifications
"""

from .audit_log import AuditEntry, AuditKind, AuditLog

__all__ = ["AuditEntry", "AuditKind", "AuditLog"]
