"""The dead-man's liveness-evidence gate (#190).

The 2026-07-22 live drill escalated `dark-session` for BOTH seats while both were
demonstrably working: the registry heartbeat ticks only on UserPromptSubmit, so a
tool-heavy turn reads stale. These tests pin the gate that fixes it — and, just as
importantly, pin every case where the gate must NOT silence the page.

Hermetic via MAW_NET_DIR; the evidence source is injected, so no test reads a real
transcript.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from net import deadman
from net.liveness import Evidence
from net.watch import Finding


class FakePager:
    def __init__(self) -> None:
        self.calls: list[tuple[Finding, str]] = []

    def __call__(self, finding: Finding, seat: str):
        self.calls.append((finding, seat))
        return True, "fake-pager"


class EvidenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._net = tempfile.TemporaryDirectory()
        self.addCleanup(self._net.cleanup)
        p = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._net.name})
        p.start()
        self.addCleanup(p.stop)
        self.now = 1_800_000_000.0
        # These tests pin ack/episode/evidence semantics against a stall-age
        # clock: pin first-seen at the episode start so they keep testing that.
        # The first-seen clock itself is tested unpinned in test_deadman_first_seen.
        fs = mock.patch.object(deadman, "_first_seen", lambda fid, now, start: start)
        fs.start()
        self.addCleanup(fs.stop)
        self.pager = FakePager()

    def _dark(self, subject="executor", age=20):
        return Finding("dark-session", subject, age,
                       "registered + pid alive but silent; herdr pane w4B:p2 status done")

    def _escalate(self, findings, evidence, basis="session-transcript", **kw):
        ev = Evidence(evidence, basis if evidence is not None else "none")
        return deadman.escalate(
            findings, "advisor", self.now, escalate_min=10, pager=self.pager,
            evidence_fn=lambda subject: ev, **kw)

    def _suppressions(self):
        return deadman._read_ledger(deadman.SUPPRESSION_LEDGER)


class TestEvidenceGate(EvidenceTestCase):
    def test_fresh_evidence_suppresses_the_page(self) -> None:
        """The exact 2026-07-22 false positive: heartbeat-stale, transcript live."""
        recs = self._escalate([self._dark()], evidence=2.0)
        self.assertEqual(recs, [])
        self.assertEqual(self.pager.calls, [])

    def test_suppression_is_recorded_not_silent(self) -> None:
        self._escalate([self._dark()], evidence=2.0)
        sup = self._suppressions()
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup[0]["finding_id"], "dark-session:executor")
        self.assertFalse(sup[0]["escalated"])
        self.assertEqual(sup[0]["suppressed_by"], "liveness-evidence")
        self.assertAlmostEqual(sup[0]["evidence_age_min"], 2.0)
        self.assertEqual(sup[0]["evidence"], "session-transcript")
        # the ESCALATION ledger stays clean — a suppression is not an escalation
        self.assertEqual(deadman._read_ledger(deadman.ESCALATION_LEDGER), [])

    def test_stale_evidence_still_escalates(self) -> None:
        recs = self._escalate([self._dark()], evidence=40.0)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(self.pager.calls), 1)

    def test_absent_evidence_still_escalates(self) -> None:
        """Fail-OPEN toward the page: a broken read must never silence the alarm."""
        recs = self._escalate([self._dark()], evidence=None)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(self.pager.calls), 1)

    def test_gate_applies_only_to_the_silence_triggered_kind(self) -> None:
        """stuck-while-working is POSITIVE evidence of a wedge (agent_status=working
        + stale + no commit). Fresh transcript writes must not silence it — that is
        the advisor's 'wedged seat with a live pid' attack."""
        wedged = Finding("stuck-while-working", "executor", 20, "no progress")
        recs = self._escalate([wedged], evidence=1.0)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(self.pager.calls), 1)

    def test_blocked_seat_is_not_gated(self) -> None:
        blocked = Finding("blocked-seat", "executor", 20, "approval unserviced")
        recs = self._escalate([blocked], evidence=1.0)
        self.assertEqual(len(recs), 1)

    def test_evidence_going_stale_mid_episode_escalates(self) -> None:
        """A suppression must not dedup away a later real page in the same episode."""
        self._escalate([self._dark()], evidence=2.0)
        recs = self._escalate([self._dark(age=25)], evidence=40.0)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(self.pager.calls), 1)

    def test_repeated_suppression_records_once_per_episode(self) -> None:
        """The sweep polls every 45s; the ledger must not grow one line per poll."""
        for _ in range(4):
            self._escalate([self._dark()], evidence=2.0)
        self.assertEqual(len(self._suppressions()), 1)

    def test_a_new_episode_records_its_own_suppression(self) -> None:
        self._escalate([self._dark(age=20)], evidence=2.0)
        later = deadman.escalate(
            [self._dark(age=20)], "advisor", self.now + 10_000, escalate_min=10,
            pager=self.pager,
            evidence_fn=lambda s: Evidence(2.0, "session-transcript"))
        self.assertEqual(later, [])
        self.assertEqual(len(self._suppressions()), 2)

    def test_threshold_is_the_boundary(self) -> None:
        at = self._escalate([self._dark()], evidence=float(
            deadman.DEFAULT_EVIDENCE_FRESH_MIN))
        self.assertEqual(at, [], "evidence exactly at the freshness bound counts as alive")
        past = self._escalate(
            [self._dark(age=21)], evidence=deadman.DEFAULT_EVIDENCE_FRESH_MIN + 0.1)
        self.assertEqual(len(past), 1)

    def test_a_weaker_dir_max_basis_is_recorded_as_such(self) -> None:
        """A suppression on the fallback basis must be legible as weaker evidence
        in the ledger — never silently equal to the seat's own transcript."""
        self._escalate([self._dark()], evidence=2.0, basis="dir-max-fallback")
        self.assertEqual(self._suppressions()[0]["evidence"], "dir-max-fallback")

    def test_evidence_fn_failure_does_not_sink_the_page(self) -> None:
        def boom(subject: str):
            raise RuntimeError("transcript read blew up")

        recs = deadman.escalate([self._dark()], "advisor", self.now,
                                escalate_min=10, pager=self.pager, evidence_fn=boom)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(self.pager.calls), 1)

    def test_an_ack_still_wins_without_consulting_evidence(self) -> None:
        """The ack path is unchanged: a supervisor who looked suppresses regardless."""
        looked = []

        def evidence(subject: str):
            looked.append(subject)
            return Evidence(None, "none")

        deadman.ack("dark-session:executor", "advisor", now_s=self.now - 60)
        recs = deadman.escalate([self._dark()], "advisor", self.now,
                                escalate_min=10, pager=self.pager,
                                evidence_fn=evidence)
        self.assertEqual(recs, [])
        self.assertEqual(looked, [], "an acked finding needs no evidence read")


class TestCheckCliWiring(EvidenceTestCase):
    def test_check_passes_the_freshness_knob_through(self) -> None:
        seen: dict = {}

        def fake_escalate(findings, seat, **kw):
            seen.update(kw)
            return []

        with mock.patch("net.deadman.sweep", return_value=[]), \
             mock.patch("net.deadman.findings_for_pair", return_value=[]), \
             mock.patch("net.deadman.escalate", side_effect=fake_escalate):
            rc = deadman.main(["check", "--for", "executor", "--evidence-fresh-min", "9"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["evidence_fresh_min"], 9)


if __name__ == "__main__":
    unittest.main()
