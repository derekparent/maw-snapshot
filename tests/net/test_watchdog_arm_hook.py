"""Behavioral tests for the supervision-watchdog arming hook (#149/F10).

maw/hooks/watchdog-arm.sh must, on a maw session start:
  - inject the pair-sweep watchdog arming line for a PAIRED seat (advisor/
    executor, including generational names like advisor2/executor2);
  - emit NOTHING for a non-paired session (cos, a bare session, no role);
  - be FAIL-OPEN: exit 0 in every case.
Also pins the hooks.json registration + the seat-doctrine wiring (the pair-sweep
watch + watcher-armed report line in both seat commands), so a future edit that
drops the arming trips a test.

Runs the hook as a real process with the exact env contract it gets under Claude
Code. Stdlib-only, hermetic (scrubbed env).
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "maw"
HOOK = PLUGIN_ROOT / "hooks" / "watchdog-arm.sh"
_LINE_MARK = "[maw-watchdog]"


def _run_hook(role: str | None) -> subprocess.CompletedProcess[str]:
    """Run the hook with a scrubbed env, setting MAW_BRIDGE_ROLE to `role`
    (omitted when None — the no-role case)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAW_")}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    if role is not None:
        env["MAW_BRIDGE_ROLE"] = role
    return subprocess.run(
        ["sh", str(HOOK)], env=env, capture_output=True, text=True, timeout=30
    )


class ArmingLineTests(unittest.TestCase):
    def test_executor_gets_arming_line(self):
        r = _run_hook("executor")
        self.assertEqual(r.returncode, 0)
        self.assertIn(_LINE_MARK, r.stdout)
        self.assertIn("net.watch --for executor", r.stdout)
        self.assertIn("watcher-armed: yes", r.stdout)

    def test_advisor_gets_arming_line(self):
        r = _run_hook("advisor")
        self.assertEqual(r.returncode, 0)
        self.assertIn(_LINE_MARK, r.stdout)
        self.assertIn("net.watch --for advisor", r.stdout)

    def test_generational_name_still_paired(self):
        # advisor2 / executor2 are the same role after a rotation.
        r = _run_hook("executor2")
        self.assertEqual(r.returncode, 0)
        self.assertIn(_LINE_MARK, r.stdout)
        self.assertIn("net.watch --for executor2", r.stdout)

    def test_non_paired_role_gets_nothing(self):
        r = _run_hook("cos")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_no_role_gets_nothing(self):
        r = _run_hook(None)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_wake_handler_doctrine_present(self):
        # The auto-ack economics must be in the injected line (read the pane,
        # auto-ack an active peer, escalate only a wedged one).
        r = _run_hook("advisor")
        self.assertIn("AUTO-ACK", r.stdout)
        self.assertIn("pane", r.stdout)


class RegistrationTests(unittest.TestCase):
    def test_hook_registered_under_sessionstart(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        commands = [
            h["command"]
            for group in hooks["hooks"]["SessionStart"]
            for h in group.get("hooks", [])
        ]
        self.assertTrue(
            any("watchdog-arm.sh" in c for c in commands),
            f"watchdog-arm.sh not registered in SessionStart: {commands}",
        )

    def test_hook_file_exists_and_is_executable(self):
        self.assertTrue(HOOK.is_file())
        self.assertTrue(os.access(HOOK, os.X_OK), "hook is not executable")


class SeatDoctrineTests(unittest.TestCase):
    def test_executor_documents_pair_sweep_and_report_line(self):
        text = (PLUGIN_ROOT / "commands" / "executor.md").read_text()
        self.assertIn("net.watch --for", text)
        self.assertIn("watcher-armed: yes", text)
        self.assertIn("auto-ack", text.lower())

    def test_advisor_documents_pair_sweep_and_report_line(self):
        text = (PLUGIN_ROOT / "commands" / "advisor.md").read_text()
        self.assertIn("net.watch --for", text)
        self.assertIn("watcher-armed: yes", text)
        self.assertIn("auto-ack", text.lower())


if __name__ == "__main__":
    unittest.main()
