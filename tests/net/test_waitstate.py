"""T2 — supervision false positives (Lane D P1, P2, P6).

Fixtures are the live ones: on 2026-10-01 ~02:30Z the dead-man escalated
(a) blocked-seat:advisor while the advisor held the operator's own arm-consent
pop-up, and (b) dark-session:executor while the executor sat idle by design
waiting for the advisor's /maw:goal. Both escalated in the same poll that first
surfaced them, seconds before the supervisor's ack.

Every suppression here has a FALSIFIER test beside it: the fixture that would
prove the suppression too broad (a seat holding the ball, a dead counterpart, a
predecessor's artifact, an executor with only a fresh transcript).

Bridge turns are written with the real `bridgelink.write_durable_turn` and
consumed with the real `consume_inbound`; questions with the real
`question.ask` / `question.decide`. Hermetic: MAW_NET_DIR and HOME are temp dirs.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from net import bridgelink, deadman, question, waitstate, watch
from net.registry import Entry, registry_dir
from net.liveness import Evidence
from net.transport import NET_MSG_BEGIN
from net.watch import Finding


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class HerdrStatus:
    """A runner answering `herdr pane get` with a fixed agent_status and every
    other command (git log for the commit check) with empty success."""

    def __init__(self, status: str) -> None:
        self.status = status

    def __call__(self, cmd: list[str], stdin_text: str | None = None):
        if cmd[:3] == ["herdr", "pane", "get"]:
            body = json.dumps({"result": {"pane": {"agent_status": self.status}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        if cmd[:2] == ["tmux", "list-panes"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="%1\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class PairCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "home").mkdir()
        self.repo = root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"MAW_NET_DIR": str(root / "net"),
                                           "HOME": str(root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()
        # Both seats registered an hour ago and went quiet 20 min ago (past the
        # 15-min dark threshold).
        self.started = _iso(self.now - 3600)
        self.quiet = _iso(self.now - 20 * 60)

    def seat(self, name: str, role: str, *, pid: int | None = None,
             started_at: str | None = None, mux: str = "tmux",
             herdr_pane: str | None = None, transcript: str = "",
             tmux_pane: str | None = "%1") -> Entry:
        e = Entry(
            name=name, role=role, repo_root=str(self.repo), cwd=str(self.repo),
            pid=pid if pid is not None else os.getpid(), tty="/dev/ttys001",
            tmux_pane=tmux_pane, session_id=f"{name}-sess",
            started_at=started_at or self.started, last_seen=self.quiet,
            transport={"tmux_push": False}, mux=mux, herdr_pane=herdr_pane,
            repo_common_dir=str(self.repo / ".git"), transcript_path=transcript,
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(json.dumps(e.__dict__), encoding="utf-8")
        return e

    def send(self, sender: Entry, recipient: Entry, body: str = "hello"):
        link = bridgelink.eligible_pair(sender, recipient)
        assert link is not None
        return bridgelink.write_durable_turn(link, sender, body)

    def pull(self, recipient: Entry) -> None:
        got = bridgelink.next_inbound(recipient)
        assert got is not None
        bridgelink.consume_inbound(*got)

    def kinds(self, name: str) -> list[str]:
        return [f.kind for f in watch.dark_sessions(15, self.now, runner=HerdrStatus("idle"))
                if f.subject == name]


class TestWaitingOnPeer(PairCase):
    def test_fixture_b_executor_idle_after_grounding_ping_is_waiting(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv, "grounded at main, standing by for /goal")
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])
        f = next(f for f in watch.dark_sessions(15, self.now, runner=HerdrStatus("idle"))
                 if f.subject == "executor")
        self.assertIn("waiting on advisor", f.detail)

    def test_waiting_after_the_peer_pulled_the_turn(self) -> None:
        # The advisor consumed the ping and is busy with the operator: the next
        # move is still the advisor's.
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv)
        self.pull(adv)
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])

    def test_falsifier_seat_holding_the_ball_is_dark(self) -> None:
        # The advisor sent the /maw:goal LAST and the executor consumed it, then went
        # quiet: that is the executor's move, so it is dark, however it waited
        # before.
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv, "standing by")
        self.pull(adv)
        time.sleep(1.1)  # turn ts has 1s resolution; the /maw:goal must be later
        self.send(adv, exe, "/goal ...")
        self.pull(exe)
        self.assertEqual(self.kinds("executor"), ["dark-session"])
        # and the advisor, whose turn is now newest, is the one waiting
        self.assertEqual(self.kinds("advisor"), ["waiting-on-counterpart"])

    def test_falsifier_unconsumed_inbound_is_dark(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv)
        self.send(adv, exe)  # never pulled
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_older_unpulled_inbound_is_dark(self) -> None:
        # The seat spoke last but never pulled an EARLIER peer turn: that turn
        # is its move, whatever the timestamps say.
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(adv, exe, "/goal ...")
        time.sleep(1.1)
        self.send(exe, adv, "standing by")
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_dead_peer_in_the_entries_is_not_a_counterpart(self) -> None:
        # who(prune=True) drops a dead pid before dark_sessions sees it; the wait
        # check must not rely on that, so feed it an unpruned dead peer directly.
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv)
        dead = self.seat("advisor", "advisor", pid=_dead_pid())
        self.assertIsNone(waitstate.waiting_on_peer(exe, [exe, dead]))
        self.assertIsNotNone(waitstate.waiting_on_peer(exe, [exe, adv]))

    def test_falsifier_same_second_is_not_waiting(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        t = self.send(exe, adv)
        u = self.send(adv, exe)
        self.pull(exe)
        if t.ts == u.ts:
            self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_dead_counterpart_is_dark(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv)
        self.seat("advisor", "advisor", pid=_dead_pid())  # the advisor died
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_predecessor_turn_does_not_excuse_a_new_seat(self) -> None:
        adv = self.seat("advisor", "advisor")
        old = self.seat("executor", "executor")
        self.send(old, adv)
        # A new executor registers AFTER that turn and says nothing.
        self.seat("executor", "executor", started_at=_iso(self.now + 60))
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_no_turn_at_all_is_dark(self) -> None:
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_unreadable_bridge_is_no_evidence(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv)
        with mock.patch.object(bridgelink, "sent_durable", side_effect=OSError("boom")):
            self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_open_question_never_masks_an_unpulled_turn(self) -> None:
        # Advisor review blocker on #293: an open question was checked first and
        # excused a seat sitting on its peer's review request.
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        question.ask("advisor", "q1", "release sync?", ["a", "b"], "a", ["nothing"],
                     "operator-only", to="cos", sender=lambda to, s, body: (True, "test"))
        self.send(exe, adv, "REVIEW REQUEST PR #1")  # never pulled
        self.assertEqual(self.kinds("advisor"), ["dark-session"])

    def test_falsifier_gone_pane_is_dark(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor", tmux_pane="%9")  # not listed
        self.send(exe, adv)
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_push_deaf_seat_is_dark(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor", tmux_pane=None)
        self.send(exe, adv)
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_herdr_idle_or_done_can_wait_unreadable_cannot(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor", mux="herdr", herdr_pane="w1:p2",
                        tmux_pane=None)
        self.send(exe, adv)
        for status, want in (("idle", "waiting-on-counterpart"),
                             ("done", "waiting-on-counterpart"), ("", "dark-session")):
            got = [f.kind for f in watch.dark_sessions(15, self.now, runner=HerdrStatus(status))
                   if f.subject == "executor"]
            self.assertEqual(got, [want], status)

    def test_herdr_blocked_never_reaches_the_wait_check(self) -> None:
        # blocked-seat is a prompt in a pane, maybe the operator's: the peer
        # must still be told (fixture a). Its noise is fixed by the dead-man's
        # ack window, not by hiding it.
        adv = self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")
        exe = self.seat("executor", "executor")
        self.send(adv, exe)
        got = [f.kind for f in watch.dark_sessions(15, self.now, runner=HerdrStatus("blocked"))
               if f.subject == "advisor"]
        self.assertEqual(got, ["blocked-seat"])


class TestWaitingOnOperator(PairCase):
    def _ask(self, seat: str, qid: str = "q1") -> None:
        question.ask(seat, qid, "which way?", ["a", "b"], "a", ["lane-2"],
                     "within-goal", to="cos", sender=lambda to, s, body: (True, "test"))

    def _answer(self, seat: str, qid: str = "q1") -> None:
        text = "\n".join([NET_MSG_BEGIN, "from: cos (session=x)", "---", "ANSWER",
                          f"id: {qid}", "decision: a", "source: cos-within-goal"])
        verdict, reason, _ = question.decide(seat, text)
        self.assertEqual(verdict, "act", reason)

    def test_open_question_is_waiting(self) -> None:
        self.seat("executor", "executor")
        self._ask("executor")
        self.assertEqual(self.kinds("executor"), ["waiting-on-counterpart"])

    def test_falsifier_answered_question_is_dark(self) -> None:
        self.seat("executor", "executor")
        self._ask("executor")
        self._answer("executor")
        self.assertEqual(self.kinds("executor"), ["dark-session"])

    def test_falsifier_question_from_before_registration_is_dark(self) -> None:
        self.seat("executor", "executor")
        self._ask("executor")
        self.seat("executor", "executor", started_at=_iso(self.now + 60))
        self.assertEqual(self.kinds("executor"), ["dark-session"])


class TestWatchCli(PairCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(watch, "herdr_agent_status", return_value="idle"), \
                mock.patch.object(watch, "pane_exists", return_value=True):
            rc = watch.main(list(args))
        return rc, out.getvalue()

    def test_waiting_is_rendered_but_never_wakes(self) -> None:
        adv = self.seat("advisor", "advisor")
        exe = self.seat("executor", "executor")
        self.send(exe, adv)
        # the advisor holds the ball and is quiet: the only alarm is ITS
        rc, ids = self.run_main("--for", "executor", "--ids-only", "--quiet-when-clear")
        self.assertEqual(ids.split(), ["dark-session:advisor"])
        self.assertEqual(rc, 1)
        rc, full = self.run_main("--for", "executor", "--no-header")
        self.assertIn("[waiting-on-counterpart] executor", full)

    def test_hide_acked_drops_an_acked_stall_from_the_wake_set(self) -> None:
        # P6: an acked finding re-echoed every time another finding changed the
        # id set. With --hide-acked it stays out of the set until the ack expires.
        # Seen from the advisor's sweep: its own dark-session is a self-quiet
        # record (#345), so the executor's is the stall it supervises; a second
        # finding (parked wire) stands in for "some OTHER finding".
        self.seat("advisor", "advisor")
        self.seat("executor", "executor")
        other = [Finding("parked-wire", "advisor", 12, "1 unconsumed turn(s)")]
        with mock.patch.object(watch, "parked_wires", return_value=other):
            _, before = self.run_main("--for", "advisor", "--ids-only", "--hide-acked")
            self.assertIn("dark-session:executor", before.split())
            deadman.ack("dark-session:executor", "advisor")
            _, after = self.run_main("--for", "advisor", "--ids-only", "--hide-acked")
            self.assertNotIn("dark-session:executor", after.split())
            self.assertIn("parked-wire:advisor", after.split())
            _, unfiltered = self.run_main("--for", "advisor", "--ids-only")
            self.assertIn("dark-session:executor", unfiltered.split())

    def test_hide_acked_honours_ack_expiry(self) -> None:
        self.seat("executor", "executor")
        deadman.ack("dark-session:executor", "executor", ttl_min=1,
                    now_s=time.time() - 120)
        _, out = self.run_main("--for", "executor", "--ids-only", "--hide-acked")
        self.assertIn("dark-session:executor", out.split())


class Pager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, f: Finding, seat: str):
        self.calls.append(deadman.finding_id(f))
        return True, "test"


class TestDeadmanAckWindow(unittest.TestCase):
    """The dead-man's clock starts when a sweep FIRST SEES the finding."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.t0 = 1_800_000_000.0

    def esc(self, age: int, at: float, pager: Pager) -> list[dict]:
        f = Finding("blocked-seat", "advisor", age, "prompt waiting")
        return deadman.escalate([f], "executor", at, escalate_min=10, pager=pager,
                                evidence_fn=lambda s: (_ for _ in ()).throw(AssertionError))

    def test_fixture_a_first_sight_never_pages(self) -> None:
        # Surfaced at 16 min (past the old 10-min stall-age bar): no page.
        pager = Pager()
        self.assertEqual(self.esc(16, self.t0, pager), [])
        self.assertEqual(pager.calls, [])

    def test_fixture_a_ack_inside_the_window_means_no_page(self) -> None:
        pager = Pager()
        self.esc(16, self.t0, pager)
        deadman.ack("blocked-seat:advisor", "executor", now_s=self.t0 + 30)
        for m in range(1, 25):
            self.esc(16 + m, self.t0 + m * 60, pager)
        self.assertEqual(pager.calls, [])

    def test_falsifier_unacked_stall_pages_after_the_window(self) -> None:
        pager = Pager()
        self.esc(16, self.t0, pager)
        self.assertEqual(self.esc(25, self.t0 + 9 * 60, pager), [])
        recs = self.esc(26, self.t0 + 10 * 60, pager)
        self.assertEqual([r["finding_id"] for r in recs], ["blocked-seat:advisor"])
        self.assertEqual(self.esc(27, self.t0 + 11 * 60, pager), [])  # once per episode
        self.assertEqual(pager.calls, ["blocked-seat:advisor"])

    def test_a_new_episode_restarts_the_clock(self) -> None:
        pager = Pager()
        self.esc(16, self.t0, pager)
        self.esc(26, self.t0 + 10 * 60, pager)
        # cleared, then a fresh stall surfaces 2h later
        later = self.t0 + 7200
        self.assertEqual(self.esc(16, later, pager), [])
        self.assertEqual(len(self.esc(26, later + 600, pager)), 1)

    def test_unwritable_ledger_falls_back_to_stall_age(self) -> None:
        # Advisor note on #293: a ledger we cannot write must fail toward the
        # page (the old stall-age rule), not crash or restart the clock.
        pager = Pager()
        real = deadman._append_ledger

        def append(name: str, rec: dict) -> None:
            if name == deadman.FIRST_SEEN_LEDGER:
                raise OSError("read-only")
            real(name, rec)

        with mock.patch.object(deadman, "_append_ledger", append):
            recs = self.esc(16, self.t0, pager)
        self.assertEqual(len(recs), 1)

    def test_unreadable_ledger_falls_back_to_stall_age(self) -> None:
        pager = Pager()
        # Reads fail, writes succeed: the clock must not restart every sweep.
        self.esc(16, self.t0, pager)  # records a sighting
        real = Path.read_text

        def read_text(path: Path, *a, **k) -> str:
            if path.name == deadman.FIRST_SEEN_LEDGER:
                raise PermissionError("unreadable")
            return real(path, *a, **k)

        with mock.patch.object(Path, "read_text", read_text):
            recs = self.esc(17, self.t0 + 60, pager)
        self.assertEqual(len(recs), 1)

    def test_non_object_and_non_utf8_ledger_lines_never_crash(self) -> None:
        # Advisor round-2 note on #293: a valid-JSON non-object line crashed the
        # check (silence under the loop's 2>/dev/null).
        pager = Pager()
        path = Path(self._tmp.name) / deadman.FIRST_SEEN_LEDGER
        path.write_text('5\n[1]\n"x"\nnull\n', encoding="utf-8")
        self.esc(16, self.t0, pager)
        self.assertEqual(len(self.esc(26, self.t0 + 600, pager)), 1)
        path.write_bytes(b"\xff\xfe not utf-8\n")
        self.assertEqual(len(self.esc(16, self.t0 + 7200, pager)), 1)  # stall-age fallback

    def test_first_seen_is_shared_between_both_seats(self) -> None:
        # Both seats run the check; whichever sees it first starts the clock.
        pager = Pager()
        none = lambda s: Evidence(None, "none")  # noqa: E731 — no proof of life
        deadman.escalate([Finding("dark-session", "executor", 16, "x")], "advisor",
                         self.t0, escalate_min=10, pager=pager, evidence_fn=none)
        recs = deadman.escalate([Finding("dark-session", "executor", 26, "x")],
                                "executor", self.t0 + 600, escalate_min=10,
                                pager=pager, evidence_fn=none)
        self.assertEqual(len(recs), 1)


