"""Unit tests for ``orchestration.loop_detector``.

Coverage targets:
  * REPEAT_OP block (identical tool + args ``max_identical_calls`` times)
  * REPEAT_OP false-positive guard (same name, different args)
  * STAGNATION block (empty / failed results in a row)
  * PATTERN_REPEAT warn → halt escalation
  * PATTERN_REPEAT pattern-change clears standing warning
  * IDLE_LOOP detection
  * record_tool_result no-op when no matching pre-call exists
  * snapshot() shape
  * reset() clears state
  * constructor input validation
  * thread-safety smoke test
"""

from __future__ import annotations

import threading

import pytest

from orchestration.loop_detector import (
    LoopAction,
    LoopCheckResult,
    LoopDetector,
)


# ────────────────────── REPEAT_OP (record_tool_call) ──────────────────────


def test_distinct_calls_return_none():
    det = LoopDetector()
    assert det.record_tool_call("read_file", '{"p":"a"}').action is LoopAction.NONE
    assert det.record_tool_call("write_file", '{"p":"b"}').action is LoopAction.NONE
    assert det.record_tool_call("exec", '{"cmd":"ls"}').action is LoopAction.NONE


def test_three_identical_calls_block_on_third():
    det = LoopDetector(max_identical_calls=3)
    args = '{"path":"/tmp/x"}'
    r1 = det.record_tool_call("read_file", args)
    r2 = det.record_tool_call("read_file", args)
    r3 = det.record_tool_call("read_file", args)

    assert r1.action is LoopAction.NONE
    assert r2.action is LoopAction.NONE
    assert r3.action is LoopAction.BLOCK
    assert r3.rule_hit == "REPEAT_OP"
    assert "read_file" in r3.reason
    assert "3 times in a row" in r3.reason


def test_same_name_different_args_does_not_block():
    det = LoopDetector(max_identical_calls=3)
    assert det.record_tool_call("read_file", '{"p":"a"}').action is LoopAction.NONE
    assert det.record_tool_call("read_file", '{"p":"b"}').action is LoopAction.NONE
    # third call with different args still does not trip the rule
    r = det.record_tool_call("read_file", '{"p":"c"}')
    assert r.action is LoopAction.NONE


def test_repeat_op_threshold_is_configurable():
    det = LoopDetector(max_identical_calls=5)
    args = '{"x":1}'
    for _ in range(4):
        assert det.record_tool_call("foo", args).action is LoopAction.NONE
    assert det.record_tool_call("foo", args).action is LoopAction.BLOCK


def test_blank_name_is_ignored():
    det = LoopDetector()
    r = det.record_tool_call("", '{"x":1}')
    assert r.action is LoopAction.NONE


def test_args_hash_is_stable_across_instances():
    a = LoopDetector()
    b = LoopDetector()
    # We can't read the hash directly, but identical args from two
    # detectors must yield the same blocking behavior because the
    # pattern detection compares hashes only.
    args = '{"p":"same"}'
    for det in (a, b):
        det.record_tool_call("t", args)
        det.record_tool_call("t", args)
        assert det.record_tool_call("t", args).action is LoopAction.BLOCK


# ────────────────────── STAGNATION (record_tool_result) ──────────────────────


def test_stagnation_blocks_after_n_empty_results():
    det = LoopDetector(stagnation_run=5)
    # Use distinct args so REPEAT_OP doesn't trip first
    for i in range(4):
        det.record_tool_call("search", f'{{"q":"q{i}"}}')
        r = det.record_tool_result("search", result_length=0)
        assert r.action is LoopAction.NONE
    det.record_tool_call("search", '{"q":"q4"}')
    final = det.record_tool_result("search", result_length=0)
    assert final.action is LoopAction.BLOCK
    assert final.rule_hit == "STAGNATION"
    assert "search" in final.reason


def test_stagnation_blocks_after_n_failed_results():
    det = LoopDetector(stagnation_run=3)
    for i in range(2):
        det.record_tool_call("api_call", f'{{"i":{i}}}')
        det.record_tool_result("api_call", result_length=42, success=False)
    det.record_tool_call("api_call", '{"i":2}')
    r = det.record_tool_result("api_call", result_length=99, success=False)
    assert r.action is LoopAction.BLOCK
    assert r.rule_hit == "STAGNATION"


def test_stagnation_resets_when_successful_result_appears():
    det = LoopDetector(stagnation_run=3)
    det.record_tool_call("search", '{"q":"a"}')
    det.record_tool_result("search", result_length=0)
    det.record_tool_call("search", '{"q":"b"}')
    det.record_tool_result("search", result_length=0)
    det.record_tool_call("search", '{"q":"c"}')
    # success this time
    det.record_tool_result("search", result_length=500, success=True)
    det.record_tool_call("search", '{"q":"d"}')
    r = det.record_tool_result("search", result_length=0)
    assert r.action is LoopAction.NONE


def test_record_tool_result_without_matching_pre_call_is_noop():
    det = LoopDetector()
    r = det.record_tool_result("never_called", result_length=0)
    assert r.action is LoopAction.NONE


