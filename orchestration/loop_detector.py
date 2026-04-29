"""In-process loop / stuck-pattern / idle detector for the agent main loop.

Borrowed in spirit from automaton's ``src/agent/loop-detector.ts`` and
fused with the existing hermes spec at
``~/.hermes/skills/self-evolution/loop-guard/scripts/detect_loop.py``.

This module is **stateful** and **single-instance per session** (one per
``AIAgent``). It is **thread-safe** so that hermes' concurrent tool-call
path (``_execute_tool_calls_concurrent``) cannot race state updates.

It deliberately has **zero external dependencies** beyond the standard
library so that it can be unit-tested in isolation and reused across
projects. The caller is responsible for emitting events (e.g. into the
EventStream of module 3).

Usage sketch (the real wiring lives in ``run_agent.py``)::

    detector = LoopDetector()                    # session start

    for tool in pending_tool_calls:              # inside _execute_tool_calls
        decision = detector.record_tool_call(tool.name, tool.raw_args)
        if decision.action is LoopAction.BLOCK:
            inject_system_warning(decision.reason)
            continue                              # skip this tool
        result = run_tool(tool)
        detector.record_tool_result(
            tool.name,
            result_length=len(result.content or ""),
            success=result.ok,
        )

    end_decision = detector.end_turn()           # after the turn loop
    if end_decision.action in (LoopAction.WARN, LoopAction.HALT):
        inject_system_warning(end_decision.reason)
        if end_decision.action is LoopAction.HALT:
            stop_conversation()
"""

from __future__ import annotations

import hashlib
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ────────────────────────────── Public types ──────────────────────────────


class LoopAction(str, Enum):
    """Severity of a loop-detector decision.

    Ordered from least to most disruptive. The caller decides how to act:
      - ``NONE``  : no signal, proceed
      - ``WARN``  : inject a system message but allow execution to continue
      - ``BLOCK`` : skip the *current* tool call, inject reason
      - ``HALT``  : terminate the entire turn (caller should write task_done)
    """

    NONE = "none"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass(frozen=True)
class LoopCheckResult:
    """Outcome of a loop-detector probe.

    ``rule_hit`` is one of:
      - ``"REPEAT_OP"``     : same tool+args fired N times in a row
      - ``"STAGNATION"``    : N consecutive empty/failed tool results
      - ``"PATTERN_REPEAT"``: identical sorted-tool-set across N turns
      - ``"IDLE_LOOP"``     : N consecutive turns of idle-only tools
      - ``None``            : when ``action`` is ``LoopAction.NONE``
    """

    action: LoopAction
    reason: str
    rule_hit: Optional[str] = None


# ────────────────────────────── Internal state ──────────────────────────────


@dataclass
class _ToolCallRecord:
    """A single recorded tool invocation (window-bounded)."""

    name: str
    args_hash: str
    result_length: int = -1   # -1 means "not yet observed"
    success: bool = True


# ────────────────────────────── Detector ──────────────────────────────


