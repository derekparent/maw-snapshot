"""Tests for review.external_oracle — probe-live / mock-for-ci / snapshot modes.

Behavioral: the mode selects whether the live probe runs; the live hole is
acknowledged in mock-for-ci; a live-probe failure is NOT a pass; snapshot
records/diffs. Probe/mock are injected (product-specific), so this runs offline.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import external_oracle as ext  # noqa: E402


class ModeSelectionTest(unittest.TestCase):
    def test_mock_for_ci_skips_live_probe_and_flags_hole(self) -> None:
        probed = {"ran": False}

        def probe():
            probed["ran"] = True
            return "LIVE"

        res = ext.check_external(
            mode=ext.MOCK, probe=probe, mock=lambda: "MOCK"
        )
        self.assertFalse(probed["ran"])  # live probe deliberately not run
        self.assertEqual(res.probe.value, "MOCK")
        self.assertTrue(res.live_hole)
        # mock-for-ci is a conditional pass — ok True but hole acknowledged
        self.assertTrue(res.ok)
        self.assertIn("LIVE HOLE", ext.render(res))

    def test_probe_live_runs_probe_and_passes(self) -> None:
        res = ext.check_external(
            mode=ext.PROBE, probe=lambda: "LIVE-ANSWER", mock=lambda: "MOCK"
        )
        self.assertEqual(res.probe.value, "LIVE-ANSWER")
        self.assertEqual(res.probe.source, "probe")
        self.assertFalse(res.live_hole)
        self.assertTrue(res.ok)

    def test_probe_live_failure_falls_back_to_mock_but_is_not_a_pass(self) -> None:
        def boom():
            raise RuntimeError("auth failed")

        res = ext.check_external(
            mode=ext.PROBE, probe=boom, mock=lambda: "MOCK"
        )
        # fell back to mock value, but the verdict is NOT a pass and is a hole
        self.assertEqual(res.probe.value, "MOCK")
        self.assertTrue(res.live_hole)
        self.assertFalse(res.ok)

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ext.ExternalOracleError):
            ext.check_external(mode="bogus", probe=lambda: "x", mock=lambda: "y")


class SnapshotModeTest(unittest.TestCase):
    def test_snapshot_records_then_matches(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            r1 = ext.check_external(
                mode=ext.SNAPSHOT, probe=lambda: "answer-v1", mock=lambda: "m",
                name="asbestos", snapshot_dir=Path(d),
            )
            # new baseline is not a pass
            self.assertEqual(r1.snapshot.status, "new")
            self.assertFalse(r1.ok)
            r2 = ext.check_external(
                mode=ext.SNAPSHOT, probe=lambda: "answer-v1", mock=lambda: "m",
                name="asbestos", snapshot_dir=Path(d),
            )
        self.assertEqual(r2.snapshot.status, "match")
        self.assertTrue(r2.ok)

    def test_snapshot_change_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ext.check_external(
                mode=ext.SNAPSHOT, probe=lambda: "answer-v1", mock=lambda: "m",
                name="asbestos", snapshot_dir=Path(d),
            )
            res = ext.check_external(
                mode=ext.SNAPSHOT, probe=lambda: "answer-v2", mock=lambda: "m",
                name="asbestos", snapshot_dir=Path(d),
            )
        self.assertEqual(res.snapshot.status, "changed")
        self.assertFalse(res.ok)

    def test_snapshot_mode_requires_name_and_dir(self) -> None:
        with self.assertRaises(ext.ExternalOracleError):
            ext.check_external(
                mode=ext.SNAPSHOT, probe=lambda: "x", mock=lambda: "y"
            )


class CliTest(unittest.TestCase):
    def test_mock_for_ci_through_cli(self) -> None:
        # mock-for-ci with a deterministic fixture command → conditional pass
        with redirect_stdout(io.StringIO()):
            rc = ext.main([
                "check", "--mode", ext.MOCK,
                "--probe", "false",          # would fail if run — must NOT run
                "--mock", "printf fixture",
            ])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
