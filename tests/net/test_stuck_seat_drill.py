"""Lane 0 drill, post-lane-1 — the RED->GREEN gauge for a stalled + quiet seat.

The gizmo 2026-07-21 incident had TWO failure halves and the drill indicts
both:
  - DETECTION gap (status=working): lane 0 asserted the sweep was SILENT on this
    fixture; lane 1's stuck-while-working finding now CATCHES it. This file
    asserts the closed state — the red->green flip is the diff against lane 0.
  - CONSUMPTION gap (status=blocked, what the gizmo seat actually reported):
    the sweep FIRES a blocked-seat finding today, but nothing consumes/escalates
    it. Lanes 2/3 close it; the machine-assertable escalation red-first is lane 3.

The discriminating tests prove the working-mode fixture is a GENUINE stall
(caught under every herdr status now) and that the seat is really live
(pid_alive) — the silence lane 0 documented is a real hole, now closed.

Hermetic via MAW_NET_DIR (registry root) + HOME (durable-bridge root); time is
injected through now_s.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from net import watch
from net.drills import stuck_seat_drill as drill
from net.registry import pid_alive, who


class StuckSeatDrillTest(unittest.TestCase):
    def setUp(self) -> None:
        self._net = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._net.cleanup)
        self.addCleanup(self._home.cleanup)
        p = mock.patch.dict(
            os.environ, {"MAW_NET_DIR": self._net.name, "HOME": self._home.name}
        )
        p.start()
        self.addCleanup(p.stop)
        self.now_s = drill.FIXED_NOW_S

    def test_working_quiet_now_fires_stuck_while_working(self) -> None:
        # RED->GREEN: lane 0 asserted the sweep was SILENT on this same seeded
        # working+quiet fixture; lane 1's stuck_while_working finding now catches
        # it. The REAL sweep at now+31min surfaces a stuck-while-working finding.
        runner = drill.seed_stuck_pair(self.now_s, silent_min=31, status="working")
        kinds = {f.kind for f in drill.baseline_findings(self.now_s, runner)}
        self.assertIn("stuck-while-working", kinds)

    def test_consumption_gap_blocked_quiet_fires_blocked_seat(self) -> None:
        # CONSUMPTION-gap baseline (unaffected by lane 1): the gizmo seat
        # reported BLOCKED. blocked+quiet fires a blocked-seat finding — detection
        # is present — the failure was that nothing consumed/escalated it (lanes
        # 2/3). stuck-while-working does NOT fire here (that's the working half).
        runner = drill.seed_stuck_pair(self.now_s, silent_min=31, status="blocked")
        kinds = {f.kind for f in drill.baseline_findings(self.now_s, runner)}
        self.assertIn("blocked-seat", kinds)
        self.assertNotIn("stuck-while-working", kinds)

    def test_stuck_seat_is_pid_alive(self) -> None:
        # Adversarial target #1: the seeded entry must actually pass pid_alive so
        # the sweep treats it as a LIVE seat (not pruned) — otherwise a finding (or
        # silence) would be the trivial case of a reaped session, not the real seat.
        drill.seed_stuck_pair(self.now_s, silent_min=31)
        live = {e.role: e for e in who(prune=False)}
        self.assertIn("executor", live)
        self.assertTrue(pid_alive(live["executor"].pid))

    def test_no_status_hides_a_stalled_seat_anymore(self) -> None:
        # Adversarial target #2, post-lane-1: the seeded seat is caught under EVERY
        # herdr status. 'working' now surfaces it as stuck-while-working (the closed
        # detection hole); 'idle'/unreadable surface it as a dark-session. The
        # silence lane 0 documented is gone regardless of what the seat claims.
        drill.seed_stuck_pair(self.now_s, silent_min=31)
        cases = {
            "working": "stuck-while-working",
            "idle": "dark-session",
            "unreadable": "dark-session",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                runner = drill.WorkingHerdr("" if status == "unreadable" else status)
                kinds = {f.kind for f in watch.sweep(now_s=self.now_s, runner=runner)}
                self.assertIn(expected, kinds)

    def test_fresh_supervisor_never_flagged(self) -> None:
        # The peer (advisor) is under threshold, so it is not itself a finding —
        # the drill isolates the ONE stuck seat, it does not just silence the pair.
        drill.seed_stuck_pair(self.now_s, silent_min=31, peer_fresh_min=2)
        subjects = {f.subject for f in watch.sweep(now_s=self.now_s, runner=drill.WorkingHerdr("idle"))}
        self.assertNotIn("advisor", subjects)

    def _run_main(self, argv: list[str]) -> int:
        # main() manages its own temp roots, so snapshot/restore the two env vars
        # around it to avoid leaking a deleted dir into sibling tests.
        saved = {k: os.environ.get(k) for k in ("MAW_NET_DIR", "HOME")}
        try:
            return drill.main(argv)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_main_working_reports_detection_gap_closed_exit_one(self) -> None:
        # working-mode, post-lane-1: exit 1 == HOLE CLOSED (the sweep now catches
        # the seeded stall). The lane-0 baseline was exit 0 (silent); this is the
        # runnable red->green flip.
        self.assertEqual(self._run_main(["--silent-min", "31", "--status", "working"]), 1)

    def test_main_blocked_reports_consumption_gap_exit_zero(self) -> None:
        # blocked-mode (unaffected by lane 1): exit 0 == consumption-gap baseline
        # (blocked-seat fires, unconsumed). The finding exists; nobody's watching.
        self.assertEqual(self._run_main(["--silent-min", "31", "--status", "blocked"]), 0)


if __name__ == "__main__":
    unittest.main()