# ────────────────────── PATTERN_REPEAT (end_turn) ──────────────────────


def _do_turn(det: LoopDetector, tools: list[str]) -> LoopCheckResult:
    for i, name in enumerate(tools):
        det.record_tool_call(name, f'{{"call":{i}}}')
    return det.end_turn()


def test_pattern_warn_then_halt():
    det = LoopDetector(max_pattern_repeats=3)
    # Each turn uses the same tool set, just with different args so
    # REPEAT_OP doesn't fire across turns.
    assert _do_turn(det, ["read_file", "exec"]).action is LoopAction.NONE
    assert _do_turn(det, ["read_file", "exec"]).action is LoopAction.NONE
    warn = _do_turn(det, ["read_file", "exec"])
    assert warn.action is LoopAction.WARN
    assert warn.rule_hit == "PATTERN_REPEAT"
    halt = _do_turn(det, ["read_file", "exec"])
    assert halt.action is LoopAction.HALT
    assert halt.rule_hit == "PATTERN_REPEAT"


def test_pattern_change_clears_warning():
    det = LoopDetector(max_pattern_repeats=3)
    _do_turn(det, ["read_file", "exec"])
    _do_turn(det, ["read_file", "exec"])
    warn = _do_turn(det, ["read_file", "exec"])
    assert warn.action is LoopAction.WARN

    # Change pattern → warning cleared, no halt next time
    next_turn = _do_turn(det, ["write_file"])
    assert next_turn.action is LoopAction.NONE


def test_empty_turn_does_not_count_pattern():
    det = LoopDetector(max_pattern_repeats=3)
    # 3 empty turns should not trigger
    assert det.end_turn().action is LoopAction.NONE
    assert det.end_turn().action is LoopAction.NONE
    assert det.end_turn().action is LoopAction.NONE


def test_sorted_pattern_canonicalization():
    det = LoopDetector(max_pattern_repeats=3)
    # Different order of the same tool set should still be the same pattern
    _do_turn(det, ["a", "b", "c"])
    _do_turn(det, ["c", "a", "b"])
    warn = _do_turn(det, ["b", "c", "a"])
    assert warn.action is LoopAction.WARN


# ────────────────────── IDLE_LOOP ──────────────────────


def test_idle_only_warn_after_n_turns():
    det = LoopDetector(
        max_idle_only_turns=3,
        idle_only_tools={"status", "list"},
    )
    _do_turn(det, ["status"])
    _do_turn(det, ["list"])
    r = _do_turn(det, ["status", "list"])
    assert r.action is LoopAction.WARN
    assert r.rule_hit == "IDLE_LOOP"


def test_non_idle_tool_resets_idle_counter():
    det = LoopDetector(
        max_idle_only_turns=3,
        idle_only_tools={"status"},
    )
    _do_turn(det, ["status"])
    _do_turn(det, ["status"])
    _do_turn(det, ["write_file"])  # mutating tool — resets
    r = _do_turn(det, ["status"])
    assert r.action is LoopAction.NONE


# ────────────────────── snapshot / reset ──────────────────────


def test_snapshot_shape_and_keys():
    det = LoopDetector(idle_only_tools={"status"})
    det.record_tool_call("read_file", '{"p":"a"}')
    snap = det.snapshot()
    assert set(snap.keys()) == {"config", "state"}
    assert snap["config"]["max_identical_calls"] == 3
    assert snap["config"]["idle_only_tools"] == ["status"]
    assert snap["state"]["call_history_size"] == 1
    assert snap["state"]["current_turn_tools"] == ["read_file"]
    assert snap["state"]["current_turn_is_idle_only"] is False  # read_file isn't idle


def test_reset_clears_all_state():
    det = LoopDetector(max_identical_calls=3)
    args = '{"p":"a"}'
    for _ in range(3):
        det.record_tool_call("read_file", args)
    snap_before = det.snapshot()
    assert snap_before["state"]["call_history_size"] == 3
    assert snap_before["state"]["last_block_reason"] is not None

    det.reset()
    snap_after = det.snapshot()
    assert snap_after["state"]["call_history_size"] == 0
    assert snap_after["state"]["last_block_reason"] is None
    assert snap_after["state"]["current_turn_tools"] == []


# ────────────────────── input validation ──────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_identical_calls": 1},
        {"max_pattern_repeats": 1},
        {"max_idle_only_turns": 0},
        {"window_size": 2, "max_identical_calls": 5},  # window < threshold
        {"stagnation_run": 1},
    ],
)
def test_invalid_config_raises(kwargs):
    with pytest.raises(ValueError):
        LoopDetector(**kwargs)


# ────────────────────── thread safety ──────────────────────


def test_concurrent_record_calls_do_not_crash():
    det = LoopDetector(max_identical_calls=3)
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            for i in range(50):
                det.record_tool_call(f"tool_{idx}", f'{{"i":{i}}}')
                det.record_tool_result(f"tool_{idx}", result_length=i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    snap = det.snapshot()
    # 8 workers × 50 calls = 400, but the deque is bounded
    assert snap["state"]["call_history_size"] > 0
