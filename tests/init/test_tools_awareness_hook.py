"""Behavioral tests for the tools-awareness SessionStart hook.

The hook (maw/hooks/tools-awareness.sh) must, on EVERY maw session:
  - always emit the computer-use (visual eyes) awareness line;
  - emit the Xcode-MCP line ONLY when the repo is an iOS/Xcode env;
  - be FAIL-OPEN: exit 0 even when the embedded stack check is made to fail.

These run the hook as a real process with the exact env contract it gets
under Claude Code (CLAUDE_PLUGIN_ROOT = the plugin root). They assert
BEHAVIOR (what the hook emitted + its exit code), not importability.

Also pins the hooks.json registration + the seat-doctrine mentions, so a
future edit that drops the hook from SessionStart or strips the tool notes
from the seat commands trips a test.

Stdlib-only, hermetic (tmpdir repos, scrubbed env).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "maw"
HOOK = PLUGIN_ROOT / "hooks" / "tools-awareness.sh"


def _run_hook(cwd: Path, *, plugin_root: str | None = str(PLUGIN_ROOT)) -> subprocess.CompletedProcess[str]:
    """Run the hook as Claude Code would: `sh tools-awareness.sh` from the
    repo cwd, with CLAUDE_PLUGIN_ROOT set (or omitted to test degradation)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAW_")}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = plugin_root
    return subprocess.run(
        ["sh", str(HOOK)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class ComputerUseLineTests(unittest.TestCase):
    def test_computer_use_line_always_emitted_in_plain_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("flask\n")
            r = _run_hook(root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("computer-use", r.stdout)
        self.assertIn("non-reproducible-oracle", r.stdout)
        # Non-iOS repo: no Xcode line.
        self.assertNotIn("Xcode MCP", r.stdout)

    def test_computer_use_line_emitted_even_with_no_plugin_root(self):
        # Degrade gracefully: stack check can't run, but the eyes line ships.
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_hook(Path(tmp), plugin_root=None)
        self.assertEqual(r.returncode, 0)
        self.assertIn("computer-use", r.stdout)
        self.assertNotIn("Xcode MCP", r.stdout)


class XcodeLineTests(unittest.TestCase):
    def test_xcode_line_emitted_in_xcodeproj_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Gizmo.xcodeproj").mkdir()
            r = _run_hook(root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("computer-use", r.stdout)
        self.assertIn("Xcode MCP", r.stdout)
        self.assertIn("/mcp", r.stdout)

    def test_xcode_line_emitted_for_package_swift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text("// swift-tools-version:5.9\n")
            r = _run_hook(root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Xcode MCP", r.stdout)

    def test_no_xcode_line_in_node_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"dependencies": {"next": "14"}}))
            r = _run_hook(root)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("Xcode MCP", r.stdout)


class FailOpenTests(unittest.TestCase):
    def test_exit_zero_when_stack_check_is_broken(self):
        # Point CLAUDE_PLUGIN_ROOT at a dir with no importable init package:
        # the embedded `from init.stack_detection import ...` raises ImportError,
        # the python prints "skip", no Xcode line — and the hook still exits 0
        # AND still emits the computer-use line (the eyes are unconditional).
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_root:
            r = _run_hook(Path(tmp), plugin_root=fake_root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("computer-use", r.stdout)
        self.assertNotIn("Xcode MCP", r.stdout)

    def test_exit_zero_in_an_ios_repo_with_broken_plugin_root(self):
        # Even an iOS-shaped repo doesn't get the Xcode line if the check can't
        # load — but the hook never errors and never blocks.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_root:
            (Path(tmp) / "App.xcodeproj").mkdir()
            r = _run_hook(Path(tmp), plugin_root=fake_root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("computer-use", r.stdout)
        self.assertNotIn("Xcode MCP", r.stdout)


class RegistrationTests(unittest.TestCase):
    def test_hook_registered_under_sessionstart(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        session_start = hooks["hooks"]["SessionStart"]
        commands = [
            h["command"]
            for group in session_start
            for h in group.get("hooks", [])
        ]
        self.assertTrue(
            any("tools-awareness.sh" in c for c in commands),
            f"tools-awareness.sh not registered in SessionStart: {commands}",
        )

    def test_hook_file_exists_and_is_executable(self):
        self.assertTrue(HOOK.is_file())
        self.assertTrue(os.access(HOOK, os.X_OK), "hook is not executable")


class TimingRuleTests(unittest.TestCase):
    """Refit R2: a mid-run computer-use prompt after the operator leaves stops the seat."""

    def test_line_puts_headless_first_and_computer_use_in_first_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_hook(Path(tmp))
        self.assertEqual(r.returncode, 0)
        self.assertIn("HEADLESS", r.stdout)
        self.assertIn("FIRST turn", r.stdout)
        self.assertIn("PARKED lane", r.stdout)
        self.assertLess(r.stdout.index("HEADLESS"), r.stdout.index("**computer-use**"))

    def test_a_plain_repo_gets_no_ios_steps_and_no_operator_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}\n")
            r = _run_hook(root)
        self.assertNotIn("simctl", r.stdout)
        self.assertNotIn("XCUITest", r.stdout)
        self.assertNotRegex(r.stdout, r"\bDP\b")

    def test_the_ios_headless_steps_ride_the_xcode_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcodeproj").mkdir()
            r = _run_hook(root)
        self.assertIn("simctl io booted screenshot", r.stdout)
        self.assertIn("XCUITest", r.stdout)

    def test_both_seats_carry_the_r2_timing_rule(self):
        for seat in ("advisor", "executor"):
            text = (PLUGIN_ROOT / "commands" / f"{seat}.md").read_text()
            with self.subTest(seat):
                self.assertIn("headless first; computer-use only in your", text)
                self.assertIn("in your FIRST turn", text)
                self.assertIn("PARKED", text)


class SeatDoctrineTests(unittest.TestCase):
    def test_advisor_mentions_computer_use_and_oracle_gate(self):
        text = (PLUGIN_ROOT / "commands" / "advisor.md").read_text()
        self.assertIn("computer-use", text)
        self.assertIn("non-reproducible-oracle", text)

    def test_executor_mentions_xcode_and_computer_use(self):
        text = (PLUGIN_ROOT / "commands" / "executor.md").read_text()
        self.assertIn("Xcode MCP", text)
        self.assertIn("computer-use", text)


if __name__ == "__main__":
    unittest.main()
