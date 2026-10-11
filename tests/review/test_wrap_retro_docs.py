"""Doc-truth: both seats' CLOSE rituals carry the wrap-retro block (#193)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

COMMANDS = Path(__file__).resolve().parents[2] / "maw" / "commands"


class WrapRetroDocsTest(unittest.TestCase):
    def test_both_close_rituals_carry_the_three_line_retro(self) -> None:
        for name in ("advisor.md", "executor.md"):
            text = (COMMANDS / name).read_text(encoding="utf-8")
            close = text[text.index("## CLOSE ritual"):]
            with self.subTest(name):
                self.assertIn("Wrap-retro (#193)", close)
                for label in ("what slowed us", "what the harness should change",
                              "one lesson"):
                    self.assertIn(label, close)
                self.assertIn("opconf lessons_file", close)
                self.assertIn("MAW_LESSONS_FILE", close)
                self.assertIn("skipped: lessons_file not configured", close)
                self.assertNotIn("OPEN", close)
                self.assertNotIn("- [ ]", close)
                self.assertRegex(close, r"<YYYY-MM-DD> \(<seat>\) slowed: ")
                # numbered list stays one run: no item restarts at 1 after the retro
                items = [int(m) for m in re.findall(r"(?m)^(\d+)\. ", close.split("## Standing down")[0])]
                self.assertEqual(items, list(range(1, len(items) + 1)))


if __name__ == "__main__":
    unittest.main()
