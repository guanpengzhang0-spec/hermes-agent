"""Unit tests for ``orchestration.attention``.

Coverage targets:
  * Empty / all-completed item list returns ""
  * Active items render with correct status markers
  * pending + in_progress both appear; completed/cancelled filtered
  * Token budget honoured: oversize input drops items
  * in_progress items are preserved over pending when trimming
  * At least one item is always retained even if over budget
  * include_metrics emits a budget header when fields are present
  * include_metrics is a no-op when fields are absent
  * inject_attention_block appends to messages tail
  * inject_attention_block does not mutate the input list
  * inject_attention_block treats empty / whitespace block as no-op
  * render_todo_store returns None when store is empty (back-compat)
  * render_todo_store returns rendered string when store has items
  * Bad max_tokens raises ValueError
"""

from __future__ import annotations

import pytest

from orchestration.attention import (
    DEFAULT_MAX_TODO_TOKENS,
    format_attention_block,
    inject_attention_block,
    render_todo_store,
)


# ───────────────────────── format_attention_block ─────────────────────────


def test_empty_list_returns_empty_string():
    assert format_attention_block([]) == ""


def test_all_completed_returns_empty_string():
    items = [
        {"id": "1", "content": "ship it", "status": "completed"},
        {"id": "2", "content": "rolled back", "status": "cancelled"},
    ]
    assert format_attention_block(items) == ""


def test_pending_and_in_progress_render_with_markers():
    items = [
        {"id": "1", "content": "write tests", "status": "in_progress"},
        {"id": "2", "content": "deploy", "status": "pending"},
    ]
    out = format_attention_block(items)
    assert "## Active Goals & Tasks" in out
    assert "[~] 1. write tests" in out
    assert "[ ] 2. deploy" in out


def test_completed_filtered_when_mixed():
    items = [
        {"id": "1", "content": "design", "status": "completed"},
        {"id": "2", "content": "implement", "status": "in_progress"},
        {"id": "3", "content": "audit", "status": "pending"},
    ]
    out = format_attention_block(items)
    assert "design" not in out
    assert "implement" in out
    assert "audit" in out


def test_unknown_status_renders_question_marker():
    items = [{"id": "1", "content": "weird", "status": "limbo"}]
    out = format_attention_block(items)
    # Unknown statuses fall outside _ACTIVE_STATUSES → filtered out
    assert out == ""


def test_missing_fields_get_safe_defaults():
    items = [{"status": "pending"}]  # no id, no content
    out = format_attention_block(items)
    assert "?. (no description)" in out


# ───────────────────────── token budget ─────────────────────────


def test_oversize_drops_pending_first():
    items: list[dict] = [
        {"id": "1", "content": "in-progress task", "status": "in_progress"}
    ]
    items.extend(
        {"id": str(i), "content": "pending " * 50, "status": "pending"}
        for i in range(2, 30)
    )
    out = format_attention_block(items, max_tokens=80)
    assert "in-progress task" in out  # in_progress preserved
    # Most pending items should be dropped
    pending_count = sum(1 for line in out.splitlines() if "[ ]" in line)
    assert pending_count < 5


def test_at_least_one_item_retained_under_extreme_budget():
    items = [
        {"id": str(i), "content": "x" * 1000, "status": "pending"}
        for i in range(5)
    ]
    out = format_attention_block(items, max_tokens=10)
    assert out  # not empty
    # Exactly one item line in the body
    body_lines = [
        line for line in out.splitlines() if line.startswith("- ")
    ]
    assert len(body_lines) == 1


def test_within_budget_keeps_everything():
    items = [
        {"id": str(i), "content": f"task {i}", "status": "pending"}
        for i in range(5)
    ]
    out = format_attention_block(items, max_tokens=DEFAULT_MAX_TODO_TOKENS)
    for i in range(5):
        assert f"task {i}" in out


def test_in_progress_sorted_before_pending_in_output():
    items = [
        {"id": "p1", "content": "p1", "status": "pending"},
        {"id": "ip", "content": "ip", "status": "in_progress"},
        {"id": "p2", "content": "p2", "status": "pending"},
    ]
    out = format_attention_block(items)
    ip_idx = out.index("ip. ip")
    p1_idx = out.index("p1. p1")
    p2_idx = out.index("p2. p2")
    assert ip_idx < p1_idx
    assert ip_idx < p2_idx


