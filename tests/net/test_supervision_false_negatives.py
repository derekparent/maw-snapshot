"""Supervision false negatives left by #370 (run 1006, lane 1).

(a) An until-clear ack outlived a clear nobody watched: the `cleared` marker
    was written only by a dead-man check that ran while the finding was gone,
    so with both seats' loops down across the clear (a Monitor expired and not
    yet re-armed) the ack covered the next stall for good.
(b) One tick on which the peer's status could not be read (so the finding was
    missing from that sweep) killed a valid until-clear ack. A tick that COULD
    read the status and saw no finding is a real clear and still ends it at
    once: the operator answers, the seat works under a minute, the next prompt
    appears.
(c) An advisor holding a live token was still excused as "waiting on the
    operator's arm" after a mid-run re-request.
(d) Three guards in net.waitstate had no test.

The loop here is the seats' own: `watch.wake_step` then `deadman.escalate`,
every 45 s, on an injected clock. No real pager, no real herdr.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from net import deadman, waitstate, watch  # noqa: E402
from net.liveness import Evidence  # noqa: E402
from tests.net import test_sweep_false_positives as tsf  # noqa: E402
from tests.net import test_waitstate as tw  # noqa: E402

FID = "blocked-seat:advisor"
UNREAD = "unread"
TICK = 45
MIN = 60


def _no_evidence(_subject: str) -> Evidence:
    return Evidence(None, "none")


class LoopHarness(unittest.TestCase):
    """The executor's supervision loop over a blocked advisor: the steps."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.t0 = 1_800_000_000.0
        self.pager = tw.Pager()
        self.wakes: list[float] = []
        self.pages: list[float] = []

    def tick(self, at: float, present: "bool | str" = True, seat: str = "executor") -> None:
        """One loop step at t0+`at` seconds: the wake step, then the check.
        `present` False = the status was read and no prompt is waiting;
        UNREAD = the status could not be read, which the sweep reports as a
        dark-session for the same seat."""
        now = self.t0 + at
        age = 16 + int(at // 60)
        if present == UNREAD:
            found = [watch.Finding("dark-session", "advisor", age,
                                   "herdr pane status unreadable", unread=True)]
        elif present:
            found = [watch.Finding("blocked-seat", "advisor", age, "operator prompt")]
        else:
            found = []
        with mock.patch.object(watch, "_now_s", return_value=now):
            if FID in watch.wake_step(found, seat):
                self.wakes.append(at)
        recs = deadman.escalate(found, seat, now, pager=self.pager, evidence_fn=_no_evidence,
                                subjects={"advisor", "executor"})
        if any(r["finding_id"] == FID for r in recs):
            self.pages.append(at)

    def run_loop(self, start: float, stop: float, present=lambda at: True) -> None:
        at = start
        while at < stop:
            self.tick(at, present(at))
            at += TICK

    def ack_until_clear(self, at: float) -> None:
        deadman.ack(FID, "executor", now_s=self.t0 + at, until_clear=True)

    def marks(self) -> int:
        return len([r for r in deadman._read_ledger(deadman.ACK_LEDGER) if r.get("cleared")])

    def voids(self) -> int:
        return len([r for r in deadman._read_ledger(deadman.ACK_LEDGER) if r.get("void")])


class ComposedLoop(LoopHarness):
    """The executor's supervision loop over a blocked advisor."""

    def test_a_clear_with_no_loop_running_then_a_reblock_wakes_and_pages(self) -> None:
        # The loop runs 20 min with the ack in place, then stops for 30 min (a
        # Monitor expired, not yet re-armed). In the gap the operator answers
        # the prompt (25) and the seat blocks again (35). Nothing saw the clear.
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 20 * MIN)
        self.assertEqual((self.wakes, self.pages), ([0.0], []))
        resume = 50 * MIN
        self.run_loop(resume, 80 * MIN)
        # The first check after the gap ends the ack; the next step wakes the
        # supervisor; the page comes one full ack window after that check.
        self.assertEqual(self.wakes, [0.0, resume + TICK])
        self.assertEqual(len(self.pages), 1)
        self.assertGreaterEqual(self.pages[0], resume + 10 * MIN)
        self.assertLessEqual(self.pages[0], resume + 10 * MIN + TICK)
        self.assertEqual(self.marks(), 1)

    def test_the_new_stall_can_be_acked_again(self) -> None:
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 20 * MIN)
        self.run_loop(50 * MIN, 52 * MIN)
        self.ack_until_clear(52 * MIN)
        self.run_loop(52 * MIN, 4 * 60 * MIN)
        self.assertEqual(self.pages, [])
        self.assertEqual(len(self.wakes), 2)
        self.assertEqual(self.marks(), 1)

    def test_one_missing_tick_does_not_end_a_valid_ack(self) -> None:
        # (b) The peer's status could not be read on one tick, so the finding
        # was not in that sweep. The prompt never left the pane.
        blips = {round(k * TICK) for k in (40, 90, 91 + 40)}
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 3 * 60 * MIN,
                      present=lambda at: UNREAD if round(at) in blips else True)
        self.assertEqual((self.wakes, self.pages, self.marks()), ([0.0], [], 0))

    def test_two_unreadable_ticks_in_a_row_end_the_ack(self) -> None:
        gone = {round(40 * TICK), round(41 * TICK)}
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 2 * 60 * MIN,
                      present=lambda at: UNREAD if round(at) in gone else True)
        back = 42 * TICK
        self.assertEqual(self.wakes, [0.0, back])
        self.assertEqual(self.marks(), 1)
        self.assertEqual(len(self.pages), 1)
        self.assertGreaterEqual(self.pages[0], back + 10 * MIN)

    def test_a_real_clear_seen_by_one_tick_ends_the_ack_at_once(self) -> None:
        # Review of 1bfa12a (F1): the operator answers, the seat works for
        # under a minute, the next prompt appears. One tick reads the status
        # and sees no prompt. That is a clear, not a flake.
        gone = round(40 * TICK)
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 2 * 60 * MIN, present=lambda at: round(at) != gone)
        back = 41 * TICK
        self.assertEqual(self.marks(), 1)
        self.assertEqual(self.wakes, [0.0, back])
        self.assertEqual(len(self.pages), 1)
        self.assertGreaterEqual(self.pages[0], back + 10 * MIN)
        self.assertLessEqual(self.pages[0], back + 10 * MIN + TICK)

    def test_a_readable_absence_after_an_unreadable_one_is_a_clear(self) -> None:
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        seq = {round(40 * TICK): UNREAD, round(41 * TICK): False}
        self.run_loop(90, 60 * MIN, present=lambda at: seq.get(round(at), True))
        self.assertEqual(self.marks(), 1)
        self.assertEqual(self.wakes, [0.0, 42 * TICK])

    def test_two_seats_seeing_the_same_missing_tick_is_still_one_absence(self) -> None:
        # Both loops run, 10 s apart. One unreadable moment shows up in both.
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        for k in range(2, 120):
            present = UNREAD if k == 50 else True
            self.tick(k * TICK, present, seat="executor")
            with mock.patch.object(watch, "wake_step", return_value=""):
                self.tick(k * TICK + 10, present, seat="advisor")
        self.assertEqual((self.wakes, self.pages, self.marks()), ([0.0], [], 0))

    def test_a_short_outage_with_the_finding_still_there_keeps_the_ack(self) -> None:
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 20 * MIN)
        self.run_loop(20 * MIN + deadman.PRESENCE_GAP_S - TICK, 3 * 60 * MIN)
        self.assertEqual((self.wakes, self.pages, self.marks()), ([0.0], [], 0))

    def test_an_ack_given_while_no_loop_runs_is_held_from_the_ack(self) -> None:
        self.run_loop(0, 90)
        self.ack_until_clear(30 * MIN)   # the supervisor looked by hand, loop down
        self.run_loop(30 * MIN + 2 * MIN, 2 * 60 * MIN)
        self.assertEqual((self.pages, self.marks()), ([], 0))

    def test_an_unreadable_presence_state_fails_toward_the_wake(self) -> None:
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 20 * MIN)
        path = deadman._ledger_path(deadman.PRESENCE_STATE)
        self.assertTrue(path.is_file())
        path.write_text("{not json", encoding="utf-8")
        self.run_loop(20 * MIN, 22 * MIN)
        self.assertEqual(self.marks(), 1)
        self.assertEqual(len(self.wakes), 2)

    def test_presence_state_holds_only_live_until_clear_acks(self) -> None:
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        self.run_loop(90, 10 * MIN)
        path = deadman._ledger_path(deadman.PRESENCE_STATE)
        self.assertEqual(list(json.loads(path.read_text(encoding="utf-8"))), [FID])
        deadman.revoke(FID, "executor", now_s=self.t0 + 10 * MIN)
        self.tick(10 * MIN + TICK)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})

    def test_note_cleared_ignores_stamps_from_the_future_or_before_the_ack(self) -> None:
        self.ack_until_clear(0)
        path = deadman._ledger_path(deadman.PRESENCE_STATE)
        path.write_text(json.dumps({FID: {"seen": self.t0 + 9e9, "absent": True}}),
                        encoding="utf-8")
        # 6 min after the ack, a forged far-future sighting must not hold it.
        self.assertEqual(deadman.note_cleared({FID}, "executor", self.t0 + 6 * MIN,
                                              {"advisor", "executor"}), [FID])


    def test_an_ack_stamped_in_the_future_is_not_an_ack(self) -> None:
        # A skewed clock or a forged ledger line. On main it sat inside every
        # later episode, past any cleared marker, and silenced the finding
        # until its own date came round.
        self.run_loop(0, 90)
        self.ack_until_clear(9 * 60 * MIN)
        self.run_loop(90, 60 * MIN)
        self.assertEqual(self.wakes, [0.0])       # woken once; never acked
        self.assertEqual(len(self.pages), 1)
        self.assertLessEqual(self.pages[0], 10 * MIN + TICK)

    def test_an_ack_stamped_in_the_future_is_still_ended(self) -> None:
        # ...and it does not stay live in the ledger: a clear ends it, so it
        # cannot come into force later for a different stall.
        self.run_loop(0, 90)
        self.ack_until_clear(9 * 60 * MIN)
        gone = {round(40 * TICK), round(41 * TICK)}
        self.run_loop(90, 60 * MIN, present=lambda at: round(at) not in gone)
        # It is voided at first sight (#379), so no clear marker is needed.
        self.assertEqual((self.marks(), self.voids()), (0, 1))
        f = watch.Finding("blocked-seat", "advisor", 600, "operator prompt")
        late = self.t0 + 10 * 60 * MIN            # past the ack's own date
        self.assertFalse(deadman._acked_valid(FID, late, deadman.finding_episode_start(f, late)))


