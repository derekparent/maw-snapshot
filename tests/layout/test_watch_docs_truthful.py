"""Seat docs and the arming hook must not promise a session-long Monitor (#344),
and the advisor doc must cover the batch-arm branch and the blocked protocol
consistently (#357)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAW = ROOT / "maw"
SURFACES = [MAW / "commands" / "advisor.md", MAW / "commands" / "executor.md",
            MAW / "hooks" / "watchdog-arm.sh", MAW / "commands" / "bridge-watch.md"]


class MonitorLifetimeTruthTests(unittest.TestCase):
    def test_no_persistent_monitor_promise(self) -> None:
        for f in SURFACES:
            text = f.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"Monitor[^.\n]{0,30}\(persistent", f.name)
            self.assertNotIn("persistent: true", text, f.name)
            self.assertNotIn("large `timeout_ms`", text, f.name)
            self.assertNotIn("large timeout_ms", text, f.name)

    def test_hook_and_seat_docs_say_30_minutes_and_rearm(self) -> None:
        for f in (MAW / "hooks" / "watchdog-arm.sh", MAW / "commands" / "advisor.md",
                  MAW / "commands" / "executor.md"):
            text = f.read_text(encoding="utf-8")
            self.assertIn("30 minutes", text, f.name)
            self.assertRegex(text, r"(?i)re-arm", f.name)
            self.assertIn("1800000", text, f.name)

    def test_recipe_doc_and_script_exist(self) -> None:
        self.assertTrue((MAW / "docs" / "watch-recipes.md").is_file())
        self.assertTrue((MAW / "bin" / "maw-watch-inbox").is_file())
        for f in (MAW / "commands" / "advisor.md", MAW / "commands" / "executor.md"):
            self.assertIn("maw-watch-inbox", f.read_text(encoding="utf-8"), f.name)


class AdvisorBatchAndBlockedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (MAW / "commands" / "advisor.md").read_text(encoding="utf-8")

    def test_section_2e_has_batch_branch(self) -> None:
        sec = self.text.split("## 2e.", 1)[1].split("\n## 2f.", 1)[0]
        for needle in ("Batch branch", "grant-request session.arm --batch",
                       "goal-record", "/docs/cos-batch-arm.md", "--grant-file"):
            self.assertIn(needle, sec)
        self.assertRegex(sec, r"(?s)batch line.*skip steps 2 and 3")

    def test_batch_branch_says_no_goal_relay_until_armed(self) -> None:
        sec = self.text.split("**Batch branch**", 1)[1].split("**Single-seat flow.**", 1)[0]
        step3 = sec.split("\n3. ", 1)[1].split("\n4. ", 1)[0]
        self.assertIn("No goal relay until armed", step3)

    def test_recipe_doc_does_not_assert_the_background_timeout(self) -> None:
        text = (MAW / "docs" / "watch-recipes.md").read_text(encoding="utf-8")
        para = next(p for p in text.split("\n- ") if "7200000" in p)
        self.assertIn("UNVERIFIED", para)

    def test_blocked_protocol_keeps_peer_watchers_and_drops_operator_watchers(self) -> None:
        sec = self.text.split("## 4b.", 1)[1].split("\n## CLOSE", 1)[0]
        self.assertIn("no watchers while waiting", sec)
        self.assertRegex(sec, r"(?s)watcher\s+\*\*on the operator\*\*")
        self.assertIn("stay armed", sec)
        self.assertIn("minimal", sec)


if __name__ == "__main__":
    unittest.main()
