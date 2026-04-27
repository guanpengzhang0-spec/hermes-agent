#!/usr/bin/env python3
"""``claude_code_acp`` — delegate a coding task to Claude Code over ACP.

The tool spawns ``@zed-industries/claude-code-acp`` (an ACP shim around the
Claude Code SDK) on demand via ``npx``, sends a single prompt, streams its
progress back into the parent agent's tool-progress channel, and returns the
final assistant text.

Why a dedicated tool when ``delegate_task`` already accepts ``acp_command``?
``delegate_task`` is a Hermes-style multi-agent orchestrator that spawns its
own ``AIAgent`` and treats the ACP transport as a chat backend. This tool
is the thinner path: Hermes stays the orchestrator, Claude Code does one
focused coding turn, no nested agent loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from acp_adapter.client import (
    DEFAULT_CLAUDE_CODE_ACP_ARGS,
    DEFAULT_CLAUDE_CODE_ACP_COMMAND,
    run_claude_code_acp,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


_ALLOWED_PERMISSION_MODES = {"auto", "ask", "deny-write", "deny-all"}


def _check_requirements() -> tuple[bool, str]:
    cmd = os.getenv("HERMES_CLAUDE_CODE_ACP_COMMAND") or DEFAULT_CLAUDE_CODE_ACP_COMMAND
    if shutil.which(cmd) is None:
        return False, (
            f"'{cmd}' not on PATH. Install Node.js (>=20) so 'npx' is available, "
            f"or set HERMES_CLAUDE_CODE_ACP_COMMAND to a binary that speaks ACP."
        )
    return True, ""


def _make_progress_relay(parent_agent: Any) -> Any:
    """Return a sync ``(kind, text) -> None`` that forwards chunks to the parent."""
    cb = getattr(parent_agent, "tool_progress_callback", None) if parent_agent else None
    if cb is None:
        return None

    def _relay(kind: str, text: str) -> None:
        # Truncate long chunks: progress is for live display, not full transcript
        preview = text if len(text) <= 240 else text[:237] + "..."
        event = "subagent.thinking" if kind != "tool" else "subagent.thinking"
        try:
            cb(event, "claude_code_acp", preview, None)
        except Exception:
            logger.debug("tool_progress_callback raised", exc_info=True)

    return _relay


def claude_code_acp(
    prompt: str,
    cwd: str | None = None,
    permission_mode: str = "auto",
    timeout_seconds: float = 600.0,
    parent_agent: Any = None,
) -> str:
    """Synchronous handler — runs the async ACP client to completion."""
    if not isinstance(prompt, str) or not prompt.strip():
        return tool_error("prompt is required and must be a non-empty string")

    if permission_mode not in _ALLOWED_PERMISSION_MODES:
        return tool_error(
            f"permission_mode must be one of {sorted(_ALLOWED_PERMISSION_MODES)}, "
            f"got {permission_mode!r}"
        )

    work_dir = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    if not work_dir.exists():
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return tool_error(f"could not create cwd {work_dir}: {e}")

    cb = _make_progress_relay(parent_agent)

    if cb is not None:
        try:
            cb("message", f"→ claude_code_acp: {prompt[:80]}")
        except Exception:
            pass

    cmd = os.getenv("HERMES_CLAUDE_CODE_ACP_COMMAND") or DEFAULT_CLAUDE_CODE_ACP_COMMAND
    raw_args = os.getenv("HERMES_CLAUDE_CODE_ACP_ARGS")
    if raw_args:
        import shlex
        args = shlex.split(raw_args)
    else:
        args = list(DEFAULT_CLAUDE_CODE_ACP_ARGS)

    async def _go():
        return await run_claude_code_acp(
            prompt=prompt,
            cwd=str(work_dir),
            permission_mode=permission_mode,
            on_chunk=cb,
            command=cmd,
            args=args,
            timeout_seconds=timeout_seconds,
        )

    try:
        # Use the existing loop if we're already inside async context (rare in
        # tool handlers, but cron/gateway dispatch can wrap us in one); else
        # asyncio.run a fresh loop.
        try:
            asyncio.get_running_loop()
            # We're inside a loop — run on a worker thread with its own loop.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(asyncio.run, _go())
                result = fut.result(timeout=timeout_seconds + 30)
        except RuntimeError:
            result = asyncio.run(_go())
    except FileNotFoundError as e:
        return tool_error(str(e))
    except asyncio.TimeoutError:
        return tool_error(f"claude_code_acp timed out after {timeout_seconds}s")
    except Exception as e:
        logger.exception("claude_code_acp failed")
        return tool_error(f"ACP run failed: {e}")

    text = result.text or ""
    if not text.strip():
        # Stop reason can hint at why nothing was emitted
        reason = result.stop_reason or "unknown"
        return tool_error(
            f"Claude Code returned no text (stop_reason={reason}, chunks={len(result.chunks)})"
        )
    return text


CLAUDE_CODE_ACP_SCHEMA: dict[str, Any] = {
    "name": "claude_code_acp",
    "description": (
        "Delegate a coding task to Claude Code via the Agent Client Protocol "
        "(@zed-industries/claude-code-acp). Spawns a fresh ACP session per call, "
        "sends one prompt, returns Claude Code's assembled answer. Use this "
        "for: write/edit code, fix bugs, run tests in a sandboxed workspace. "
        "File access is hard-clamped to `cwd`. Streams progress to the parent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Task prompt for Claude Code. Be specific: which files, what "
                    "behavior, how to verify. Reference files with relative paths "
                    "(they are resolved against `cwd`)."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Workspace directory. All file reads/writes Claude Code "
                    "performs are restricted to this subtree. Defaults to the "
                    "current process cwd. Will be created if it does not exist."
                ),
            },
            "permission_mode": {
                "type": "string",
                "enum": sorted(_ALLOWED_PERMISSION_MODES),
                "default": "auto",
                "description": (
                    "Policy for permission requests from Claude Code. "
                    "'auto' = always allow (default). "
                    "'deny-write' = allow read-only operations, reject edits/exec. "
                    "'deny-all' = reject everything (read-only inspection only). "
                    "'ask' = behaves like auto in non-interactive runs."
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "default": 600,
                "description": "Wall-clock cap on the prompt-response cycle.",
            },
        },
        "required": ["prompt"],
    },
}


registry.register(
    name="claude_code_acp",
    toolset="delegation",
    schema=CLAUDE_CODE_ACP_SCHEMA,
    handler=lambda args, **kw: claude_code_acp(
        prompt=args.get("prompt"),
        cwd=args.get("cwd"),
        permission_mode=args.get("permission_mode", "auto"),
        timeout_seconds=float(args.get("timeout_seconds") or 600.0),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=_check_requirements,
    description="Run a single Claude Code coding turn via ACP",
    emoji="🧑‍💻",
)