class TestStuckAdvisorNeverCommits(PairCase):
    """P1: stuck-while-working fired on every long advisor turn (no commits)."""

    def stuck(self) -> list[str]:
        return [f.subject for f in watch.stuck_while_working(15, self.now,
                                                             runner=HerdrStatus("working"))]

    def test_falsifier_silent_working_advisor_is_still_stuck(self) -> None:
        self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")
        self.assertEqual(self.stuck(), ["advisor"])

    def test_advisor_that_sent_a_turn_recently_is_not_stuck(self) -> None:
        adv = self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")
        exe = self.seat("executor", "executor")
        self.send(adv, exe, "verdict: approved")
        self.assertEqual(self.stuck(), [])

    def test_advisor_with_its_own_fresh_transcript_is_not_stuck(self) -> None:
        tr = Path(self._tmp.name) / "adv.jsonl"
        tr.write_text("{}\n", encoding="utf-8")
        self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1",
                  transcript=str(tr))
        self.assertEqual(self.stuck(), [])

    def test_falsifier_advisor_with_a_stale_transcript_is_stuck(self) -> None:
        tr = Path(self._tmp.name) / "adv.jsonl"
        tr.write_text("{}\n", encoding="utf-8")
        old = self.now - 40 * 60
        os.utime(tr, (old, old))
        self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1",
                  transcript=str(tr))
        self.assertEqual(self.stuck(), ["advisor"])

    def test_falsifier_executor_fresh_transcript_alone_is_still_stuck(self) -> None:
        # The executor keeps the commit rule (the 2026-07-21 stall class).
        tr = Path(self._tmp.name) / "exe.jsonl"
        tr.write_text("{}\n", encoding="utf-8")
        self.seat("executor", "executor", mux="herdr", herdr_pane="w1:p2",
                  transcript=str(tr))
        self.assertEqual(self.stuck(), ["executor"])

    def test_falsifier_old_turn_does_not_clear_stuck(self) -> None:
        adv = self.seat("advisor", "advisor", mux="herdr", herdr_pane="w1:p1")
        exe = self.seat("executor", "executor")
        self.send(adv, exe)
        later = self.now + 40 * 60
        got = [f.subject for f in watch.stuck_while_working(15, later,
                                                            runner=HerdrStatus("working"))]
        self.assertIn("advisor", got)


if __name__ == "__main__":
    unittest.main()
