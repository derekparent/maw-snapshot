"""net.deadman — the dead-man escalation (#149/F10 lane 3).

Hermetic via MAW_NET_DIR (the ledger root); time is injected via now_s; the pager
is injected so NO real page ever fires (exactly one real push happens in the live
drill, the operator present). Asserts the escalation RECORD + pager calls — the machine-
assertable artifact the run's acceptance gate rests on.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from net import deadman
from net.watch import Finding


class FakePager:
    def __init__(self, delivered: bool = True) -> None:
        self.delivered = delivered
        self.calls: list[tuple[Finding, str]] = []

    def __call__(self, finding: Finding, seat: str):
        self.calls.append((finding, seat))
        return self.delivered, "fake-pager"


class DeadmanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._net = tempfile.TemporaryDirectory()
        self.addCleanup(self._net.cleanup)
        p = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._net.name})
        p.start()
        self.addCleanup(p.stop)
        self.now = 1_800_000_000.0  # a fixed epoch
        # These tests pin ack/episode/evidence semantics against a stall-age
        # clock: pin first-seen at the episode start so they keep testing that.
        # The first-seen clock itself is tested unpinned in test_deadman_first_seen.
        fs = mock.patch.object(deadman, "_first_seen", lambda fid, now, start: start)
        fs.start()
        self.addCleanup(fs.stop)

    def _stall(self, kind="stuck-while-working", subject="executor", age=20):
        return Finding(kind, subject, age, "no progress")


class TestFindingId(DeadmanTestCase):
    def test_id_is_kind_and_subject(self) -> None:
        self.assertEqual(deadman.finding_id(self._stall()), "stuck-while-working:executor")


class TestEscalate(DeadmanTestCase):
    def test_unacked_stall_escalates_and_records(self) -> None:
        pager = FakePager()
        recs = deadman.escalate([self._stall(age=20)], "advisor", self.now,
                                escalate_min=10, pager=pager)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["finding_id"], "stuck-while-working:executor")
        self.assertTrue(recs[0]["escalated"])
        self.assertEqual(len(pager.calls), 1)
        # the record is persisted to the ledger (a close-out can prove it fired)
        self.assertEqual(len(deadman._read_ledger(deadman.ESCALATION_LEDGER)), 1)

    def test_under_threshold_does_not_escalate(self) -> None:
        pager = FakePager()
        recs = deadman.escalate([self._stall(age=5)], "advisor", self.now,
                                escalate_min=10, pager=pager)
        self.assertEqual(recs, [])
        self.assertEqual(pager.calls, [])

    def test_non_stall_kind_never_escalates(self) -> None:
        # model-flip is informational — it must NEVER reach the operator's phone (the live
        # drill FP). Even 30 min old, it does not escalate.
        pager = FakePager()
        for kind in ("model-flip", "watcher-quiet", "orphaned-draft",
                     "parked-wire", "refused-heartbeat"):
            with self.subTest(kind=kind):
                recs = deadman.escalate([self._stall(kind=kind, age=30)], "advisor",
                                        self.now, escalate_min=10, pager=pager)
                self.assertEqual(recs, [])
        self.assertEqual(pager.calls, [])

    def test_all_stall_kinds_escalate(self) -> None:
        for kind in deadman.ESCALATABLE_KINDS:
            with self.subTest(kind=kind):
                pager = FakePager()
                recs = deadman.escalate([self._stall(kind=kind, age=20)], "advisor",
                                        self.now, escalate_min=10, pager=pager)
                self.assertEqual(len(recs), 1)

    def test_ack_suppresses_escalation(self) -> None:
        # The auto-ack path: the supervisor looked, it is fine -> no page.
        deadman.ack("stuck-while-working:executor", "advisor", now_s=self.now)
        pager = FakePager()
        recs = deadman.escalate([self._stall(age=20)], "advisor", self.now,
                                escalate_min=10, pager=pager)
        self.assertEqual(recs, [])
        self.assertEqual(pager.calls, [])

    def test_stale_ack_from_prior_episode_does_not_suppress(self) -> None:
        # An ack from BEFORE this stall episode (older than now - age_min) must NOT
        # suppress — a new stall with the same id escalates afresh.
        old = self.now - 60 * 60  # an hour ago, well before the 20-min episode
        deadman.ack("stuck-while-working:executor", "advisor", now_s=old)
        pager = FakePager()
        recs = deadman.escalate([self._stall(age=20)], "advisor", self.now,
                                escalate_min=10, pager=pager)
        self.assertEqual(len(recs), 1)

    def test_ack_expires_and_persisting_stall_re_escalates(self) -> None:
        # Advisor item 2: an ack is 'I looked NOW', not 'ignore forever'. Within
        # the ttl it suppresses; past the ttl a persisting stall escalates (the
        # min-12-ack, min-20-wedge case — the peer wedged AFTER the look).
        deadman.ack("stuck-while-working:executor", "advisor", now_s=self.now, ttl_min=30)
        # within the 30-min ttl -> suppressed
        p1 = FakePager()
        self.assertEqual(
            deadman.escalate([self._stall(age=20)], "advisor", self.now + 20 * 60,
                             escalate_min=10, pager=p1), [])
        self.assertEqual(p1.calls, [])
        # past the ttl, the finding persists -> escalates
        p2 = FakePager()
        recs = deadman.escalate([self._stall(age=40)], "advisor", self.now + 40 * 60,
                                escalate_min=10, pager=p2)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(p2.calls), 1)

    def test_already_escalated_does_not_repage(self) -> None:
        pager = FakePager()
        first = deadman.escalate([self._stall(age=20)], "advisor", self.now,
                                 escalate_min=10, pager=pager)
        self.assertEqual(len(first), 1)
        second = deadman.escalate([self._stall(age=20)], "advisor", self.now,
                                  escalate_min=10, pager=pager)
        self.assertEqual(second, [])          # no re-page this episode
        self.assertEqual(len(pager.calls), 1)  # pager called exactly once


class TestAck(DeadmanTestCase):
    def test_ack_writes_ledger_record(self) -> None:
        deadman.ack("blocked-seat:executor", "advisor", now_s=self.now)
        recs = deadman._read_ledger(deadman.ACK_LEDGER)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["finding_id"], "blocked-seat:executor")
        self.assertEqual(recs[0]["seat"], "advisor")


class TestEndToEndDrill(DeadmanTestCase):
    """The acceptance-shape (machine half): a seeded stall with NO ack produces an
    escalation record; the same stall WITH an ack does not. The one real push is
    the live drill's job — here the pager is injected."""

    def test_seeded_stall_no_ack_escalates_with_ack_does_not(self) -> None:
        stall = self._stall(kind="blocked-seat", subject="executor", age=25)
        # no ack -> escalates
        pager = FakePager()
        recs = deadman.escalate([stall], "advisor", self.now, escalate_min=10, pager=pager)
        self.assertEqual(len(recs), 1)
        # a fresh episode (advance the clock past the ledgered escalation window)
        later = self.now + 3 * 60 * 60
        stall2 = self._stall(kind="blocked-seat", subject="executor", age=25)
        deadman.ack("blocked-seat:executor", "advisor", now_s=later)
        pager2 = FakePager()
        recs2 = deadman.escalate([stall2], "advisor", later, escalate_min=10, pager=pager2)
        self.assertEqual(recs2, [])
        self.assertEqual(pager2.calls, [])


class TestCli(DeadmanTestCase):
    def test_cli_ack(self) -> None:
        rc = deadman.main(["ack", "dark-session:executor", "--as", "advisor"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(deadman._read_ledger(deadman.ACK_LEDGER)), 1)

    def test_cli_check_clean_registry_returns_zero(self) -> None:
        # Empty registry -> no findings -> no escalation -> exit 0.
        rc = deadman.main(["check", "--for", "executor"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
