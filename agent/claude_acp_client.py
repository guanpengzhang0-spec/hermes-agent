"""ACP client for the Claude Code agent backend (`claude-agent-acp`).

Reuses the generic ACP plumbing from ``CopilotACPClient`` but defaults the
subprocess command to ``claude-agent-acp`` (formerly published as
``@zed-industries/claude-code-acp`` / now ``@agentclientprotocol/claude-agent-acp``).
The bridge speaks ACP over stdio and is backed by the Claude Agent SDK, which
inherits the same OAuth/API-key auth as the local ``claude`` CLI.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

from agent.copilot_acp_client import CopilotACPClient

ACP_MARKER_BASE_URL = "acp://claude-agent"
_DEFAULT_COMMAND = "claude-agent-acp"


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_CLAUDE_ACP_COMMAND", "").strip()
        or os.getenv("CLAUDE_AGENT_ACP_PATH", "").strip()
        or _DEFAULT_COMMAND
    )


def _resolve_args() -> list[str]:
    raw = os.getenv("HERMES_CLAUDE_ACP_ARGS", "").strip()
    if not raw:
        return []
    return shlex.split(raw)


class ClaudeACPClient(CopilotACPClient):
    """Thin subclass that points the generic ACP shim at ``claude-agent-acp``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **kwargs: Any,
    ):
        # Resolve our own defaults BEFORE delegating, because
        # CopilotACPClient.__init__ uses ``args or copilot_default`` — an
        # empty list would otherwise be replaced by the copilot fallback.
        resolved_command = command or _resolve_command()
        resolved_args = list(args) if args is not None else _resolve_args()
        super().__init__(
            api_key=api_key or "claude-acp",
            base_url=base_url or ACP_MARKER_BASE_URL,
            command=resolved_command,
            **kwargs,
        )
        # Force-override the parent's args resolution.
        self._acp_args = resolved_args
