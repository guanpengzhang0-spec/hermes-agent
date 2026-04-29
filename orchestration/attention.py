"""Manus-style ``Active Goals & Tasks`` block injection.

Inspired by automaton's ``src/orchestration/attention.ts``: a small,
hard-budgeted markdown block appended to the message list every turn so
that long contexts cannot drift away from the agent's standing goals.

Design points specific to hermes:

  * **Source-agnostic** — accepts any ``Sequence[Mapping]`` of items
    with ``{id, content, status}`` keys. Compatible with the existing
    ``tools.todo_tool.TodoStore.read()`` output AND the future
    ``orchestration.task_graph.TaskGraph.to_attention_format(...)``
    (module 6).
  * **In-progress is sacred** — when over the token budget, items are
    dropped from the *tail* but ``in_progress`` items are kept first,
    on the principle that they reflect work the agent is actively
    holding state for.
  * **Empty-safe** — when there are no active items, returns ``""``
    so the caller can skip injection entirely (no noisy empty header).
  * **Non-destructive injection** — ``inject_attention_block`` returns
    a new ``list``; the caller's ``messages`` is never mutated.
  * **No I/O, no global state** — pure functions; trivially testable.

The module deliberately does NOT replace ``TodoStore.format_for_injection``
in-place. The runtime wiring point in ``run_agent.py`` is the caller —
flipping the call site over to ``render_todo_store(self._todo_store)``
gives the same string in the empty case (now ``""`` instead of
``None``) and a token-bounded one in the populated case.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

# Default knobs — match automaton (2000 tokens, 4 chars/token estimate).
DEFAULT_MAX_TODO_TOKENS: int = 2000
CHARS_PER_TOKEN_ESTIMATE: int = 4

# Status markers — kept compatible with existing TodoStore output so
# that downstream tooling (CLI display, /todos command) is unaffected.
_STATUS_MARKER: dict[str, str] = {
    "in_progress": "[~]",   # TodoStore semantics — actively held
    "running": "[*]",       # TaskGraph semantics — executor claimed it
    "ready": "[>]",         # TaskGraph — deps satisfied, waiting
    "pending": "[ ]",
    "blocked": "[!]",       # TaskGraph — dep failed
    "completed": "[x]",
    "cancelled": "[-]",
    "failed": "[X]",
}

# Statuses that contribute to the visible block. Terminal / blocked
# items are filtered out — re-injecting completed work makes the model
# re-do it; surfacing blocked items adds noise without action.
_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"pending", "ready", "in_progress", "running"}
)

# Header line. Stable string — search-friendly in logs.
_HEADER_TITLE: str = "Active Goals & Tasks"
_HEADER_HINT: str = (
    "Your standing task list — re-injected every turn. "
    "Update via the `todo` tool when status changes."
)


# ─────────────────────────── token budgeting ───────────────────────────


def _estimate_tokens(text: str) -> int:
    """Cheap char-based token estimate. Matches automaton's heuristic.

    Slightly over-counts ASCII (which averages ~4.5 chars/token) and
    under-counts CJK (which averages ~2 chars/token). Good enough for
    a soft bound on injection size.
    """
    return -(-len(text) // CHARS_PER_TOKEN_ESTIMATE)


# ─────────────────────────── rendering ───────────────────────────


def _format_item(item: Mapping[str, Any]) -> str:
    """Render a single item line. Robust to missing/extra fields."""
    raw_id = str(item.get("id", "?")).strip() or "?"
    content = str(item.get("content", "")).strip() or "(no description)"
    status = str(item.get("status", "pending")).strip().lower()
    marker = _STATUS_MARKER.get(status, "[?]")
    return f"- {marker} {raw_id}. {content}"


def _render_block(
    items: Sequence[Mapping[str, Any]],
    *,
    include_metrics: bool,
) -> str:
    """Render the full block from a (possibly empty) item list.

    ``include_metrics`` is reserved for the TaskGraph integration in
    module 6 — when the items carry ``estimated_cost_tokens`` /
    ``actual_cost_tokens`` fields, the header line will include a
    ``[$budget / $spent]`` summary. For module 4 it is a no-op.
    """
    if not items:
        return ""

    header = f"## {_HEADER_TITLE}"
    if include_metrics:
        est = sum(int(it.get("estimated_cost_tokens", 0) or 0) for it in items)
        act = sum(int(it.get("actual_cost_tokens", 0) or 0) for it in items)
        if est or act:
            header = f"{header} [budget≈{est} tok, spent≈{act} tok]"

    lines = [header, _HEADER_HINT]
    lines.extend(_format_item(it) for it in items)
    return "\n".join(lines)


def _trim_to_budget(
    active: list[Mapping[str, Any]],
    max_tokens: int,
    *,
    include_metrics: bool,
) -> list[Mapping[str, Any]]:
    """Drop items from the tail until under budget.

    ``in_progress`` items are sorted to the front so that when we drop
    from the tail we remove ``pending`` first. If only ``in_progress``
    items remain and we are still over budget, we keep cropping from
    the tail — at minimum one item is retained.
    """
    if not active:
        return active

    sorted_active = sorted(
        active,
        key=lambda it: 0 if str(it.get("status", "")).lower() == "in_progress" else 1,
    )

    kept = list(sorted_active)
    while True:
        rendered = _render_block(kept, include_metrics=include_metrics)
        if _estimate_tokens(rendered) <= max_tokens:
            return kept
        if len(kept) <= 1:
            # We've already cropped to one item but still over budget
            # (e.g. a giant single content). Caller gets the smallest
            # representable block and a callsite-side truncation is
            # the right next step — don't return an empty list.
            return kept
        kept.pop()


# ─────────────────────────── public API ───────────────────────────


def format_attention_block(
    items: Sequence[Mapping[str, Any]],
    *,
    max_tokens: int = DEFAULT_MAX_TODO_TOKENS,
    include_metrics: bool = False,
) -> str:
    """Render an Active Goals & Tasks block from raw item dicts.

    Returns ``""`` (empty string, *not* ``None``) when there are no
    active items, so that ``inject_attention_block`` becomes a no-op.

    Args:
      items: Sequence of mappings carrying at least ``id``, ``content``,
        ``status``. Extra fields (``estimated_cost_tokens`` / etc.) are
        consumed when ``include_metrics=True``.
      max_tokens: Hard upper bound on the block size in estimated
        tokens. Defaults to ``DEFAULT_MAX_TODO_TOKENS``.
      include_metrics: If True, render budget/spent in the header.
        Reserved for TaskGraph integration; off by default.
    """
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")

    active = [
        it
        for it in items
        if str(it.get("status", "pending")).strip().lower() in _ACTIVE_STATUSES
    ]
    if not active:
        return ""

    kept = _trim_to_budget(active, max_tokens, include_metrics=include_metrics)
    return _render_block(kept, include_metrics=include_metrics)


def inject_attention_block(
    messages: list[dict[str, Any]],
    block: str,
) -> list[dict[str, Any]]:
    """Append the attention block as a system message at the tail.

    Returns a NEW list — the input is never mutated. Empty / whitespace
    block is a no-op (caller's list is returned as a shallow copy so
    callers can rely on identity semantics).

    Putting the block at the *end* (after history, after the latest
    user message) is the automaton convention: anything closer to the
    inference call has stronger pull on attention, so this is where
    standing goals belong.
    """
    if not block or not block.strip():
        return list(messages)

    return [*messages, {"role": "system", "content": block}]


# ─────────────────────────── convenience adapters ───────────────────────────


def render_todo_store(
    store: Any,
    *,
    max_tokens: int = DEFAULT_MAX_TODO_TOKENS,
    include_metrics: bool = False,
) -> Optional[str]:
    """Adapter for the existing ``tools.todo_tool.TodoStore``.

    Returns ``None`` (mirror of TodoStore.format_for_injection's
    historical contract) when there is nothing to inject — letting
    callers replace ``self._todo_store.format_for_injection()`` with
    ``render_todo_store(self._todo_store)`` line-for-line.

    Returns the rendered string when the store has active items.
    """
    has_items = getattr(store, "has_items", lambda: False)()
    if not has_items:
        return None

    items = list(store.read())
    rendered = format_attention_block(
        items,
        max_tokens=max_tokens,
        include_metrics=include_metrics,
    )
    return rendered or None