class LoopDetector:
    """Detect three classes of agent pathologies in real time.

    Configured thresholds are chosen to match both automaton's defaults
    (``maxIdenticalCalls=3``) and hermes' loop-guard spec
    (``STAGNATION_THRESHOLD=5``). All thresholds are constructor-injected
    so callers can tune per workload.

    Thread-safety: every public method acquires an internal RLock, so
    concurrent tool calls from ``_execute_tool_calls_concurrent`` are
    serialized at the detector boundary. The lock is reentrant to allow
    snapshot calls from inside hooks.
    """

    DEFAULT_MAX_IDENTICAL_CALLS = 3
    DEFAULT_MAX_PATTERN_REPEATS = 3
    DEFAULT_MAX_IDLE_ONLY_TURNS = 3
    DEFAULT_WINDOW_SIZE = 10
    DEFAULT_STAGNATION_RUN = 5

    def __init__(
        self,
        max_identical_calls: int = DEFAULT_MAX_IDENTICAL_CALLS,
        max_pattern_repeats: int = DEFAULT_MAX_PATTERN_REPEATS,
        max_idle_only_turns: int = DEFAULT_MAX_IDLE_ONLY_TURNS,
        window_size: int = DEFAULT_WINDOW_SIZE,
        stagnation_run: int = DEFAULT_STAGNATION_RUN,
        idle_only_tools: Optional[set[str]] = None,
    ) -> None:
        if max_identical_calls < 2:
            raise ValueError("max_identical_calls must be >= 2")
        if max_pattern_repeats < 2:
            raise ValueError("max_pattern_repeats must be >= 2")
        if max_idle_only_turns < 1:
            raise ValueError("max_idle_only_turns must be >= 1")
        if window_size < max_identical_calls:
            raise ValueError("window_size must be >= max_identical_calls")
        if stagnation_run < 2:
            raise ValueError("stagnation_run must be >= 2")

        self._max_identical_calls = max_identical_calls
        self._max_pattern_repeats = max_pattern_repeats
        self._max_idle_only_turns = max_idle_only_turns
        self._window_size = window_size
        self._stagnation_run = stagnation_run
        self._idle_only_tools: frozenset[str] = frozenset(idle_only_tools or ())

        # Mutable state guarded by _lock
        self._lock = threading.RLock()
        self._call_history: deque[_ToolCallRecord] = deque(maxlen=window_size * 4)
        self._turn_patterns: deque[str] = deque(maxlen=window_size)
        self._current_turn_tools: list[str] = []
        self._current_turn_is_idle_only: bool = True
        self._consecutive_idle_only_turns: int = 0
        self._pattern_warning_issued: Optional[str] = None
        self._last_block_reason: Optional[str] = None

    # ─────────────────────────── public API ───────────────────────────

    def record_tool_call(self, name: str, args: str) -> LoopCheckResult:
        """Probe before executing a tool.

        Returns ``BLOCK`` when the same ``(name, args_hash)`` has been
        observed ``max_identical_calls`` times **in a row** at the tail
        of the window. The caller MUST skip the tool execution and
        inject ``result.reason`` as a system message.

        Always records the call (even on BLOCK) so that future calls
        observe the full pattern. The block decision is idempotent for
        the same (name, args) pair — repeated probes will keep blocking
        until the pattern is interrupted.
        """
        if not isinstance(name, str) or not name:
            return LoopCheckResult(LoopAction.NONE, "", None)

        args_hash = self._stable_hash(args or "")
        record = _ToolCallRecord(name=name, args_hash=args_hash)

        with self._lock:
            self._call_history.append(record)
            self._current_turn_tools.append(name)
            if name not in self._idle_only_tools:
                self._current_turn_is_idle_only = False

            threshold = self._max_identical_calls
            if len(self._call_history) >= threshold:
                tail = list(self._call_history)[-threshold:]
                if all(
                    rec.name == name and rec.args_hash == args_hash
                    for rec in tail
                ):
                    reason = (
                        f'You have called "{name}" with identical arguments '
                        f"{threshold} times in a row. This is a loop. You MUST "
                        "try a different approach, use a different tool, or "
                        "report failure to the user."
                    )
                    self._last_block_reason = reason
                    return LoopCheckResult(LoopAction.BLOCK, reason, "REPEAT_OP")

        return LoopCheckResult(LoopAction.NONE, "", None)

    def record_tool_result(
        self,
        name: str,
        *,
        result_length: int,
        success: bool = True,
    ) -> LoopCheckResult:
        """Update the detector with the outcome of the most recent call.

        Returns ``BLOCK`` when the most recent ``stagnation_run`` calls
        with the same name all yielded ``result_length == 0`` or
        ``success == False`` — that is the "stuck on empty results"
        signal from the loop-guard spec.

        The most recent record matching ``name`` (the one created by the
        prior ``record_tool_call``) is updated in place. If no matching
        record exists (caller forgot pre-call), this is a no-op.
        """
        with self._lock:
            target: Optional[_ToolCallRecord] = None
            for rec in reversed(self._call_history):
                if rec.name == name and rec.result_length == -1:
                    target = rec
                    break
            if target is None:
                return LoopCheckResult(LoopAction.NONE, "", None)

            target.result_length = max(0, int(result_length))
            target.success = bool(success)

            same_name = [
                rec for rec in self._call_history if rec.name == name
            ]
            if len(same_name) >= self._stagnation_run:
                tail = same_name[-self._stagnation_run :]
                if all(
                    (rec.result_length == 0 or not rec.success)
                    for rec in tail
                ):
                    reason = (
                        f'Tool "{name}" has produced empty/failed results '
                        f"for {self._stagnation_run} consecutive calls. "
                        "Stop calling it and try a different approach."
                    )
                    self._last_block_reason = reason
                    return LoopCheckResult(
                        LoopAction.BLOCK, reason, "STAGNATION"
                    )

        return LoopCheckResult(LoopAction.NONE, "", None)

    def end_turn(self) -> LoopCheckResult:
        """Probe after a full agent turn (all tool calls executed).

        Detection rules (in priority order):
          1. Same sorted tool-set seen ``max_pattern_repeats`` turns in
             a row → first occurrence ``WARN``, second ``HALT``
          2. ``max_idle_only_turns`` consecutive turns containing only
             idle-only tools → ``WARN``

        Always resets per-turn accumulators (``current_turn_tools``,
        ``current_turn_is_idle_only``).
        """
        with self._lock:
            pattern = ",".join(sorted(self._current_turn_tools))
            had_tools = len(self._current_turn_tools) > 0
            was_idle_only = self._current_turn_is_idle_only

            # Reset per-turn state up front so any early return path is clean
            self._current_turn_tools = []
            self._current_turn_is_idle_only = True

            # --- pattern repeat detection ---
            if had_tools:
                self._turn_patterns.append(pattern)
                if len(self._turn_patterns) >= self._max_pattern_repeats:
                    last_n = list(self._turn_patterns)[
                        -self._max_pattern_repeats :
                    ]
                    if all(p == pattern for p in last_n):
                        if self._pattern_warning_issued == pattern:
                            # Already warned, escalate
                            self._pattern_warning_issued = None
                            self._turn_patterns.clear()
                            self._consecutive_idle_only_turns = 0
                            reason = (
                                f"LOOP ENFORCEMENT: you were warned about the "
                                f'tool pattern "{pattern}" but kept repeating '
                                "it. Stop. Try a completely different approach "
                                "or report failure to the user."
                            )
                            return LoopCheckResult(
                                LoopAction.HALT, reason, "PATTERN_REPEAT"
                            )

                        # First-time warning
                        self._pattern_warning_issued = pattern
                        reason = (
                            f'WARNING: you have used the tool pattern "{pattern}" '
                            f"for {self._max_pattern_repeats} consecutive turns. "
                            "On the next turn you MUST take a different approach. "
                            "If you cannot make progress, report failure."
                        )
                        return LoopCheckResult(
                            LoopAction.WARN, reason, "PATTERN_REPEAT"
                        )

                # Pattern changed → clear the standing warning
                if (
                    self._pattern_warning_issued is not None
                    and pattern != self._pattern_warning_issued
                ):
                    self._pattern_warning_issued = None

            # --- idle-only run detection ---
            if had_tools and was_idle_only:
                self._consecutive_idle_only_turns += 1
            else:
                self._consecutive_idle_only_turns = 0

            if self._consecutive_idle_only_turns >= self._max_idle_only_turns:
                self._consecutive_idle_only_turns = 0
                reason = (
                    f"IDLE LOOP DETECTED: the last "
                    f"{self._max_idle_only_turns} turns only used "
                    "status-check tools. You already know your status. "
                    "Take a CONCRETE action: write code, run a command, or "
                    "report completion."
                )
                return LoopCheckResult(LoopAction.WARN, reason, "IDLE_LOOP")

        return LoopCheckResult(LoopAction.NONE, "", None)

    def reset(self) -> None:
        """Clear all state. Call on session start or /reset."""
        with self._lock:
            self._call_history.clear()
            self._turn_patterns.clear()
            self._current_turn_tools = []
            self._current_turn_is_idle_only = True
            self._consecutive_idle_only_turns = 0
            self._pattern_warning_issued = None
            self._last_block_reason = None

    def snapshot(self) -> dict:
        """Export current state as a JSON-serializable dict.

        Intended consumer: EventStream (module 3). Format is stable and
        considered part of the public API — adding fields is OK,
        removing fields is a breaking change.
        """
        with self._lock:
            return {
                "config": {
                    "max_identical_calls": self._max_identical_calls,
                    "max_pattern_repeats": self._max_pattern_repeats,
                    "max_idle_only_turns": self._max_idle_only_turns,
                    "window_size": self._window_size,
                    "stagnation_run": self._stagnation_run,
                    "idle_only_tools": sorted(self._idle_only_tools),
                },
                "state": {
                    "call_history_size": len(self._call_history),
                    "turn_patterns_size": len(self._turn_patterns),
                    "current_turn_tools": list(self._current_turn_tools),
                    "current_turn_is_idle_only": self._current_turn_is_idle_only,
                    "consecutive_idle_only_turns": self._consecutive_idle_only_turns,
                    "pattern_warning_issued": self._pattern_warning_issued,
                    "last_block_reason": self._last_block_reason,
                },
            }

    # ─────────────────────────── helpers ───────────────────────────

    @staticmethod
    def _stable_hash(value: str) -> str:
        """Cross-process-stable hash, prefix of blake2b 16-byte digest."""
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()
        return digest
