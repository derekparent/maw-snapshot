"""Tests for review.oracle — the probe/mock/snapshot harness (backlog item 2)."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import oracle  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_str_passthrough_with_trailing_newline(self) -> None:
        self.assertEqual(oracle._normalize("a"), "a\n")
        self.assertEqual(oracle._normalize("a\n"), "a\n")

    def test_crlf_normalized(self) -> None:
        self.assertEqual(oracle._normalize("a\r\nb"), "a\nb\n")

    def test_dict_is_order_stable(self) -> None:
        a = oracle._normalize({"y": 1, "x": 2})
        b = oracle._normalize({"x": 2, "y": 1})
        self.assertEqual(a, b)


class ProbeOrMockTest(unittest.TestCase):
    def test_not_live_uses_mock_and_flags_hole(self) -> None:
        res = oracle.probe_or_mock(
            probe=lambda: "LIVE", mock=lambda: "MOCK", live=False
        )
        self.assertEqual(res.value, "MOCK")
        self.assertEqual(res.source, "mock")
        self.assertTrue(res.ok)
        self.assertTrue(res.live_hole)

    def test_live_probe_ok(self) -> None:
        res = oracle.probe_or_mock(
            probe=lambda: "LIVE", mock=lambda: "MOCK", live=True
        )
        self.assertEqual(res.value, "LIVE")
        self.assertEqual(res.source, "probe")
        self.assertTrue(res.ok)
        self.assertFalse(res.live_hole)

    def test_live_probe_failure_falls_back_and_marks_not_ok(self) -> None:
        def boom() -> object:
            raise RuntimeError("host down")

        res = oracle.probe_or_mock(probe=boom, mock=lambda: "MOCK", live=True)
        self.assertEqual(res.value, "MOCK")
        self.assertEqual(res.source, "mock")
        self.assertFalse(res.ok)  # fallback is NOT a confirmed result
        self.assertTrue(res.live_hole)
        self.assertIn("LIVE HOLE", res.detail)


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "snaps"
        self.addCleanup(self.tmp.cleanup)

    def test_new_baseline_is_not_a_pass(self) -> None:
        res = oracle.verify("ui-layer", "radar tab\n", self.dir)
        self.assertEqual(res.status, "new")
        self.assertFalse(res.ok)  # unreviewed baseline must not count as verified
        self.assertTrue(res.path.exists())

    def test_match(self) -> None:
        oracle.verify("ui-layer", "radar tab\n", self.dir)
        res = oracle.verify("ui-layer", "radar tab\n", self.dir)
        self.assertEqual(res.status, "match")
        self.assertTrue(res.ok)
        self.assertEqual(res.diff, "")

    def test_changed_reports_diff_and_keeps_snapshot(self) -> None:
        oracle.verify("ui-layer", "radar tab\n", self.dir)
        res = oracle.verify("ui-layer", "wind tab\n", self.dir)  # silently wrong layer
        self.assertEqual(res.status, "changed")
        self.assertFalse(res.ok)
        self.assertIn("radar tab", res.diff)
        self.assertIn("wind tab", res.diff)
        # the recorded snapshot is NOT clobbered by a failing verify
        self.assertEqual(res.path.read_text(encoding="utf-8"), "radar tab\n")

    def test_changed_with_update_rewrites(self) -> None:
        oracle.verify("ui-layer", "radar tab\n", self.dir)
        res = oracle.verify("ui-layer", "wind tab\n", self.dir, update=True)
        self.assertEqual(res.status, "changed")
        self.assertNotEqual(res.diff, "")
        self.assertEqual(res.path.read_text(encoding="utf-8"), "wind tab\n")

    def test_dict_value_matches_across_orderings(self) -> None:
        oracle.verify("api", {"a": 1, "b": 2}, self.dir)
        res = oracle.verify("api", {"b": 2, "a": 1}, self.dir)
        self.assertEqual(res.status, "match")

    def test_snapshot_path_sanitizes_slashes(self) -> None:
        p = oracle.snapshot_path("a/b/c", self.dir)
        self.assertEqual(p.name, "a_b_c.snap")


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.dir = self.base / "snaps"
        self.cap = self.base / "capture.txt"
        self.addCleanup(self.tmp.cleanup)

    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = oracle.main(argv)
        return rc, out.getvalue()

    def test_new_then_match_then_changed(self) -> None:
        self.cap.write_text("radar\n", encoding="utf-8")
        rc, out = self._run(["snapshot", "--name", "x", "--file", str(self.cap), "--dir", str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertIn("NEW baseline", out)

        rc, out = self._run(["snapshot", "--name", "x", "--file", str(self.cap), "--dir", str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertIn("MATCH", out)

        self.cap.write_text("wind\n", encoding="utf-8")
        rc, out = self._run(["snapshot", "--name", "x", "--file", str(self.cap), "--dir", str(self.dir)])
        self.assertEqual(rc, 2)  # changed → non-zero so CI/reviewer notices
        self.assertIn("CHANGED", out)

    def test_missing_file_errors(self) -> None:
        rc, _ = self._run(["snapshot", "--name", "x", "--file", str(self.base / "nope"), "--dir", str(self.dir)])
        self.assertEqual(rc, 1)

    def test_strict_makes_new_baseline_a_failure(self) -> None:
        # Advisor note: under --strict/--ci a NEW (unreviewed) baseline must NOT
        # silently pass a CI gate — exit non-zero, matching SnapshotResult.ok.
        self.cap.write_text("radar\n", encoding="utf-8")
        rc, out = self._run(
            ["snapshot", "--name", "x", "--file", str(self.cap), "--dir", str(self.dir), "--strict"]
        )
        self.assertEqual(rc, 3)
        self.assertIn("NEW baseline", out)
        # once recorded, a matching capture passes even under --strict
        rc, out = self._run(
            ["snapshot", "--name", "x", "--file", str(self.cap), "--dir", str(self.dir), "--strict"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("MATCH", out)

    def test_ci_is_an_alias_for_strict(self) -> None:
        self.cap.write_text("radar\n", encoding="utf-8")
        rc, _ = self._run(
            ["snapshot", "--name", "y", "--file", str(self.cap), "--dir", str(self.dir), "--ci"]
        )
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
