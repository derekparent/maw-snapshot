"""Tests for review.harness — the force → capture → verify orchestrator.

Behavioral: assert the harness runs the legs in the right ORDER, short-circuits
on the first failing leg, and reports pass/fail correctly. The force/capture
hooks are injected (the product-specific part), so these run offline with no
product, no network, no grants.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import harness  # noqa: E402


def _ok(out: str):
    return lambda: (True, out)


def _fail(out: str = ""):
    return lambda: (False, out)


class HarnessOrderTest(unittest.TestCase):
    def test_force_then_capture_then_verify_in_order(self) -> None:
        calls: list[str] = []

        def force():
            calls.append("force")
            return True, "forced-state"

        def capture():
            calls.append("capture")
            return True, "captured-payload"

        with tempfile.TemporaryDirectory() as d:
            res = harness.run_harness(
                name="t", force=force, capture=capture, snapshot_dir=Path(d)
            )
        # force ran before capture
        self.assertEqual(calls, ["force", "capture"])
        # first run records a NEW baseline → not a pass (unreviewed)
        self.assertFalse(res.ok)
        self.assertEqual([l.name for l in res.legs], ["force", "capture", "verify"])
        self.assertEqual(res.legs[-1].name, "verify")

    def test_force_failure_short_circuits_before_capture(self) -> None:
        captured = {"ran": False}

        def capture():
            captured["ran"] = True
            return True, "x"

        with tempfile.TemporaryDirectory() as d:
            res = harness.run_harness(
                name="t", force=_fail("force boom"), capture=capture, snapshot_dir=Path(d)
            )
        self.assertFalse(captured["ran"])  # capture never ran
        self.assertFalse(res.ok)
        self.assertEqual(res.failed_leg, "force")
        self.assertEqual([l.name for l in res.legs], ["force"])

    def test_capture_failure_short_circuits_before_verify(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            res = harness.run_harness(
                name="t", force=_ok("f"), capture=_fail("cap boom"), snapshot_dir=Path(d)
            )
        self.assertFalse(res.ok)
        self.assertEqual(res.failed_leg, "capture")
        self.assertEqual([l.name for l in res.legs], ["force", "capture"])
        # no snapshot file written when capture fails
        self.assertEqual(list(Path(d).glob("*.snap")), [])


class HarnessVerifyTest(unittest.TestCase):
    def test_matching_capture_passes_on_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            # first run records the baseline (new, not a pass)
            r1 = harness.run_harness(
                name="tab", force=_ok("f"), capture=_ok("active-tab=Regs"),
                snapshot_dir=Path(d),
            )
            self.assertFalse(r1.ok)
            self.assertEqual(r1.snapshot.status, "new")
            # second run with the SAME capture matches → pass
            r2 = harness.run_harness(
                name="tab", force=_ok("f"), capture=_ok("active-tab=Regs"),
                snapshot_dir=Path(d),
            )
        self.assertTrue(r2.ok)
        self.assertEqual(r2.snapshot.status, "match")

    def test_changed_capture_fails_with_diff(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            harness.run_harness(
                name="tab", force=_ok("f"), capture=_ok("kind=map"),
                snapshot_dir=Path(d),
            )
            res = harness.run_harness(
                name="tab", force=_ok("f"), capture=_ok("kind=guide-page"),
                snapshot_dir=Path(d),
            )
        self.assertFalse(res.ok)
        self.assertEqual(res.failed_leg, "verify")
        self.assertEqual(res.snapshot.status, "changed")
        self.assertIn("guide-page", res.snapshot.diff)


class HarnessProvenanceLegTest(unittest.TestCase):
    """When artifact+sha are given, provenance runs FIRST and gates the rest."""

    def test_stale_artifact_aborts_before_force(self) -> None:
        forced = {"ran": False}

        def force():
            forced["ran"] = True
            return True, "f"

        with tempfile.TemporaryDirectory() as d:
            artifact = Path(d) / "App.app"
            artifact.mkdir()
            # stamp it from a fake clean tree at a different SHA
            from review import provenance
            (artifact / provenance.PROVENANCE_FILENAME).write_text(
                '{"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", '
                '"dirty": false, "built_at": "", '
                f'"artifact_sha256": "{provenance.artifact_digest(artifact)}"}}\n'
            )
            res = harness.run_harness(
                name="t", force=force, capture=_ok("c"),
                snapshot_dir=Path(d),
                artifact=str(artifact),
                expected_sha="bbbbbbb",  # different SHA under review
            )
        self.assertFalse(forced["ran"])  # never forced a stale artifact
        self.assertEqual(res.failed_leg, "provenance")
        self.assertEqual(res.legs[0].name, "provenance")

    def test_matching_provenance_lets_force_run(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            artifact = Path(d) / "App.app"
            artifact.mkdir()
            from review import provenance
            (artifact / provenance.PROVENANCE_FILENAME).write_text(
                '{"sha": "abcdef1234567890abcdef1234567890abcdef12", '
                '"dirty": false, "built_at": "", '
                f'"artifact_sha256": "{provenance.artifact_digest(artifact)}"}}\n'
            )
            res = harness.run_harness(
                name="t", force=_ok("f"), capture=_ok("c"),
                snapshot_dir=Path(d),
                artifact=str(artifact),
                expected_sha="abcdef1",
            )
        self.assertEqual(res.legs[0].name, "provenance")
        self.assertTrue(res.legs[0].ok)
        self.assertIn("force", [l.name for l in res.legs])


class HarnessMalformedProvenanceTest(unittest.TestCase):
    def test_a_malformed_sidecar_fails_the_leg_instead_of_raising(self) -> None:
        forced = {"ran": False}

        def force():
            forced["ran"] = True
            return True, "f"

        with tempfile.TemporaryDirectory() as d:
            artifact = Path(d) / "App.app"
            artifact.mkdir()
            from review import provenance
            (artifact / provenance.PROVENANCE_FILENAME).write_text(
                '{"sha": "abcdef1234567890abcdef1234567890abcdef12", '
                '"dirty": null, "built_at": ""}\n')
            res = harness.run_harness(
                name="t", force=force, capture=_ok("c"), snapshot_dir=Path(d),
                artifact=str(artifact), expected_sha="abcdef1")
        self.assertFalse(forced["ran"])
        self.assertEqual(res.failed_leg, "provenance")
        self.assertIn("MALFORMED", res.legs[0].detail)


class HarnessCliTest(unittest.TestCase):
    def test_argv_hooks_compose_through_cli(self) -> None:
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            rc = harness.main([
                "run", "--name", "echo-test",
                "--force", "true",
                "--capture", "printf hello",
                "--dir", d,
            ])
        # new baseline on first run → exit 2 (not a pass)
        self.assertEqual(rc, 2)

    def test_artifact_without_sha_is_rejected(self) -> None:
        rc = harness.main([
            "run", "--name", "x", "--force", "true", "--capture", "true",
            "--dir", "/tmp", "--artifact", "/tmp/whatever",
        ])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
