"""Unit tests for ``acp_adapter.client``.

Covers the pure-logic surface (path sandbox, permission policy, update
rendering, terminal lifecycle) without spawning a real ACP subprocess. A real
end-to-end test against ``@zed-industries/claude-code-acp`` lives at the
bottom and is gated behind ``HERMES_RUN_ACP_E2E=1``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, List

from acp_adapter.client import (
    HermesACPClient,
    _PathOutsideSandbox,
    _resolve_inside,
    _select_permission,
    run_claude_code_acp,
)


class _FakePermOption(SimpleNamespace):
    """Mimics acp.schema.PermissionOption for _select_permission tests."""


class PathSandboxTests(unittest.TestCase):
    def test_relative_path_resolves_inside_sandbox(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            (base / "sub").mkdir()
            resolved = _resolve_inside(base, "sub/file.txt")
            self.assertTrue(str(resolved).startswith(str(base.resolve())))

    def test_absolute_path_inside_sandbox_ok(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            target = (base / "ok.txt")
            resolved = _resolve_inside(base, str(target))
            self.assertEqual(resolved, target.resolve())

    def test_absolute_escape_rejected(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            with self.assertRaises(_PathOutsideSandbox):
                _resolve_inside(base, "/etc/passwd")

    def test_relative_dotdot_escape_rejected(self):
        with TemporaryDirectory() as td:
            base = Path(td) / "inner"
            base.mkdir()
            with self.assertRaises(_PathOutsideSandbox):
                _resolve_inside(base, "../../etc/passwd")


class PermissionPolicyTests(unittest.TestCase):
    def _opts(self) -> list[_FakePermOption]:
        return [
            _FakePermOption(option_id="allow_once", kind="allow_once", name="Allow"),
            _FakePermOption(option_id="reject_once", kind="reject_once", name="Reject"),
        ]

    def test_auto_picks_allow(self):
        chosen = _select_permission(self._opts(), mode="auto", tool_kind=None)
        self.assertEqual(chosen, "allow_once")

    def test_deny_all_picks_reject(self):
        chosen = _select_permission(self._opts(), mode="deny-all", tool_kind=None)
        self.assertEqual(chosen, "reject_once")

    def test_deny_write_blocks_edit_kind(self):
        chosen = _select_permission(self._opts(), mode="deny-write", tool_kind="edit")
        self.assertEqual(chosen, "reject_once")

    def test_deny_write_allows_read_kind(self):
        chosen = _select_permission(self._opts(), mode="deny-write", tool_kind="read")
        self.assertEqual(chosen, "allow_once")

    def test_empty_options_returns_none(self):
        self.assertIsNone(_select_permission([], mode="auto", tool_kind=None))


def _make_update(class_name: str, **attrs: Any) -> Any:
    """Build an instance whose ``type(...).__name__`` equals ``class_name``."""
    cls = type(class_name, (), {})
    obj = cls()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


class UpdateRenderingTests(unittest.TestCase):
    def test_message_chunk_with_text_content(self):
        update = _make_update(
            "AgentMessageChunk", content=SimpleNamespace(text="hello world")
        )
        kind, text = HermesACPClient._render_update(update)
        self.assertEqual(kind, "message")
        self.assertEqual(text, "hello world")

    def test_thought_chunk_routes_to_thought_kind(self):
        update = _make_update(
            "AgentThoughtChunk", content=SimpleNamespace(text="thinking...")
        )
        kind, _ = HermesACPClient._render_update(update)
        self.assertEqual(kind, "thought")

    def test_tool_call_progress_renders_label(self):
        update = _make_update(
            "ToolCallProgress",
            content=None,
            title="apply_patch",
            status="in_progress",
        )
        kind, text = HermesACPClient._render_update(update)
        self.assertEqual(kind, "tool")
        self.assertIn("apply_patch", text)


class FsClampingTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_outside_sandbox_raises_invalid_params(self):
        import acp

        with TemporaryDirectory() as td:
            client = HermesACPClient(sandbox=Path(td))
            with self.assertRaises(acp.RequestError) as cm:
                await client.read_text_file(
                    path="/etc/passwd", session_id="sess-1"
                )
            self.assertEqual(cm.exception.code, -32602)  # invalid_params

    async def test_write_outside_sandbox_raises_invalid_params(self):
        import acp

        with TemporaryDirectory() as td:
            client = HermesACPClient(sandbox=Path(td))
            with self.assertRaises(acp.RequestError) as cm:
                await client.write_text_file(
                    content="x", path="/tmp/escape.txt", session_id="sess-1"
                )
            self.assertEqual(cm.exception.code, -32602)

    async def test_read_inside_sandbox_returns_content(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            (base / "hello.txt").write_text("hi", encoding="utf-8")
            client = HermesACPClient(sandbox=base)
            resp = await client.read_text_file(path="hello.txt", session_id="sess-1")
            self.assertEqual(resp.content, "hi")

    async def test_write_inside_sandbox_creates_file(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            client = HermesACPClient(sandbox=base)
            await client.write_text_file(
                content="payload", path="nested/dir/out.txt", session_id="sess-1"
            )
            self.assertEqual(
                (base / "nested/dir/out.txt").read_text(encoding="utf-8"),
                "payload",
            )


class ChunkCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_chunk_called_for_message(self):
        with TemporaryDirectory() as td:
            seen: List[tuple[str, str]] = []

            def cb(kind: str, text: str) -> None:
                seen.append((kind, text))

            client = HermesACPClient(sandbox=Path(td), on_chunk=cb)

            update = _make_update(
                "AgentMessageChunk", content=SimpleNamespace(text="hi")
            )

            await client.session_update(session_id="sess-1", update=update)
            self.assertEqual(seen, [("message", "hi")])


# ---------------------------------------------------------------------------
# E2E (gated) — actually spawns @zed-industries/claude-code-acp via npx and
# asks it to write a file. Skipped unless explicitly requested AND `npx` is
# available, because it requires network + a Claude Code login.
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    os.getenv("HERMES_RUN_ACP_E2E") == "1" and shutil.which("npx"),
    "set HERMES_RUN_ACP_E2E=1 with npx available to run claude-code-acp e2e",
)
class ClaudeCodeACPSmokeE2E(unittest.TestCase):
    def test_write_hello_py(self):
        with TemporaryDirectory() as td:
            result = asyncio.run(
                run_claude_code_acp(
                    prompt=(
                        "Create a file named hello.py with exactly: "
                        'print("hello, acp")\n'
                        "Then exit. Do not add anything else."
                    ),
                    cwd=td,
                    timeout_seconds=180.0,
                )
            )
            self.assertTrue(
                (Path(td) / "hello.py").exists(),
                f"hello.py was not created. stop_reason={result.stop_reason}, "
                f"chunks={len(result.chunks)}",
            )


if __name__ == "__main__":
    unittest.main()
