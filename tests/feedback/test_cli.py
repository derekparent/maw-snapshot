"""Tests for feedback.cli — capture drop-box create/list/archive."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from feedback.cli import REVIEWED_DIRNAME, feedback_dir, main  # noqa: E402


class FeedbackCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "feedback"
        self._saved = os.environ.get("MAW_FEEDBACK_DIR")
        os.environ["MAW_FEEDBACK_DIR"] = str(self.dir)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("MAW_FEEDBACK_DIR", None)
        else:
            os.environ["MAW_FEEDBACK_DIR"] = self._saved
        self.tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_env_override_wins(self) -> None:
        self.assertEqual(feedback_dir(), self.dir)

    def test_new_creates_file_with_frontmatter(self) -> None:
        rc, out, _ = self.run_cli(
            ["new", "--repo", "widget", "--seat", "executor", "--arc", "phase 0d close"]
        )
        self.assertEqual(rc, 0)
        path = Path(out.strip())
        self.assertTrue(path.is_file())
        today = datetime.now(timezone.utc).date().isoformat()  # UTC, matching #148 fix
        self.assertEqual(path.name, f"{today}-widget-executor.md")
        body = path.read_text()
        self.assertIn("repo: widget", body)
        self.assertIn("seat: executor", body)
        self.assertIn(f"date: {today}", body)
        self.assertIn("arc: phase 0d close", body)
        self.assertIn("## What worked", body)
        self.assertIn("## Friction", body)
        self.assertIn("## Wish I had", body)

    def test_new_stamps_utc_date_not_local(self) -> None:
        # #148: 01:15Z on 2026-07-19 — under a UTC-7 localtime date.today() stamped
        # 07-18 (colliding with the prior run, breaking newest-first sort).
        from feedback.cli import cmd_new
        rc = cmd_new("maw", "advisor", "arc",
                     now=datetime(2026, 7, 19, 1, 15, tzinfo=timezone.utc))
        self.assertEqual(rc, 0)
        names = [f.name for f in self.dir.glob("*.md")]
        self.assertTrue(any(n.startswith("2026-07-19-") for n in names), names)

    def test_new_converts_local_offset_to_utc_date(self) -> None:
        # An evening-PDT wall clock (18:15-07:00 on 07-19) is 01:15Z on 07-20 —
        # the file must carry the UTC date 07-20, not the local 07-19.
        from feedback.cli import cmd_new
        pdt = timezone(timedelta(hours=-7))
        rc = cmd_new("maw", "advisor", "arc",
                     now=datetime(2026, 7, 19, 18, 15, tzinfo=pdt))
        self.assertEqual(rc, 0)
        names = [f.name for f in self.dir.glob("*.md")]
        self.assertTrue(any(n.startswith("2026-07-20-") for n in names), names)

    def test_new_collision_gets_suffix(self) -> None:
        rc1, out1, _ = self.run_cli(["new", "--repo", "widget", "--seat", "advisor"])
        rc2, out2, _ = self.run_cli(["new", "--repo", "widget", "--seat", "advisor"])
        self.assertEqual((rc1, rc2), (0, 0))
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(Path(out1.strip()).name, f"{today}-widget-advisor.md")
        self.assertEqual(Path(out2.strip()).name, f"{today}-widget-advisor-2.md")

    def test_new_rejects_filename_unsafe_names(self) -> None:
        rc, _, err = self.run_cli(["new", "--repo", "../evil", "--seat", "advisor"])
        self.assertEqual(rc, 1)
        self.assertIn("invalid repo", err)
        rc, _, err = self.run_cli(["new", "--repo", "widget", "--seat", "a b"])
        self.assertEqual(rc, 1)
        self.assertIn("invalid seat", err)

    def test_list_empty_and_pending(self) -> None:
        rc, out, _ = self.run_cli(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("pending: 0", out)
        self.run_cli(["new", "--repo", "maw", "--seat", "solo"])
        rc, out, _ = self.run_cli(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("pending: 1", out)
        self.assertIn("-maw-solo.md", out)
        self.assertIn("reviewed: 0", out)

    def test_archive_moves_to_reviewed(self) -> None:
        _, out, _ = self.run_cli(["new", "--repo", "maw", "--seat", "solo"])
        name = Path(out.strip()).name
        rc, out, _ = self.run_cli(["archive", name])
        self.assertEqual(rc, 0)
        dest = Path(out.strip())
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.parent.name, REVIEWED_DIRNAME)
        rc, out, _ = self.run_cli(["list"])
        self.assertIn("pending: 0", out)
        self.assertIn("reviewed: 1", out)

    def test_archive_missing_file_errors(self) -> None:
        rc, _, err = self.run_cli(["archive", "nope.md"])
        self.assertEqual(rc, 1)
        self.assertIn("no pending file", err)

    def test_archive_rejects_path_traversal(self) -> None:
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("secret")
        rc, _, err = self.run_cli(["archive", "../outside.md"])
        self.assertEqual(rc, 1)
        self.assertIn("no pending file", err)
        self.assertTrue(outside.is_file())

    def _strays(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "__pycache__").mkdir()
        (self.dir / "notes.txt").write_text("x")
        (self.dir / ".DS_Store").write_text("x")
        (self.dir / ".swap.md").write_text("x")
        (self.dir / "dir.md").mkdir()
        (self.dir / "x.pyc").write_text("x")

    def test_list_ignores_strays(self) -> None:
        self._strays()
        self.run_cli(["new", "--repo", "maw", "--seat", "solo"])
        rc, out, _ = self.run_cli(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("pending: 1", out)
        for stray in ("__pycache__", "notes.txt", ".DS_Store", ".swap.md", "dir.md", "x.pyc"):
            self.assertNotIn(stray, out)

    def test_archive_refuses_non_md(self) -> None:
        self._strays()
        for name in ("notes.txt", ".DS_Store", ".swap.md", "x.pyc"):
            rc, _, err = self.run_cli(["archive", name])
            self.assertEqual(rc, 1, name)
            self.assertTrue((self.dir / name).is_file(), name)

    def test_new_only_creates_md(self) -> None:
        _, out, _ = self.run_cli(["new", "--repo", "maw", "--seat", "solo"])
        self.assertEqual(Path(out.strip()).suffix, ".md")


if __name__ == "__main__":
    unittest.main()
