"""ACP client — Hermes initiates a connection to a remote ACP agent.

Currently used by ``tools/claude_code_acp_tool.py`` to drive
``@zed-industries/claude-code-acp`` (a Claude Code ACP shim) via stdio JSON-RPC,
but written generically so any ACP-speaking subprocess can be the target.

Design choices baked in (see PR discussion):
* permission policy is per-call (``permission_mode`` arg) — A3
* response chunks stream back via ``on_chunk`` callback — B2
* fs reads/writes are clamped to the session ``cwd`` subtree — C1
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import acp
from acp.schema import (
    AgentCapabilities,
    ClientCapabilities,
    CreateTerminalResponse,
    FileSystemCapabilities,
    Implementation,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    SelectedPermissionOutcome,
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallUpdate,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

logger = logging.getLogger(__name__)


PermissionMode = str  # "auto" | "ask" | "deny-write" | "deny-all"

ChunkCallback = Callable[[str, str], Optional[Awaitable[None]]]
"""Streaming callback. Args: (kind, text). kind in {'message', 'thought', 'tool'}."""


# ---------------------------------------------------------------------------
# Path safety (C1)
# ---------------------------------------------------------------------------


class _PathOutsideSandbox(Exception):
    """Raised when a remote agent requests a path outside the session sandbox."""


def _resolve_inside(sandbox: Path, requested: str) -> Path:
    """Resolve ``requested`` and ensure it lies inside ``sandbox`` (or equals it).

    Treats absolute and relative paths the same way the agent intends:
    relative paths are joined onto sandbox; absolute paths are taken as-is
    but still verified to fall within sandbox.
    """
    sandbox_abs = sandbox.resolve()
    p = Path(requested)
    if not p.is_absolute():
        p = sandbox_abs / p
    p = p.resolve()
    try:
        p.relative_to(sandbox_abs)
    except ValueError as e:
        raise _PathOutsideSandbox(
            f"Path {requested!r} is outside the session sandbox {str(sandbox_abs)!r}"
        ) from e
    return p


# ---------------------------------------------------------------------------
# Permission policy (A3)
# ---------------------------------------------------------------------------


def _select_permission(
    options: list[PermissionOption],
    *,
    mode: PermissionMode,
    tool_kind: Optional[str],
) -> Optional[str]:
    """Return the option_id to select, or None to cancel.

    Strategy:
    * ``auto``       — pick the first non-reject option
    * ``deny-write`` — reject if tool kind looks like fs/exec mutation; else allow
    * ``deny-all``   — always reject
    * ``ask``        — currently behaves like ``auto`` (interactive prompting
                       is wired by callers that pass an explicit hook)
    """
    if not options:
        return None

    def _is_allow(opt: PermissionOption) -> bool:
        kind = (opt.kind or "").lower() if hasattr(opt, "kind") else ""
        return "reject" not in kind and "deny" not in kind and "cancel" not in kind

    def _is_reject(opt: PermissionOption) -> bool:
        kind = (opt.kind or "").lower() if hasattr(opt, "kind") else ""
        return any(k in kind for k in ("reject", "deny", "cancel"))

    if mode == "deny-all":
        for opt in options:
            if _is_reject(opt):
                return opt.option_id
        return None

    if mode == "deny-write":
        write_kinds = {"edit", "write", "execute", "delete", "move"}
        if tool_kind and tool_kind.lower() in write_kinds:
            for opt in options:
                if _is_reject(opt):
                    return opt.option_id
            return None

    # auto / ask / fallback — pick the most permissive non-reject option
    for opt in options:
        if _is_allow(opt):
            return opt.option_id
    return options[0].option_id


# ---------------------------------------------------------------------------
# Terminal management
# ---------------------------------------------------------------------------


@dataclass
class _Terminal:
    proc: asyncio.subprocess.Process
    output_buf: bytearray = field(default_factory=bytearray)
    output_byte_limit: Optional[int] = None
    truncated: bool = False
    reader_task: Optional[asyncio.Task] = None
    cwd: Optional[str] = None


# ---------------------------------------------------------------------------
# Hermes ACP client
# ---------------------------------------------------------------------------


class HermesACPClient:
    """Implements ``acp.Client`` so a remote ACP agent can call back into Hermes."""

    def __init__(
        self,
        *,
        sandbox: Path,
        permission_mode: PermissionMode = "auto",
        on_chunk: Optional[ChunkCallback] = None,
    ):
        self._sandbox = sandbox.resolve()
        self._permission_mode = permission_mode
        self._on_chunk = on_chunk
        self._terminals: dict[str, _Terminal] = {}
        self._terminal_seq = 0

    # ----- Connection lifecycle -----

    def on_connect(self, conn: "acp.Agent") -> None:  # type: ignore[name-defined]
        self._conn = conn

    # ----- File system (C1: clamped to sandbox) -----

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        try:
            resolved = _resolve_inside(self._sandbox, path)
        except _PathOutsideSandbox as e:
            raise acp.RequestError.invalid_params({"path": path, "reason": str(e)}) from e

        try:
            content = resolved.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise acp.RequestError.resource_not_found(uri=path) from e
        except OSError as e:
            raise acp.RequestError.internal_error({"path": path, "reason": str(e)}) from e

        # Apply optional line/limit slicing
        if line is not None or limit is not None:
            lines = content.splitlines(keepends=True)
            start = max((line or 1) - 1, 0)
            end = start + limit if limit is not None else len(lines)
            content = "".join(lines[start:end])

        return ReadTextFileResponse(content=content)

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **kwargs: Any,
    ) -> WriteTextFileResponse | None:
        try:
            resolved = _resolve_inside(self._sandbox, path)
        except _PathOutsideSandbox as e:
            raise acp.RequestError.invalid_params({"reason": str(e)}) from e
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return None

    # ----- Permissions (A3) -----

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        kind = getattr(tool_call, "kind", None)
        chosen = _select_permission(options, mode=self._permission_mode, tool_kind=kind)
        if chosen is None:
            return RequestPermissionResponse(
                outcome=SelectedPermissionOutcome(outcome="cancelled")
            )
        return RequestPermissionResponse(
            outcome=SelectedPermissionOutcome(outcome="selected", option_id=chosen)
        )

    # ----- Streaming updates (B2) -----

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,
    ) -> None:
        kind, text = self._render_update(update)
        if not text or self._on_chunk is None:
            return
        try:
            result = self._on_chunk(kind, text)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("on_chunk callback raised")

    @staticmethod
    def _render_update(update: Any) -> tuple[str, str]:
        """Reduce an ACP update event to (kind, text). Empty text = ignore."""
        tag = type(update).__name__

        # AgentMessageChunk / AgentThoughtChunk / UserMessageChunk all expose `content`
        content = getattr(update, "content", None)
        if content is not None:
            text = _content_to_text(content)
            if text:
                if "Thought" in tag:
                    return ("thought", text)
                if "Tool" in tag:
                    return ("tool", text)
                return ("message", text)

        # ToolCallStart/Progress
        if "ToolCall" in tag:
            title = getattr(update, "title", None) or ""
            status = getattr(update, "status", None) or ""
            label = f"[tool:{title}] {status}".strip()
            if label != "[tool:]":
                return ("tool", label)

        return ("", "")

    # ----- Terminals (let the remote agent run shell commands here) -----

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[Any] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        # Clamp cwd into sandbox if provided
        if cwd:
            try:
                resolved_cwd = _resolve_inside(self._sandbox, cwd)
            except _PathOutsideSandbox as e:
                raise acp.RequestError.invalid_params({"reason": str(e)}) from e
        else:
            resolved_cwd = self._sandbox

        env_dict = os.environ.copy()
        for ev in env or []:
            name = getattr(ev, "name", None)
            value = getattr(ev, "value", None)
            if isinstance(name, str):
                env_dict[name] = "" if value is None else str(value)

        argv = [command] + list(args or [])
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(resolved_cwd),
            env=env_dict,
        )

        self._terminal_seq += 1
        term_id = f"term-{self._terminal_seq}"
        term = _Terminal(
            proc=proc,
            output_byte_limit=output_byte_limit,
            cwd=str(resolved_cwd),
        )
        self._terminals[term_id] = term

        async def _drain() -> None:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                if (
                    term.output_byte_limit is not None
                    and len(term.output_buf) >= term.output_byte_limit
                ):
                    term.truncated = True
                    continue
                term.output_buf.extend(chunk)
                if (
                    term.output_byte_limit is not None
                    and len(term.output_buf) > term.output_byte_limit
                ):
                    overflow = len(term.output_buf) - term.output_byte_limit
                    del term.output_buf[term.output_byte_limit:]
                    term.truncated = True
                    del overflow

        term.reader_task = asyncio.create_task(_drain())
        return CreateTerminalResponse(terminal_id=term_id)

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        term = self._terminals.get(terminal_id)
        if term is None:
            raise acp.RequestError.invalid_params({"terminal_id": terminal_id, "reason": "unknown"})
        exit_status = None
        if term.proc.returncode is not None:
            exit_status = TerminalExitStatus(exit_code=term.proc.returncode)
        return TerminalOutputResponse(
            output=term.output_buf.decode("utf-8", errors="replace"),
            truncated=term.truncated,
            exit_status=exit_status,
        )

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        term = self._terminals.get(terminal_id)
        if term is None:
            raise acp.RequestError.invalid_params({"terminal_id": terminal_id, "reason": "unknown"})
        rc = await term.proc.wait()
        if term.reader_task is not None:
            try:
                await term.reader_task
            except Exception:
                logger.exception("terminal reader task failed")
        return WaitForTerminalExitResponse(exit_code=rc)

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse | None:
        term = self._terminals.get(terminal_id)
        if term is None:
            return None
        if term.proc.returncode is None:
            try:
                term.proc.kill()
            except ProcessLookupError:
                pass
        return None

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        term = self._terminals.pop(terminal_id, None)
        if term is None:
            return None
        if term.proc.returncode is None:
            try:
                term.proc.kill()
                await term.proc.wait()
            except ProcessLookupError:
                pass
        if term.reader_task is not None:
            term.reader_task.cancel()
            try:
                await term.reader_task
            except (asyncio.CancelledError, Exception):
                pass
        return None

    # ----- Extension methods (no-op default) -----

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise acp.RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content_to_text(content: Any) -> str:
    """Flatten an ACP content block (or list/string) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            t = _content_to_text(item)
            if t:
                parts.append(t)
        return "".join(parts)
    # Single content block — pydantic model
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return ""


