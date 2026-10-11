"""Behavioral tests for orchestrate.merge_echo — the bounded-merge gate artifact.

The contract under test is the LITERAL ORDERED FORMAT the operator's Y is taken against:
numbered, in the given order, '#<n> — <title>', closed by the fixed confirm
line. Titles come through an injected runner (no live gh).
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from orchestrate import merge_echo  # noqa: E402


def _titled_runner(titles: dict[int, str]):
    def runner(cmd):
        # gh pr view <n> --json title
        n = int(cmd[cmd.index("view") + 1])
        if n not in titles:
            return subprocess.CompletedProcess(cmd, 1, "", "no such PR")
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"title": titles[n]}), ""
        )

    return runner


class MergeEchoTest(unittest.TestCase):
    def test_literal_ordered_format(self) -> None:
        runner = _titled_runner({29: "feat: wire receipt", 30: "feat: review helpers"})
        out = merge_echo.merge_echo([29, 30], runner=runner)
        expected = (
            "1. #29 — feat: wire receipt\n"
            "2. #30 — feat: review helpers\n"
            "Confirm Y to merge in this exact order"
        )
        self.assertEqual(out, expected)

    def test_order_is_preserved_verbatim(self) -> None:
        # Given out-of-order numbers, the echo must NOT sort — the operator confirms the
        # exact order the advisor vetted.
        runner = _titled_runner({1: "one", 2: "two", 3: "three"})
        out = merge_echo.merge_echo([3, 1, 2], runner=runner)
        lines = out.splitlines()
        self.assertEqual(lines[0], "1. #3 — three")
        self.assertEqual(lines[1], "2. #1 — one")
        self.assertEqual(lines[2], "3. #2 — two")
        self.assertEqual(lines[3], merge_echo.CONFIRM_LINE)

    def test_empty_plan_is_explicit_not_blank(self) -> None:
        out = merge_echo.merge_echo([], runner=_titled_runner({}))
        self.assertNotEqual(out.strip(), "")
        self.assertNotIn(merge_echo.CONFIRM_LINE, out)
        self.assertIn("nothing to confirm", out)

    def test_single_pr(self) -> None:
        out = merge_echo.merge_echo([42], runner=_titled_runner({42: "fix: thing"}))
        self.assertEqual(
            out, "1. #42 — fix: thing\nConfirm Y to merge in this exact order"
        )

    def test_unavailable_title_is_loud_placeholder(self) -> None:
        # A gh failure must surface a LOUD placeholder, never a blank or a guess —
        # The operator's Y can't land against a PR he can't see.
        out = merge_echo.merge_echo([99], runner=_titled_runner({}))
        self.assertIn("#99", out)
        self.assertIn("title unavailable", out)

    def test_confirm_line_is_last(self) -> None:
        runner = _titled_runner({5: "a", 6: "b"})
        out = merge_echo.merge_echo([5, 6], runner=runner)
        self.assertTrue(out.endswith(merge_echo.CONFIRM_LINE))

    def test_pr_title_parses_gh_json(self) -> None:
        runner = _titled_runner({7: "feat: parse me"})
        self.assertEqual(merge_echo.pr_title(7, runner=runner), "feat: parse me")


if __name__ == "__main__":
    unittest.main()