# ───────────────────────── include_metrics ─────────────────────────


def test_include_metrics_with_cost_fields():
    items = [
        {
            "id": "1",
            "content": "do thing",
            "status": "in_progress",
            "estimated_cost_tokens": 1000,
            "actual_cost_tokens": 250,
        },
    ]
    out = format_attention_block(items, include_metrics=True)
    assert "budget≈1000" in out
    assert "spent≈250" in out


def test_include_metrics_no_op_without_cost_fields():
    items = [{"id": "1", "content": "do thing", "status": "pending"}]
    out = format_attention_block(items, include_metrics=True)
    # No metrics in header when fields are zero
    assert "budget" not in out


def test_metrics_off_by_default():
    items = [
        {
            "id": "1",
            "content": "do thing",
            "status": "in_progress",
            "estimated_cost_tokens": 1000,
        },
    ]
    out = format_attention_block(items)
    assert "budget" not in out


# ───────────────────────── inject_attention_block ─────────────────────────


def test_inject_appends_at_tail():
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]
    block = "## Active Goals & Tasks\n- [ ] 1. test"
    out = inject_attention_block(msgs, block)
    assert len(out) == 3
    assert out[-1] == {"role": "system", "content": block}
    # Original messages preserved at the front
    assert out[0] == msgs[0]
    assert out[1] == msgs[1]


def test_inject_does_not_mutate_input():
    msgs = [{"role": "user", "content": "hi"}]
    inject_attention_block(msgs, "block")
    assert len(msgs) == 1


def test_inject_empty_block_is_noop():
    msgs = [{"role": "user", "content": "hi"}]
    out = inject_attention_block(msgs, "")
    assert out == msgs
    assert out is not msgs  # new list


def test_inject_whitespace_block_is_noop():
    msgs = [{"role": "user", "content": "hi"}]
    out = inject_attention_block(msgs, "  \n  \t  ")
    assert out == msgs


def test_inject_into_empty_messages_list():
    out = inject_attention_block([], "## block")
    assert len(out) == 1
    assert out[0] == {"role": "system", "content": "## block"}


# ───────────────────────── render_todo_store adapter ─────────────────────────


class _FakeStore:
    """Minimal duck-typed TodoStore for testing the adapter."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def has_items(self) -> bool:
        return bool(self._items)

    def read(self) -> list[dict]:
        return [item.copy() for item in self._items]


def test_render_todo_store_returns_none_when_empty():
    store = _FakeStore([])
    assert render_todo_store(store) is None


def test_render_todo_store_returns_none_when_only_completed():
    store = _FakeStore(
        [{"id": "1", "content": "done", "status": "completed"}]
    )
    assert render_todo_store(store) is None


def test_render_todo_store_returns_string_when_active():
    store = _FakeStore(
        [{"id": "1", "content": "active task", "status": "in_progress"}]
    )
    out = render_todo_store(store)
    assert out is not None
    assert "active task" in out


def test_render_todo_store_honours_max_tokens():
    items = [
        {"id": str(i), "content": "filler " * 30, "status": "pending"}
        for i in range(20)
    ]
    store = _FakeStore(items)
    out = render_todo_store(store, max_tokens=60)
    assert out is not None
    # Body should be much shorter than the un-budgeted version
    body_lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(body_lines) < 20


# ───────────────────────── validation ─────────────────────────


def test_invalid_max_tokens_raises():
    with pytest.raises(ValueError):
        format_attention_block(
            [{"id": "1", "content": "x", "status": "pending"}],
            max_tokens=0,
        )


# ───────────────────────── compatibility with real TodoStore ─────────────────────────


def test_real_todo_store_compatible():
    """Smoke test: the actual ``tools.todo_tool.TodoStore`` works
    transparently with ``render_todo_store``."""
    from tools.todo_tool import TodoStore

    store = TodoStore()
    store.write(
        [
            {"id": "a", "content": "draft spec", "status": "in_progress"},
            {"id": "b", "content": "review", "status": "pending"},
            {"id": "c", "content": "ship", "status": "completed"},
        ]
    )
    out = render_todo_store(store)
    assert out is not None
    assert "draft spec" in out
    assert "review" in out
    assert "ship" not in out  # completed filtered