class Notes379(LoopHarness):
    """#379: the four notes from the PR 376 review, on the composed loop."""

    DARK = "dark-session:advisor"

    def sweep(self, at: float, kind: "str | None", unread: bool = False) -> list[str]:
        """One dead-man check with a single finding for the advisor (or none);
        returns the ids paged."""
        now = self.t0 + at
        found = [] if kind is None else [
            watch.Finding(kind, "advisor", 16 + int(at // 60), "x", unread=unread)]
        recs = deadman.escalate(found, "executor", now, pager=self.pager,
                                evidence_fn=_no_evidence, subjects={"advisor", "executor"})
        return [r["finding_id"] for r in recs]

    # --- note 1: the page clock means CONTINUOUSLY seen -------------------

    def test_two_unreadable_ticks_hours_apart_do_not_page(self) -> None:
        # A working seat: absent on every readable tick, a dark-session only
        # on the two ticks where the status could not be read.
        paged: list[str] = []
        at = 0.0
        while at < 6 * 60 * MIN:
            flaky = round(at) in (0, round(4 * 60 * MIN))
            paged += self.sweep(at, "dark-session" if flaky else None, unread=flaky)
            at += TICK
        self.assertEqual(paged, [])

    def test_a_finding_seen_continuously_still_pages_after_the_window(self) -> None:
        paged = [(at, self.sweep(at, "dark-session", unread=True))
                 for at in range(0, 20 * MIN, TICK)]
        first = [at for at, ids in paged if ids]
        self.assertEqual(first, [10 * MIN + (TICK - 10 * MIN % TICK) % TICK])

    def test_a_permanently_unreadable_dark_seat_still_pages(self) -> None:
        # The fix must not turn "could not read" into "never page".
        ids: list[str] = []
        for at in range(0, 15 * MIN, TICK):
            ids += self.sweep(at, "dark-session", unread=True)
        self.assertEqual(ids, [self.DARK])

    def test_a_readable_absence_does_not_restart_a_real_findings_clock(self) -> None:
        # A hung seat reading `working` drops stuck-while-working for a tick
        # on every local-branch commit. Seen on readable checks, it keeps its
        # first-seen stamp: the page comes at minute 10, not after the flicker.
        pages = []
        for at in range(0, 25 * MIN, TICK):
            gone = at == 11 * TICK
            pages += [at for _ in self.sweep(at, None if gone else "stuck-while-working")]
        self.assertEqual(len(pages), 1)
        self.assertLess(pages[0], 11 * MIN)

    def test_a_flicker_every_few_minutes_cannot_hold_the_page_off(self) -> None:
        pages = []
        for n, at in enumerate(range(0, 60 * MIN, TICK)):
            pages += [at for _ in self.sweep(at, None if n % 8 == 7 else "blocked-seat")]
        self.assertEqual(len(pages), 1)
        self.assertLess(pages[0], 11 * MIN)

    def test_an_unreadable_tick_does_not_restart_the_clock(self) -> None:
        # At 8:15 the status could not be read: nobody saw the prompt gone.
        pages = []
        for at in range(0, 25 * MIN, TICK):
            if at == 11 * TICK:
                self.sweep(at, "dark-session", unread=True)
            else:
                pages += [at for i in self.sweep(at, "blocked-seat") if i == FID]
        self.assertEqual(len(pages), 1)
        self.assertLess(pages[0], 11 * MIN)

    def test_one_readable_sighting_makes_an_unread_run_a_real_one(self) -> None:
        # Unread at 0, read (idle, really dark) at 0:45, then read-and-absent
        # once at 1:30: the run was seen on a readable check, so it is kept.
        pages = []
        for n, at in enumerate(range(0, 15 * MIN, TICK)):
            kind = None if n == 2 else "dark-session"
            pages += [at for _ in self.sweep(at, kind, unread=(n == 0))]
        self.assertEqual(len(pages), 1)
        self.assertLess(pages[0], 11 * MIN)

    def test_without_a_pair_scope_no_sighting_is_closed(self) -> None:
        deadman.escalate([watch.Finding("dark-session", "advisor", 20, "x", unread=True)],
                         "executor", self.t0, pager=self.pager, evidence_fn=_no_evidence,
                         subjects=None)
        self.assertEqual(deadman.note_gone(set(), self.t0 + 45, None), [])
        self.assertEqual(deadman.note_gone(set(), self.t0 + 45, {"someone-else"}), [])
        self.assertEqual(deadman.note_gone(set(), self.t0 + 45, {"advisor"}), [self.DARK])
        self.assertEqual(deadman.note_gone(set(), self.t0 + 90, {"advisor"}), [])  # once

    def test_a_sighting_recorded_before_this_change_is_never_closed(self) -> None:
        deadman._append_ledger(deadman.FIRST_SEEN_LEDGER,
                               {"ts": "x", "epoch": self.t0, "finding_id": self.DARK})
        self.assertEqual(deadman.note_gone(set(), self.t0 + 45, {"advisor"}), [])

    # --- note 2: unknown / unparseable reads do not end an until-clear ack ---

    def test_one_unread_tick_of_either_kind_keeps_the_ack(self) -> None:
        # The sweep now reports both as an unread dark-session (UnreadStatus).
        self.run_loop(0, 90)
        self.ack_until_clear(90)
        flake = round(40 * TICK)
        self.run_loop(90, 60 * MIN, present=lambda at: UNREAD if round(at) == flake else True)
        self.assertEqual((self.marks(), self.pages), (0, []))

    # --- note 3: a clock that steps back ----------------------------------

    def _step_back(self, step: int) -> None:
        """Acked at clock `step`+95; the clock then steps back `step` seconds
        and the finding persists 3 h."""
        self.run_loop(step, step + 90)
        self.ack_until_clear(step + 95)
        self.run_loop(100, 3 * 60 * MIN)

    def test_a_clock_stepping_back_15_minutes_after_an_ack_does_not_page(self) -> None:
        self._step_back(900)
        self.assertEqual(self.pages, [])
        self.assertEqual(self.voids(), 0)

    def test_an_ack_dated_an_hour_ahead_is_voided_and_the_stall_pages(self) -> None:
        self._step_back(3600)
        self.assertEqual(self.voids(), 1)
        self.assertEqual(len(self.pages), 1)

    # --- note 4: a future-dated ack never comes alive ---------------------

    def test_a_future_dated_ack_on_a_finding_that_never_clears_stays_dead(self) -> None:
        self.run_loop(0, 90)
        self.ack_until_clear(60 * MIN)                 # dated an hour ahead
        self.run_loop(90, 30 * MIN)
        f = watch.Finding("blocked-seat", "advisor", 200, "operator prompt")
        for late in (61 * MIN, 2 * 60 * MIN, 24 * 60 * MIN):   # its date has arrived
            now = self.t0 + late
            self.assertFalse(deadman._acked_valid(FID, now, deadman.finding_episode_start(f, now)))
        self.assertEqual(self.voids(), 1)              # voided once, not per check

    def test_a_void_names_one_ack_and_leaves_a_later_real_one_alone(self) -> None:
        self.ack_until_clear(60 * MIN)
        self.tick(0)
        self.ack_until_clear(50)                       # a real ack, after the void
        f = watch.Finding("blocked-seat", "advisor", 20, "operator prompt")
        now = self.t0 + 100
        self.assertTrue(deadman._acked_valid(FID, now, deadman.finding_episode_start(f, now)))
        self.assertEqual(self.voids(), 1)

    def test_another_pairs_future_ack_is_not_voided_from_here(self) -> None:
        deadman.ack("blocked-seat:advisor2", "executor2", now_s=self.t0 + 60 * MIN,
                    until_clear=True)
        self.tick(0)
        self.assertEqual(self.voids(), 0)
        self.assertEqual(deadman.void_future_acks("executor2", self.t0, None),
                         ["blocked-seat:advisor2"])
        self.assertEqual(deadman.void_future_acks("executor2", self.t0, None), [])


class UnreadStatus(tw.PairCase):
    """The sweep says when it could not read a seat's status."""

    def findings(self, runner) -> "list[watch.Finding]":
        return [f for f in watch.dark_sessions(15, self.now, runner=runner)
                if f.subject == "advisor"]

    def test_an_unreadable_herdr_status_marks_the_dark_finding_unread(self) -> None:
        self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")

        def unreadable(cmd, stdin_text=None):
            import subprocess
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no socket")
        got = self.findings(unreadable)
        self.assertEqual([(f.kind, f.unread) for f in got], [("dark-session", True)])

    def test_a_readable_status_is_not_marked_unread(self) -> None:
        self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")
        for status, kind in (("idle", "dark-session"), ("blocked", "blocked-seat")):
            got = self.findings(tw.HerdrStatus(status))
            self.assertEqual([(f.kind, f.unread) for f in got], [(kind, False)], status)

    def test_a_status_of_unknown_is_not_a_look_at_the_seat(self) -> None:
        # #379: herdr answering `unknown` could not classify the pane.
        self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")
        got = self.findings(tw.HerdrStatus("unknown"))
        self.assertEqual([(f.kind, f.unread) for f in got], [("dark-session", True)])

    def test_an_unparseable_last_seen_is_marked_unread(self) -> None:
        # #379: one torn read of the entry is not a look at the seat either.
        e = self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")
        data = dict(e.__dict__, last_seen="not-a-time")
        (tw.registry_dir() / "advisor.json").write_text(json.dumps(data), encoding="utf-8")
        got = self.findings(tw.HerdrStatus("blocked"))
        self.assertEqual([(f.kind, f.unread) for f in got], [("dark-session", True)])

    def test_a_tmux_seat_is_never_marked_unread(self) -> None:
        self.seat("advisor", "advisor")
        got = self.findings(tw.HerdrStatus("idle"))
        self.assertEqual([f.unread for f in got], [False])

    def test_escalate_passes_the_unreadable_subjects_to_the_clear_rule(self) -> None:
        deadman.ack(FID, "executor", now_s=self.now, until_clear=True)
        dark = watch.Finding("dark-session", "advisor", 20, "status unreadable", unread=True)
        deadman.escalate([dark], "executor", self.now + 45, pager=tw.Pager(),
                         evidence_fn=_no_evidence, subjects={"advisor", "executor"})
        self.assertEqual([r for r in deadman._read_ledger(deadman.ACK_LEDGER)
                          if r.get("cleared")], [])
        deadman.escalate([], "executor", self.now + 90, pager=tw.Pager(),
                         evidence_fn=_no_evidence, subjects={"advisor", "executor"})
        self.assertEqual(len([r for r in deadman._read_ledger(deadman.ACK_LEDGER)
                              if r.get("cleared")]), 1)


class ArmExcuse(tsf.ArmCase):
    def marker(self, adv, at: float) -> None:
        from guards import session_auth as sa
        path = sa._arm_wait_path(adv.session_id, tsf.REPO)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session_id": adv.session_id, "repo": tsf.REPO,
                                    "created_at": tw._iso(at),
                                    "requests": [tw._iso(at)]}), encoding="utf-8")

    def advisor_holding_a_review_request(self):
        """The executor spoke last and the advisor pulled it: the advisor holds
        the ball. Then it asked for an arm again and went silent."""
        adv, exe = self.held_for_arm()
        time.sleep(1.1)
        self.send(exe, adv, "REVIEW REQUEST")
        self.pull(adv)
        self.marker(adv, self.now - 5 * 60)
        return adv, exe

    def executor_holding_a_goal(self):
        adv, exe = self.held_for_arm()
        time.sleep(1.1)
        self.send(adv, exe, "/goal: build lane 1")
        self.pull(exe)
        self.marker(adv, self.now - 5 * 60)
        return adv, exe

    def token_path(self, adv) -> Path:
        return Path(adv.repo_root) / ".maw" / ".runtime" / "session-auth.json"

    # (c)
    def test_fixture_an_unarmed_advisor_that_asked_is_waiting(self) -> None:
        self.advisor_holding_a_review_request()
        self.assertEqual(self.kinds("advisor"), ["waiting-on-counterpart"])

    def test_an_armed_advisor_is_not_excused_by_its_own_rerequest(self) -> None:
        adv, _exe = self.advisor_holding_a_review_request()
        self.token(adv, armed_at=self.now - 50 * 60)  # live, armed BEFORE the request
        self.assertEqual(self.kinds("advisor"), ["dark-session"])

    # (d)
    def test_fixture_an_executor_held_for_the_arm_is_waiting(self) -> None:
        self.executor_holding_a_goal()
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])

    def test_an_unreadable_token_is_treated_as_live(self) -> None:
        adv, _exe = self.executor_holding_a_goal()
        path = self.token_path(adv)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(waitstate._token(adv)[1], "unreadable")
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_a_predecessor_sessions_token_is_not_this_pairs_arm(self) -> None:
        adv, _exe = self.executor_holding_a_goal()
        self.token(adv, armed_at=self.now - 50 * 60)
        path = self.token_path(adv)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["session_id"] = "advisor-of-an-earlier-run"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(waitstate._holds_live_token(adv, self.now))
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])
        # The same token under THIS session id is the pair's arm: no excuse.
        data["session_id"] = adv.session_id
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_a_request_newer_than_now_never_excuses(self) -> None:
        # The lower bound of the cap, reached past _arm_requests' own filter.
        adv, exe = self.executor_holding_a_goal()
        entries = [adv, exe]
        self.assertIsNotNone(waitstate.waiting_on_arm(exe, entries, self.now))
        with mock.patch.object(waitstate, "_arm_requests", return_value=[self.now + 1]):
            self.assertIsNone(waitstate.waiting_on_arm(exe, entries, self.now))
        with mock.patch.object(waitstate, "_arm_requests",
                               return_value=[self.now - waitstate.ARM_WAIT_MAX_MIN * 60 - 1]):
            self.assertIsNone(waitstate.waiting_on_arm(exe, entries, self.now))
        with mock.patch.object(waitstate, "_arm_requests",
                               return_value=[self.now - waitstate.ARM_WAIT_MAX_MIN * 60]):
            self.assertIsNotNone(waitstate.waiting_on_arm(exe, entries, self.now))


if __name__ == "__main__":
    unittest.main()
