"""#345 / #346 — pair-sweep false positives that survived #210 / #293.

Fixtures are the live reports in findings/2026-10-03-feedback-round-4.md
(clusters A04/A05/A06):

  #345 (a) maw run 1002 / 1002c: both seats wait on the operator's arm. The
      advisor relayed "hold" to the executor, the executor pulled it, and the
      sweep read the executor `dark-session` (it held the ball by turn order)
      and the advisor "waiting on executor". The arm wait is not a
      net.question, so nothing excused it. Acked by hand 3-4 times per run.
  #345 (b) two peer-repo executors 10-01, a peer-repo advisor 09-28: a seat's
      sweep wakes the seat about its OWN idleness. (The dead-man still pages
      it: if the peer's monitor is down, that page is the last one left.)
  #345 (c) a peer-repo executor 10-02: the ack lapsed at 30 min while the
      operator's prompt still sat in the pane, and the same poll ESCALATED it.
      Another peer-repo executor 10-02: an ack given too early cannot be revoked.
  #346 peer-repo executors 09-01 and 10-01/10-02, maw 10-02: the loop
      re-fires a persisting finding when another one is acked (id-set
      oscillation), on every Monitor re-arm (prev_ids empty), and delivers a
      header with no detail when the finding clears between two calls.

Every suppression has a FALSIFIER beside it: a seat that is genuinely dark
(holding work, a satisfied arm, no live peer) still surfaces and still pages.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from net import deadman, watch
from net.liveness import Evidence
from tests.net import test_waitstate as tw


REPO = "o/r"


def _no_evidence(_subject: str) -> Evidence:
    return Evidence(None, "none")


class ArmCase(tw.PairCase):
    def setUp(self) -> None:
        super().setUp()
        self.grants = Path(self._tmp.name) / "grants"
        env = mock.patch.dict(os.environ, {"MAW_GRANTS_DIR": str(self.grants)})
        env.start()
        self.addCleanup(env.stop)
        # The fixture repo has no origin remote; the seats' repo is REPO.
        from net import waitstate
        repo = mock.patch.object(waitstate, "_advisor_repo", return_value=REPO)
        repo.start()
        self.addCleanup(repo.stop)

    def batch_request(self, adv, at: float | None = None) -> None:
        """The record `session_auth grant-request session.arm --batch` leaves."""
        req = self.grants / "requests" / f"{adv.session_id}.json"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(json.dumps({"line": {"session_id": adv.session_id, "repo": REPO},
                                   "created_at": tw._iso(at or self.now - 25 * 60)}),
                       encoding="utf-8")

    def token(self, adv, armed_at: float) -> None:
        path = Path(adv.repo_root) / ".maw" / ".runtime" / "session-auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": adv.session_id, "pid": adv.pid, "seat": "advisor",
            "armed_at": tw._iso(armed_at), "expires_at": tw._iso(armed_at + 6 * 3600),
            "scope": "lanes 1-3", "nonce": "n"}), encoding="utf-8")

    def held_for_arm(self):
        """maw 1002c: executor grounded, advisor relayed 'hold for the arm',
        executor pulled it; the advisor asked the operator for the arm."""
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv, "grounded, standing by")
        self.pull(adv)
        time.sleep(1.1)
        self.send(adv, exe, "HOLD: waiting on the operator's arm")
        self.pull(exe)
        return adv, exe


class TestOperatorArmWait(ArmCase):
    def test_fixture_both_seats_held_for_an_arm_are_waiting(self) -> None:
        adv, _exe = self.held_for_arm()
        self.batch_request(adv)
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])
        self.assertEqual(self.kinds("advisor"), ["waiting-on-counterpart"])
        details = {f.subject: f.detail for f in
                   watch.dark_sessions(15, self.now, runner=tw.HerdrStatus("idle"))}
        self.assertIn("arm", details["executor"])
        self.assertIn("arm", details["advisor"])
        self.assertNotIn("waiting on executor", details["advisor"])

    def test_single_phone_arm_request_is_recorded_and_read(self) -> None:
        # The non-batch `grant-request session.arm` left no artifact at all.
        adv, _exe = self.held_for_arm()
        self.ask(adv, self.now - 5 * 60)
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])

    def test_falsifier_satisfied_arm_no_longer_excuses(self) -> None:
        adv, _exe = self.held_for_arm()
        self.batch_request(adv, at=self.now - 25 * 60)
        self.token(adv, armed_at=self.now - 22 * 60)  # the operator armed after it
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_request_from_a_previous_registration(self) -> None:
        adv, _exe = self.held_for_arm()
        self.batch_request(adv, at=self.now - 2 * 3600)  # before started_at
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_dead_seat_with_work_in_flight_still_escalates(self) -> None:
        # An arm is pending, but a turn waits for the executor unpulled: the
        # executor holds work and is silent. That is dark, and the dead-man
        # pages it once the supervisor's window closes.
        adv, exe = self.held_for_arm()
        self.batch_request(adv)
        self.send(adv, exe, "/goal: build lane 1")  # never pulled
        found = [f for f in watch.dark_sessions(15, self.now, runner=tw.HerdrStatus("idle"))
                 if f.subject == "executor"]
        self.assertEqual([f.kind for f in found], ["dark-session"])
        pager = tw.Pager()
        deadman.escalate(found, "advisor", self.now, pager=pager, evidence_fn=_no_evidence)
        later = [watch.Finding(f.kind, f.subject, f.age_min + 11, f.detail) for f in found]
        recs = deadman.escalate(later, "advisor", self.now + 11 * 60, pager=pager,
                                evidence_fn=_no_evidence)
        self.assertEqual([r["finding_id"] for r in recs], ["dark-session:executor"])


    def marker(self, adv, at: float, repo: str = REPO) -> None:
        """The arm-wait marker `grant-request session.arm` writes."""
        from guards import session_auth as sa
        path = sa._arm_wait_path(adv.session_id, repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session_id": adv.session_id, "repo": repo,
                                    "created_at": tw._iso(at),
                                    "requests": [tw._iso(at)]}), encoding="utf-8")

    def ask(self, adv, at: float) -> None:
        """Run the real `grant-request session.arm` as the advisor at `at`."""
        from guards import session_auth as sa
        req = {"session": {"repo": REPO, "seat": "advisor",
                           "session_id": adv.session_id, "scope": "lanes 1-3"},
               "instruction": "x"}
        with mock.patch.object(sa, "session_request", return_value=(req, "")), \
                mock.patch.object(sa, "_now_s", return_value=at), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = sa.main(["--repo-root", str(self.repo), "grant-request",
                          "session.arm", "--scope", "lanes 1-3"])
        self.assertEqual(rc, 0)

    def ten_hours_of_rerequests(self, adv) -> "tuple[dict[int, list[str]], list[str]]":
        """10 hours, one sweep + dead-man check every 5 min, the advisor
        asking for an arm again every 100 min. The executor never moves.
        Returns ({minute: the executor's finding kinds}, the pages)."""
        pager = tw.Pager()
        kinds: dict[int, list[str]] = {}
        for minute in range(0, 601, 5):
            now = self.now + minute * 60
            if minute % 100 == 0:
                self.ask(adv, now)
            found = [f for f in watch.dark_sessions(15, now, runner=tw.HerdrStatus("idle"))
                     if f.subject == "executor"]
            kinds[minute] = [f.kind for f in found]
            deadman.escalate(found, "advisor", now, pager=pager,
                             evidence_fn=_no_evidence)
        return kinds, pager.calls

    def executor_holding_a_goal(self):
        adv, exe = self.held_for_arm()
        time.sleep(1.1)
        self.send(adv, exe, "/goal: build lane 1")
        self.pull(exe)  # the executor has the work and then goes silent
        return adv, exe

    def test_falsifier_rerequest_never_excuses_an_armed_executor(self) -> None:
        # The advisor holds a live token armed BEFORE the re-request, so the
        # token is no answer to it. The executor is armed, holds its goal and
        # is dead: it stays dark-session and pages, however often the advisor
        # asks again.
        adv, _exe = self.executor_holding_a_goal()
        self.token(adv, armed_at=self.now - 50 * 60)
        kinds, pages = self.ten_hours_of_rerequests(adv)
        self.assertEqual({m: k for m, k in kinds.items() if k != ["dark-session"]}, {})
        self.assertEqual(pages, ["dark-session:executor"])

    def test_falsifier_rerequests_do_not_restart_the_cap(self) -> None:
        # No token at all: the wait is real at first, but the cap counts from
        # the OLDEST unanswered request. Asking again every 100 min must not
        # hide a dead executor for the whole run.
        adv, _exe = self.executor_holding_a_goal()
        kinds, pages = self.ten_hours_of_rerequests(adv)
        waiting = [m for m, k in kinds.items() if k == ["waiting-on-counterpart"]]
        dark = [m for m, k in kinds.items() if k == ["dark-session"]]
        # (request stamps are whole seconds, so the cap minute itself is past it)
        self.assertEqual(waiting, list(range(0, 120, 5)))
        self.assertEqual(dark, list(range(120, 601, 5)))
        self.assertEqual(pages, ["dark-session:executor"])

    def test_marker_lists_every_unanswered_request_and_drops_answered_ones(self) -> None:
        from guards import session_auth as sa
        adv, _exe = self.held_for_arm()
        t = self.now
        self.ask(adv, t)
        self.ask(adv, t + 600)
        path = sa._arm_wait_path(adv.session_id, REPO)
        self.assertEqual(json.loads(path.read_text())["requests"],
                         [tw._iso(t), tw._iso(t + 600)])
        self.token(adv, armed_at=t + 900)  # the operator armed: both answered
        self.ask(adv, t + 1200)
        self.assertEqual(json.loads(path.read_text())["requests"], [tw._iso(t + 1200)])

    def test_falsifier_request_dated_in_the_future_never_excuses(self) -> None:
        # now - asked < 0 passed the old `> cap` check for as long as the
        # clock took to catch up.
        adv, _exe = self.held_for_arm()
        self.marker(adv, self.now + 30 * 24 * 3600)
        self.assertEqual(self.kinds("executor"), ["dark-session"])
        from net import waitstate
        entries = watch.who(prune=False)
        self.assertIsNone(waitstate.waiting_on_arm(adv, entries, self.now))

    def test_falsifier_marker_of_another_repo_never_matches(self) -> None:
        # The grants dir is machine-wide: the same session id in another repo
        # is another seat.
        from guards import session_auth as sa
        adv, _exe = self.held_for_arm()
        self.marker(adv, self.now - 5 * 60, repo="o/other")
        self.assertNotEqual(sa._arm_wait_path(adv.session_id, "o/other"),
                            sa._arm_wait_path(adv.session_id, REPO))
        self.assertEqual(self.kinds("executor"), ["dark-session"])
        self.marker(adv, self.now - 5 * 60)
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])

    def kinds_at(self, name: str, now: float) -> list[str]:
        return [f.kind for f in watch.dark_sessions(15, now, runner=tw.HerdrStatus("idle"))
                if f.subject == name]

    def test_arm_wait_excuse_lapses_after_the_cap(self) -> None:
        # A marker is a plain file: it must not hide a dark pair all run.
        from net import waitstate
        adv, _exe = self.held_for_arm()
        asked = self.now - 10 * 60
        self.marker(adv, asked)
        cap = waitstate.ARM_WAIT_MAX_MIN * 60
        self.assertEqual(waitstate.ARM_WAIT_MAX_MIN, 120)
        self.assertEqual(self.kinds_at("executor", asked + cap - 60),
                         ["waiting-on-counterpart"])
        self.assertEqual(self.kinds_at("executor", asked + cap + 60), ["dark-session"])

    def test_falsifier_marker_of_another_session_id_never_matches(self) -> None:
        adv, _exe = self.held_for_arm()
        path = self.grants / "requests" / f"{adv.session_id}.x.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"line": {"session_id": adv.session_id + ".x",
                                             "repo": REPO},
                                    "created_at": tw._iso(self.now - 5 * 60)}),
                        encoding="utf-8")
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_keyed_batch_record_is_read(self) -> None:
        # #338 keys the batch record `<sid>.<digest>.json`; the legacy
        # `<sid>.json` path no longer exists.
        adv, _exe = self.held_for_arm()
        path = self.grants / "requests" / f"{adv.session_id}.0123456789abcdef.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"line": {"session_id": adv.session_id, "repo": REPO},
                                    "created_at": tw._iso(self.now - 5 * 60)}),
                        encoding="utf-8")
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])

    def test_marker_does_not_depend_on_the_batch_record_path(self) -> None:
        # #338 changed `_request_path` to (repo, seat, sid). The marker builds
        # its own path, and a marker that cannot be written never breaks
        # grant-request.
        from guards import session_auth as sa
        adv, _exe = self.held_for_arm()
        req = {"session": {"repo": "o/r", "seat": "advisor", "session_id": adv.session_id,
                           "scope": "lanes 1-3"}, "instruction": "x"}

        def three_args(repo, seat, sid):  # the #338 signature
            raise AssertionError("the marker must not use the batch record path")

        with mock.patch.object(sa, "session_request", return_value=(req, "")), \
                mock.patch.object(sa, "_request_path", side_effect=three_args), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = sa.main(["--repo-root", str(self.repo), "grant-request",
                          "session.arm", "--scope", "lanes 1-3"])
        self.assertEqual(rc, 0)
        self.assertTrue(sa._arm_wait_path(adv.session_id, REPO).exists())
        with mock.patch.object(sa, "session_request", return_value=(req, "")), \
                mock.patch.object(sa, "_arm_wait_path", side_effect=RuntimeError("x")), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = sa.main(["--repo-root", str(self.repo), "grant-request",
                          "session.arm", "--scope", "lanes 1-3"])
        self.assertEqual(rc, 0)