# ---------------------------------------------------------------------------
# Top-level convenience: run a single Claude Code prompt over ACP
# ---------------------------------------------------------------------------


DEFAULT_CLAUDE_CODE_ACP_COMMAND = "npx"
DEFAULT_CLAUDE_CODE_ACP_ARGS = ["-y", "@zed-industries/claude-code-acp"]


# Env vars that signal "we are running inside a Claude Code session". Set by
# the Claude Code CLI in its own subprocesses; @zed-industries/claude-code-acp
# refuses to start when any of them are present (anti-nest guard). When Hermes
# spawns claude-code-acp as a tool, the child runs in an isolated process group
# with its own stdio — no actual nesting — so we strip these vars.
_NESTED_CLAUDE_CODE_ENV_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SSE_PORT",
)


@dataclass
class ACPRunResult:
    text: str
    stop_reason: Optional[str]
    session_id: str
    chunks: list[tuple[str, str]] = field(default_factory=list)


async def run_claude_code_acp(
    prompt: str,
    cwd: str | os.PathLike[str],
    *,
    permission_mode: PermissionMode = "auto",
    on_chunk: Optional[ChunkCallback] = None,
    command: str = DEFAULT_CLAUDE_CODE_ACP_COMMAND,
    args: Optional[list[str]] = None,
    env_overrides: Optional[dict[str, str]] = None,
    timeout_seconds: float = 600.0,
) -> ACPRunResult:
    """Spawn a Claude Code ACP shim, send one prompt, return the assembled answer.

    The default ``command``/``args`` use ``npx -y @zed-industries/claude-code-acp``
    so Hermes does not need to keep that package permanently installed.

    Args:
        prompt: The user prompt to send.
        cwd: The working directory the remote agent operates inside. Also serves
            as the fs sandbox — any read/write outside this subtree is rejected.
        permission_mode: One of ``auto``, ``deny-write``, ``deny-all``, ``ask``.
        on_chunk: Optional ``(kind, text) -> None|coro`` callback called for each
            streaming update so callers can pipe Claude Code's progress back into
            the parent Hermes turn.
        command, args: Override the default ACP server invocation.
        env_overrides: Extra env vars merged on top of the parent env.
        timeout_seconds: Wall-clock cap on the prompt-response cycle.
    """
    if shutil.which(command) is None:
        raise FileNotFoundError(
            f"ACP command not found on PATH: {command!r}. "
            "Install Node.js (>=20) so 'npx' is available, or pass command='/path/to/claude-code-acp'."
        )

    sandbox = Path(cwd).resolve()
    sandbox.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    for var in _NESTED_CLAUDE_CODE_ENV_VARS:
        env.pop(var, None)
    if env_overrides:
        env.update({str(k): str(v) for k, v in env_overrides.items()})

    captured_chunks: list[tuple[str, str]] = []

    async def _wrapped_on_chunk(kind: str, text: str) -> None:
        captured_chunks.append((kind, text))
        if on_chunk is not None:
            res = on_chunk(kind, text)
            if asyncio.iscoroutine(res):
                await res

    client = HermesACPClient(
        sandbox=sandbox,
        permission_mode=permission_mode,
        on_chunk=_wrapped_on_chunk,
    )

    actual_args = list(args if args is not None else DEFAULT_CLAUDE_CODE_ACP_ARGS)

    async def _drive() -> ACPRunResult:
        async with acp.spawn_agent_process(
            client, command, *actual_args, env=env, cwd=str(sandbox)
        ) as (conn, proc):
            init_resp = await conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_info=Implementation(
                    name="hermes-agent",
                    title="Hermes Agent",
                    version=_hermes_version(),
                ),
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(
                        read_text_file=True,
                        write_text_file=True,
                    ),
                    terminal=True,
                ),
            )
            logger.info("ACP initialize OK; agent=%s", getattr(init_resp, "agent_info", None))

            new_session = await conn.new_session(cwd=str(sandbox), mcp_servers=[])
            session_id = new_session.session_id

            try:
                prompt_resp = await conn.prompt(
                    session_id=session_id,
                    prompt=[TextContentBlock(type="text", text=prompt)],
                )
            finally:
                # Always try to close the session cleanly; don't mask the real error.
                try:
                    await conn.close_session(session_id=session_id)
                except Exception:
                    pass

            text = "".join(t for kind, t in captured_chunks if kind == "message")
            return ACPRunResult(
                text=text,
                stop_reason=getattr(prompt_resp, "stop_reason", None),
                session_id=session_id,
                chunks=list(captured_chunks),
            )

    return await asyncio.wait_for(_drive(), timeout=timeout_seconds)


def _hermes_version() -> str:
    try:
        from hermes_cli import __version__ as v
        return v
    except Exception:
        return "0.0.0"
