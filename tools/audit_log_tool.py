"""LLM-callable tool surface for ``self_evolution.audit_log.AuditLog``.

Single tool entry point with an ``action`` parameter:

    audit_log(action="record", kind="skill_modified",
              target_path="...", new_text="...", old_text="...",
              rationale="...")
    audit_log(action="recent", limit=10)
    audit_log(action="recent", kind="config_changed")
    audit_log(action="verify")

Self-evolution skills that mutate SKILL.md / config.yaml / SOUL.md
should call ``audit_log(action="record", ...)`` BEFORE the file write
so the diff and rationale are persisted with a content hash that can
later be compared against on-disk state via ``verify``.

Returns JSON-serializable dicts. Errors → ``{"error": "..."}``.
"""

from __future__ import annotations

import json
from typing import Any, Optional


_DISABLED_RESPONSE = {
    "error": "audit_log is disabled. Set "
    "self_evolution.audit_log.enabled=true (and "
    "observability.event_stream.enabled=true) in ~/.hermes/config.yaml."
}

# Map LLM-friendly short names to AuditKind values
_KIND_ALIAS = {
    "skill_modified": "audit_skill_modified",
    "config_changed": "audit_config_changed",
    "tool_installed": "audit_tool_installed",
    "tool_removed": "audit_tool_removed",
    "soul_updated": "audit_soul_updated",
    "other": "audit_other",
}


def audit_log_tool(*, agent: Any = None, action: str = "", **kwargs: Any) -> str:
    bridge = getattr(agent, "_orch_bridge", None) if agent is not None else None
    al = getattr(bridge, "audit_log", None) if bridge is not None else None
    if al is None:
        return json.dumps(_DISABLED_RESPONSE)

    action = (action or "").strip().lower()
    if action == "record":
        return _do_record(al, kwargs)
    if action == "recent":
        return _do_recent(al, kwargs)
    if action == "verify":
        return _do_verify(al, kwargs)
    return json.dumps(
        {"error": f"unknown action {action!r}. Valid: record / recent / verify"}
    )


def _do_record(al: Any, args: dict) -> str:
    try:
        from self_evolution.audit_log import AuditKind, AuditLog
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"audit_log import failed: {exc}"})

    kind_short = str(args.get("kind", "other")).strip().lower()
    kind_value = _KIND_ALIAS.get(kind_short, kind_short)
    try:
        kind = AuditKind(kind_value)
    except ValueError:
        return json.dumps(
            {
                "error": f"unknown kind {kind_short!r}. "
                f"Valid: {sorted(_KIND_ALIAS.keys())}"
            }
        )

    target_path = str(args.get("target_path", "")).strip()
    if not target_path:
        return json.dumps({"error": "missing required arg target_path"})

    actor = str(args.get("actor", "agent:tool")).strip() or "agent:tool"
    rationale = str(args.get("rationale", "")).strip()
    if not rationale:
        return json.dumps({"error": "missing required arg rationale"})

    new_text = args.get("new_text")
    old_text = args.get("old_text", "") or ""
    metadata = args.get("metadata") or {}

    if new_text is None:
        # Allow callers that already computed hash + diff to pass them
        diff = str(args.get("diff", ""))
        content_hash = str(args.get("content_hash", ""))
        from self_evolution.audit_log import AuditEntry

        entry = AuditEntry(
            kind=kind,
            actor=actor,
            target_path=target_path,
            diff=diff,
            content_hash=content_hash,
            rationale=rationale,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
    else:
        entry = AuditLog.make_entry_for_text(
            kind,
            actor=actor,
            target_path=target_path,
            new_text=str(new_text),
            old_text=str(old_text),
            rationale=rationale,
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    rid = al.record(entry)
    if rid is None:
        return json.dumps({"error": "audit record failed (event_stream unhealthy)"})
    return json.dumps(
        {
            "audit_id": rid,
            "kind": kind.value,
            "target_path": target_path,
            "content_hash": entry.content_hash,
        }
    )


def _do_recent(al: Any, args: dict) -> str:
    try:
        from self_evolution.audit_log import AuditKind
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"audit_log import failed: {exc}"})

    limit = int(args.get("limit", 20) or 20)
    kind_short = args.get("kind")
    kind: Optional[Any] = None
    if kind_short:
        kind_value = _KIND_ALIAS.get(str(kind_short).lower(), str(kind_short))
        try:
            kind = AuditKind(kind_value)
        except ValueError:
            return json.dumps({"error": f"unknown kind {kind_short!r}"})

    entries = al.query_recent(limit=limit, kind=kind)
    return json.dumps(
        {
            "entries": [
                {
                    "kind": e.kind.value,
                    "actor": e.actor,
                    "target_path": e.target_path,
                    "rationale": e.rationale,
                    "content_hash": e.content_hash,
                    "metadata": e.metadata,
                }
                for e in entries
            ]
        }
    )


def _do_verify(al: Any, args: dict) -> str:
    del args
    return json.dumps(al.verify_integrity())


# ─────────────────────────── tool schema ───────────────────────────


TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "audit_log",
        "description": (
            "Append-only audit log for self-modifications "
            "(skill edits, config changes, tool installs). Self-evolution "
            "skills MUST record an entry before writing such files. "
            "Records carry a SHA256 content hash so verify can detect "
            "unaudited tampering."
        ),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["record", "recent", "verify"],
                },
                "kind": {
                    "type": "string",
                    "enum": list(_KIND_ALIAS.keys()),
                    "description": "Category of self-mod (record / recent filter).",
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "Who is making the change, e.g. 'skill:loop-guard', "
                        "'user:cli', 'agent:tool'."
                    ),
                },
                "target_path": {
                    "type": "string",
                    "description": "Absolute path to the file being modified.",
                },
                "new_text": {
                    "type": "string",
                    "description": (
                        "Full new file content. AuditLog computes the diff "
                        "and SHA256 from this. Pass empty string for deletion."
                    ),
                },
                "old_text": {
                    "type": "string",
                    "description": "Previous file content (for diff). Empty for create.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this change is being made.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Free-form structured context.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return for action=recent.",
                },
            },
        },
    },
}