class TestOwnSeatDark(ArmCase):
    """(b): a seat cannot be woken about its own silence; its peer owns that."""

    def ids_for(self, seat: str) -> list[str]:
        return sorted(f"{f.kind}:{f.subject}" for f in watch.findings_for_pair(
            watch.dark_sessions(15, self.now, runner=tw.HerdrStatus("idle")), seat,
            quiet_self=True)
            if f.kind not in watch.QUIET_KINDS)

    def test_fixture_own_dark_session_is_not_in_my_wake_set(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        self.assertNotIn("dark-session:executor", self.ids_for("executor"))
        # the peer's sweep still carries it
        self.assertIn("dark-session:executor", self.ids_for("advisor"))

    def test_falsifier_deadman_still_pages_my_own_dark_session(self) -> None:
        # The quieting is for WAKES only. The peer is registered and live, but
        # its monitor is down: the seat's own dead-man check is the only pager
        # left, so it keeps the raw finding and pages it, once per episode.
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        pager = tw.Pager()
        for minutes in (0, 11, 22):
            fs = watch.findings_for_pair(
                watch.dark_sessions(15, self.now + minutes * 60,
                                    runner=tw.HerdrStatus("idle")), "executor")
            deadman.escalate(fs, "executor", self.now + minutes * 60, pager=pager,
                             evidence_fn=_no_evidence)
        self.assertEqual(pager.calls.count("dark-session:executor"), 1)

    def test_deadman_check_cli_keeps_the_raw_finding(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        fs = watch.dark_sessions(15, self.now, runner=tw.HerdrStatus("idle"))
        seen: list = []

        def fake_escalate(findings, *_a, **_k):
            seen.extend(findings)
            return []

        with mock.patch.object(deadman, "sweep", return_value=fs), \
                mock.patch.object(deadman, "escalate", side_effect=fake_escalate):
            self.assertEqual(deadman.main(["check", "--for", "executor"]), 0)
        self.assertIn("dark-session:executor", {f"{f.kind}:{f.subject}" for f in seen})

    def test_watch_ids_only_quiets_my_own_dark_session(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(watch, "herdr_agent_status", return_value="idle"), \
                mock.patch.object(watch, "pane_exists", return_value=True), \
                mock.patch.object(watch, "_now_s", return_value=self.now):
            watch.main(["--for", "executor", "--ids-only"])
        self.assertNotIn("dark-session:executor", out.getvalue())
        self.assertIn("dark-session:advisor", out.getvalue())

    def test_falsifier_peer_sweep_still_pages_a_dark_seat(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        pager = tw.Pager()
        for minutes in (0, 11):
            fs = watch.findings_for_pair(
                watch.dark_sessions(15, self.now + minutes * 60,
                                    runner=tw.HerdrStatus("idle")), "advisor")
            deadman.escalate(fs, "advisor", self.now + minutes * 60, pager=pager,
                             evidence_fn=_no_evidence)
        self.assertIn("dark-session:executor", pager.calls)

    def test_falsifier_no_live_peer_keeps_my_own_finding(self) -> None:
        # Nobody else is watching: the seat's own loop is the only pager left.
        self.seat("executor", "executor")
        self.seat("advisor", "advisor", pid=tw._dead_pid())
        self.assertIn("dark-session:executor", self.ids_for("executor"))


class TestAckLifetime(unittest.TestCase):
    """(c): an ack that lapses must not page in the same poll; acks revoke."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.t0 = 1_800_000_000.0

    def esc(self, minute: int, pager) -> list[dict]:
        f = watch.Finding("blocked-seat", "advisor", 16 + minute, "operator prompt")
        return deadman.escalate([f], "executor", self.t0 + minute * 60, pager=pager,
                                evidence_fn=_no_evidence)

    def test_fixture_lapsed_ack_reopens_the_window_instead_of_paging(self) -> None:
        # peer-repo 10-02: acked at minute 1, the prompt sat 47 min, the ack
        # lapsed at minute 31 and the same poll paged the operator again.
        pager = tw.Pager()
        self.esc(0, pager)
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 60)
        for m in range(1, 41):
            self.esc(m, pager)
        self.assertEqual(pager.calls, [])

    def test_falsifier_lapsed_ack_still_pages_after_the_window(self) -> None:
        pager = tw.Pager()
        self.esc(0, pager)
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 60)
        for m in range(1, 42):
            self.esc(m, pager)
        self.assertEqual(pager.calls, ["blocked-seat:advisor"])

    def test_until_clear_ack_holds_for_the_whole_episode(self) -> None:
        pager = tw.Pager()
        self.esc(0, pager)
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 60,
                    until_clear=True)
        for m in range(1, 120):
            self.esc(m, pager)
        self.assertEqual(pager.calls, [])

    def test_falsifier_until_clear_ack_does_not_cover_the_next_episode(self) -> None:
        pager = tw.Pager()
        self.esc(0, pager)
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 60,
                    until_clear=True)
        later = self.t0 + 5 * 3600  # cleared, then a fresh stall
        f = watch.Finding("blocked-seat", "advisor", 16, "x")
        deadman.escalate([f], "executor", later, pager=pager, evidence_fn=_no_evidence)
        f2 = watch.Finding("blocked-seat", "advisor", 27, "x")
        deadman.escalate([f2], "executor", later + 660, pager=pager,
                         evidence_fn=_no_evidence)
        self.assertEqual(pager.calls, ["blocked-seat:advisor"])

    def test_falsifier_until_clear_ack_dies_when_the_finding_clears(self) -> None:
        # The review's simulation: acked until-clear at +2, the operator
        # answers the prompt at +10 (the finding clears; last_seen does not
        # move, so the age-derived episode never ends), the seat blocks again
        # at +40. One check a minute for 240 minutes, exactly as the loop runs
        # it: the second stall must wake the supervisor and page.
        pager = tw.Pager()
        paged_at: list[int] = []
        woke_at_40 = None
        for m in range(0, 241):
            now = self.t0 + m * 60
            present = m < 10 or m >= 40
            f = watch.Finding("blocked-seat", "advisor", 16 + m, "operator prompt")
            if m == 40:  # the wake step runs before the dead-man check
                with mock.patch.object(watch, "_now_s", return_value=now):
                    woke_at_40 = not watch._acked_now(f)
            if deadman.escalate([f] if present else [], "executor", now, pager=pager,
                                evidence_fn=_no_evidence):
                paged_at.append(m)
            if m == 2:
                deadman.ack("blocked-seat:advisor", "executor", now_s=now + 1,
                            until_clear=True)
        self.assertTrue(woke_at_40, "the returned stall is hidden by the dead ack")
        # the supervisor gets a full ack window on the new stall, then the page
        self.assertEqual(paged_at, [50])
        self.assertEqual(pager.calls, ["blocked-seat:advisor"])

    def test_until_clear_ack_can_be_given_again_after_a_clear(self) -> None:
        pager = tw.Pager()
        f = lambda m: [watch.Finding("blocked-seat", "advisor", 16 + m, "x")]  # noqa: E731
        esc = lambda m, fs: deadman.escalate(  # noqa: E731
            fs, "executor", self.t0 + m * 60, pager=pager, evidence_fn=_no_evidence)
        esc(0, f(0))
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 60, until_clear=True)
        esc(10, [])
        esc(40, f(40))
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 41 * 60,
                    until_clear=True)
        for m in range(42, 200):
            esc(m, f(m))
        self.assertEqual(pager.calls, [])
        marks = [r for r in deadman._read_ledger(deadman.ACK_LEDGER) if r.get("cleared")]
        self.assertEqual(len(marks), 1)  # one marker per ack, not one per poll

    def test_falsifier_a_check_never_clears_another_pairs_ack(self) -> None:
        deadman.ack("blocked-seat:their-advisor", "their-executor", now_s=self.t0,
                    until_clear=True)
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0, until_clear=True)
        self.assertEqual(
            deadman.note_cleared(set(), "executor", self.t0 + 60,
                                 subjects={"advisor", "executor"}),
            ["blocked-seat:advisor"])
        their = watch.Finding("blocked-seat", "their-advisor", 20, "x")
        self.assertTrue(deadman._acked_valid(
            "blocked-seat:their-advisor", self.t0 + 120,
            deadman.finding_episode_start(their, self.t0 + 120)))

    def test_check_cli_clears_within_its_own_pair(self) -> None:
        # The CLI checks at the real clock, so the acks are dated just before it
        # (self.t0 is a fixed date; ahead of the clock it would be voided, #379).
        acked = time.time() - 60
        deadman.ack("blocked-seat:advisor", "executor", now_s=acked, until_clear=True)
        deadman.ack("blocked-seat:elsewhere", "executor", now_s=acked, until_clear=True)
        with mock.patch.object(deadman, "sweep", return_value=[]), \
                mock.patch.object(deadman, "pair_subjects",
                                  return_value={"advisor", "executor"}):
            self.assertEqual(deadman.main(["check", "--for", "executor"]), 0)
        ledger = deadman._read_ledger(deadman.ACK_LEDGER)
        self.assertEqual([r["finding_id"] for r in ledger if r.get("cleared")],
                         ["blocked-seat:advisor"])
        self.assertEqual([r for r in ledger if r.get("void")], [])

    def test_check_cli_voids_a_future_dated_ack_within_its_own_pair(self) -> None:
        ahead = time.time() + 3600
        deadman.ack("blocked-seat:advisor", "executor", now_s=ahead, until_clear=True)
        deadman.ack("blocked-seat:elsewhere", "executor", now_s=ahead, until_clear=True)
        with mock.patch.object(deadman, "sweep", return_value=[]), \
                mock.patch.object(deadman, "pair_subjects",
                                  return_value={"advisor", "executor"}):
            self.assertEqual(deadman.main(["check", "--for", "executor"]), 0)
        ledger = deadman._read_ledger(deadman.ACK_LEDGER)
        self.assertEqual([r["finding_id"] for r in ledger if r.get("void")],
                         ["blocked-seat:advisor"])

    def test_revoke_cancels_an_ack(self) -> None:
        # peer-repo 10-02: acked before confirming real work, no un-ack.
        pager = tw.Pager()
        self.esc(0, pager)
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 60)
        self.assertEqual(self.esc(11, pager), [])
        deadman.revoke("blocked-seat:advisor", "executor", now_s=self.t0 + 12 * 60)
        recs = self.esc(12, pager)
        self.assertEqual([r["finding_id"] for r in recs], ["blocked-seat:advisor"])

    def test_cli_until_clear_and_revoke(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(deadman.main(["ack", "dark-session:x", "--as", "y",
                                           "--until-clear"]), 0)
            self.assertEqual(deadman.main(["ack", "dark-session:x", "--as", "y",
                                           "--revoke"]), 0)


class TestWakeLoop(ArmCase):
    """#346: `net.watch --wake` is the whole edge-triggered loop step."""

    def wake(self, seat: str = "executor", now: float | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(watch, "herdr_agent_status", return_value="idle"), \
                mock.patch.object(watch, "pane_exists", return_value=True), \
                mock.patch.object(watch, "_now_s", return_value=now or self.now):
            rc = watch.main(["--for", seat, "--wake"])
        return rc, out.getvalue()

    def two_findings(self) -> None:
        # A quiet pair with no artifact: both seats dark. From the advisor's
        # sweep both are in scope only if the advisor sees the executor; add a
        # parked wire for a second, independent finding the advisor owns.
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")

    def test_header_and_detail_arrive_in_one_output(self) -> None:
        self.two_findings()
        rc, out = self.wake("advisor")
        self.assertEqual(rc, 1)
        self.assertIn("PAIR-SWEEP", out)
        self.assertIn("[dark-session] executor", out)

    def test_fixture_rearm_does_not_refire_a_persisting_finding(self) -> None:
        # peer-repo 10-01/10-02: every Monitor re-arm started with prev_ids
        # empty, so an already-paged finding re-woke the seat.
        self.two_findings()
        self.assertEqual(self.wake("advisor")[0], 1)
        rc, out = self.wake("advisor", self.now + 60)  # a fresh process: re-arm
        self.assertEqual((rc, out), (0, ""))

    def test_fixture_acking_one_finding_does_not_refire_the_other(self) -> None:
        # peer-repo executor 09-01: acking ONE of two findings shrank the id set
        # and the loop re-echoed the OTHER, unchanged finding.
        self.two_findings()
        self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1",
                  tmux_pane=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(watch, "herdr_agent_status", return_value="blocked"), \
                mock.patch.object(watch, "pane_exists", return_value=True):
            self.assertEqual(watch.main(["--for", "advisor", "--wake"]), 1)
        self.assertIn("blocked-seat:advisor", out.getvalue())
        self.assertIn("dark-session:executor", out.getvalue())
        deadman.ack("dark-session:executor", "advisor")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(watch, "herdr_agent_status", return_value="blocked"), \
                mock.patch.object(watch, "pane_exists", return_value=True):
            rc = watch.main(["--for", "advisor", "--wake"])
        self.assertEqual((rc, out.getvalue()), (0, ""))

    def test_a_new_finding_wakes_and_names_only_the_new_id(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        self.wake("advisor")
        # the advisor's inbox now holds a parked wire turn as well
        wire = Path(os.environ["MAW_NET_DIR"]) / "wire" / "advisor"
        wire.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(watch, "parked_wires", return_value=[
                watch.Finding("parked-wire", "advisor", 12, "1 unconsumed turn(s)")]):
            rc, out = self.wake("advisor", self.now + 60)
        self.assertEqual(rc, 1)
        head = out.splitlines()[0]
        self.assertIn("parked-wire:advisor", head)
        self.assertNotIn("dark-session:executor", head)

    def test_a_cleared_finding_that_recurs_wakes_again(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        self.wake("advisor")
        with mock.patch.object(watch, "dark_sessions", return_value=[]):
            self.assertEqual(self.wake("advisor", self.now + 60), (0, ""))
        self.assertEqual(self.wake("advisor", self.now + 120)[0], 1)

    def test_falsifier_a_lapsed_ack_wakes_once(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        deadman.ack("dark-session:executor", "advisor", ttl_min=1,
                    now_s=self.now - 30)
        self.assertEqual(self.wake("advisor", self.now - 20), (0, ""))
        self.assertEqual(self.wake("advisor", self.now + 60)[0], 1)  # ack lapsed
        self.assertEqual(self.wake("advisor", self.now + 120), (0, ""))

    def test_falsifier_a_new_registration_rebaselines(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        self.wake("advisor")
        self.seat("advisor", "advisor", started_at=tw._iso(self.now - 600))
        self.assertEqual(self.wake("advisor", self.now + 60)[0], 1)

    def test_unreadable_seen_file_fails_toward_the_wake(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        self.wake("advisor")
        seen = Path(os.environ["MAW_NET_DIR"]) / "watch-seen" / "advisor.json"
        seen.write_bytes(b"\xff not json")
        self.assertEqual(self.wake("advisor", self.now + 60)[0], 1)


class TestSeatDocsUseTheWakeStep(unittest.TestCase):
    """The defects lived in the documented loop (sec 1e), so pin it."""

    ROOT = Path(__file__).resolve().parents[2] / "maw"

    def test_both_seat_commands_and_the_hook_use_wake(self) -> None:
        for rel in ("commands/executor.md", "commands/advisor.md",
                    "hooks/watchdog-arm.sh"):
            text = (self.ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("--wake", text, rel)
            self.assertNotIn("prev_ids", text, rel)
            self.assertNotIn("--ids-only", text, rel)

    def test_seat_commands_document_until_clear_and_revoke(self) -> None:
        for rel in ("commands/executor.md", "commands/advisor.md"):
            text = (self.ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("--until-clear", text, rel)
            self.assertIn("--revoke", text, rel)


if __name__ == "__main__":
    unittest.main()
