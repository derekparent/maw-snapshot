"""Tests for net.watch — dark-session and parked-wire detection.

Hermetic via MAW_NET_DIR (the registry/wire root override). Time is
injected through now_s; tmux is a recorded fake. The detection logic is
pure reads, so every scenario is a matter of staging files.
"""
from __future__ import annotations

import calendar
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from bridge.format import append_turn, make_turn, mark_consumed  # noqa: E402
from net import watch  # noqa: E402
from orchestrate import pulse  # noqa: E402
from net.registry import Entry, registry_dir  # noqa: E402
from net.wire import next_turn_id, record_delivery, wire_file  # noqa: E402


def _write_refusal(path, ts: str, kind: str, name: str) -> None:
    rec = {"ts": ts, "kind": kind, "name": name,
           "caller_pid": 1234, "owner_pid": 5678, "reason": "test"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


class FakeTmux:
    def __init__(self, panes: list[str]) -> None:
        self.panes = panes

    def __call__(self, cmd: list[str], stdin_text: str | None = None):
        if cmd[1] == "list-panes":
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(self.panes), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class WatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        # A deterministic "now": noon UTC as a true epoch (timegm, the
        # same UTC math the code under test uses).
        self.now_s = float(
            calendar.timegm(time.strptime("2026-06-11T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        )

    def ago(self, minutes: int) -> str:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now_s - minutes * 60)
        )

    def add_entry(
        self,
        name: str,
        last_seen: str,
        pane: str | None = "%1",
        role: str | None = None,
    ) -> None:
        entry = Entry(
            name=name,
            role=role if role is not None else name,
            repo_root="/repo",
            cwd="/repo",
            pid=os.getpid(),  # alive, so who(prune=True) keeps it
            tty="/dev/ttys001",
            tmux_pane=pane,
            session_id=f"{name}-x",
            started_at=last_seen,
            last_seen=last_seen,
            transport={"tmux_push": pane is not None},
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(
            json.dumps(entry.__dict__), encoding="utf-8"
        )


class TestDarkSessions(WatchTestCase):
    def test_silent_session_flagged(self) -> None:
        self.add_entry("advisor", self.ago(60))
        findings = watch.dark_sessions(15, self.now_s, runner=FakeTmux(["%1"]))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "dark-session")
        self.assertEqual(findings[0].subject, "advisor")
        self.assertGreaterEqual(findings[0].age_min, 59)
        self.assertIn("pane %1 still exists", findings[0].detail)

    def test_fresh_session_not_flagged(self) -> None:
        self.add_entry("advisor", self.ago(5))
        self.assertEqual(
            watch.dark_sessions(15, self.now_s, runner=FakeTmux(["%1"])), []
        )

    def test_gone_pane_named_in_detail(self) -> None:
        self.add_entry("advisor", self.ago(60), pane="%9")
        findings = watch.dark_sessions(15, self.now_s, runner=FakeTmux(["%1"]))
        self.assertIn("pane %9 is GONE", findings[0].detail)

    def test_unparseable_last_seen_fails_loud(self) -> None:
        self.add_entry("advisor", "not-a-timestamp")
        findings = watch.dark_sessions(15, self.now_s, runner=FakeTmux(["%1"]))
        self.assertEqual(len(findings), 1)
        self.assertIn("unparseable", findings[0].detail)


class FakeHerdr:
    """herdr `pane get` stand-in scripting one agent_status (None=unreadable)."""

    def __init__(self, status: str | None) -> None:
        self.status = status
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], stdin_text: str | None = None):
        self.calls.append(cmd)
        if cmd[:3] == ["herdr", "pane", "get"]:
            if self.status is None:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="x")
            body = json.dumps({"result": {"pane": {"agent_status": self.status}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestHerdrLiveness(WatchTestCase):
    """A3 — herdr status REFINES a silence-triggered dark finding; it never
    triggers one on its own (herdr detection is heuristic, #765)."""

    def add_herdr_entry(self, name: str, last_seen: str, pane: str = "2-1") -> None:
        entry = Entry(
            name=name, role=name, repo_root="/repo", cwd="/repo", pid=os.getpid(),
            tty=None, tmux_pane=None, session_id=f"{name}-x",
            started_at=last_seen, last_seen=last_seen,
            transport={"herdr_push": True}, mux="herdr", herdr_pane=pane,
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(json.dumps(entry.__dict__), encoding="utf-8")

    def test_working_suppresses_dark_finding(self) -> None:
        # silent 60 min but herdr says WORKING -> heads-down, not dark.
        self.add_herdr_entry("advisor", self.ago(60))
        findings = watch.dark_sessions(15, self.now_s, runner=FakeHerdr("working"))
        self.assertEqual(findings, [])

    def test_blocked_surfaces_as_blocked_seat(self) -> None:
        self.add_herdr_entry("advisor", self.ago(60))
        findings = watch.dark_sessions(15, self.now_s, runner=FakeHerdr("blocked"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "blocked-seat")
        self.assertIn("prompt is waiting", findings[0].detail)

    def test_blocked_seat_never_tells_a_seat_to_answer_the_prompt(self) -> None:
        # Lane D row 12 (live 2026-09-22 20:47Z and again overnight): the old
        # text said "service it" while the prompt was the operator's own arm consent.
        import re
        self.add_herdr_entry("advisor", self.ago(60))
        [f] = watch.dark_sessions(15, self.now_s, runner=FakeHerdr("blocked"))
        text = f.line().lower()
        for bad in ("service it", "servic", "answer it", "press enter", "approve it"):
            self.assertNotIn(bad, text)
        self.assertIsNone(re.search(r"(^|[.;:—-]\s*)(answer|service|approve|press|select)\b", text))
        self.assertIn("operator", text)
        self.assertIn("page the operator", text)
        self.assertIn("never answers", text)
        # The id is what acks and dedup key on; the wording change must not move it.
        self.assertEqual((f.kind, f.subject), ("blocked-seat", "advisor"))
        from net import deadman
        self.assertEqual(deadman.finding_id(f), "blocked-seat:advisor")

    def test_idle_but_silent_still_dark(self) -> None:
        # idle != working: an idle seat silent past the threshold is genuinely
        # dark (its heartbeat hook isn't firing). Only working suppresses.
        self.add_herdr_entry("advisor", self.ago(60))
        findings = watch.dark_sessions(15, self.now_s, runner=FakeHerdr("idle"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "dark-session")

    def test_herdr_unreadable_is_pre_a3_behavior(self) -> None:
        # herdr absent/unreadable -> the normal dark finding, unchanged.
        self.add_herdr_entry("advisor", self.ago(60))
        findings = watch.dark_sessions(15, self.now_s, runner=FakeHerdr(None))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "dark-session")

    def test_fresh_herdr_seat_never_consults_herdr(self) -> None:
        # under threshold -> no finding AND herdr is never queried (status
        # alone must never trigger anything).
        self.add_herdr_entry("advisor", self.ago(5))
        fake = FakeHerdr("blocked")
        self.assertEqual(watch.dark_sessions(15, self.now_s, runner=fake), [])
        self.assertFalse(any(c[:3] == ["herdr", "pane", "get"] for c in fake.calls))

    def test_tmux_seat_never_consults_herdr(self) -> None:
        self.add_entry("advisor", self.ago(60), pane="%1")
        fake = FakeTmux(["%1"])
        findings = watch.dark_sessions(15, self.now_s, runner=fake)
        self.assertEqual(findings[0].kind, "dark-session")


class TestParkedWires(WatchTestCase):
    def park(self, to: str = "advisor", sender: str = "executor") -> None:
        record_delivery(
            sender_name=sender,
            sender_session_id=f"{sender}-x",
            to_name=to,
            body="parked message",
            pushed=False,
            detail="input box not empty",
        )

    def test_old_unconsumed_turn_flagged(self) -> None:
        self.park()
        later = time.time() + 30 * 60
        findings = watch.parked_wires(10, later)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "parked-wire")
        self.assertEqual(findings[0].subject, "advisor")
        self.assertIn("from executor", findings[0].detail)

    def test_fresh_turn_not_flagged(self) -> None:
        self.park()
        self.assertEqual(watch.parked_wires(10, time.time()), [])

    def test_consumed_turn_not_flagged(self) -> None:
        self.park()
        file = wire_file("advisor", "executor")
        self.assertTrue(mark_consumed(file, 1))
        later = time.time() + 30 * 60
        self.assertEqual(watch.parked_wires(10, later), [])

    def test_multiple_turns_grouped_per_recipient(self) -> None:
        self.park()
        self.park()
        later = time.time() + 30 * 60
        findings = watch.parked_wires(10, later)
        self.assertEqual(len(findings), 1)
        self.assertIn("2 unconsumed turn(s)", findings[0].detail)


class TestDeadlockPairs(WatchTestCase):
    """#149 layer 2: both seats of a pair sitting on an unpulled durable inbox."""

    def setUp(self) -> None:
        super().setUp()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        p = mock.patch.dict(os.environ, {"HOME": self._home.name})
        p.start()
        self.addCleanup(p.stop)

    def _pair(self) -> None:
        self.add_entry("advisor", self.ago(60), role="advisor")
        self.add_entry("executor", self.ago(60), role="executor")

    def _durable(self, from_role: str, ts: str, turn_id: int = 1) -> Path:
        d = Path(self._home.name) / ".claude" / "bridge" / "repo"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"from-{from_role}.md"
        append_turn(f, make_turn(turn_id=turn_id, role=from_role,
                                 session_id=f"{from_role}-x", body="msg", ts=ts))
        return f

    def test_both_parked_old_fires(self) -> None:
        self._pair()
        self._durable("advisor", self.ago(30))   # executor's inbound, unconsumed, old
        self._durable("executor", self.ago(30))  # advisor's inbound, unconsumed, old
        findings = watch.deadlock_pairs(12, self.now_s)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "deadlock-both-parked")
        self.assertEqual(findings[0].subject, "repo")
        self.assertGreaterEqual(findings[0].age_min, 12)

    def test_one_sided_no_finding(self) -> None:
        self._pair()
        self._durable("advisor", self.ago(30))   # only the executor's inbox is parked
        self.assertEqual(watch.deadlock_pairs(12, self.now_s), [])

    def test_fresh_turns_no_finding(self) -> None:
        self._pair()
        self._durable("advisor", self.ago(2))    # both fresh -> in-flight, not stuck
        self._durable("executor", self.ago(2))
        self.assertEqual(watch.deadlock_pairs(12, self.now_s), [])

    def test_consumed_inbox_no_finding(self) -> None:
        self._pair()
        f_adv = self._durable("advisor", self.ago(30))
        self._durable("executor", self.ago(30))
        mark_consumed(f_adv, turn_id=1)          # executor PULLED its inbox
        self.assertEqual(watch.deadlock_pairs(12, self.now_s), [])

    def test_solo_seat_no_finding(self) -> None:
        # Only the executor is live; no pair -> no deadlock.
        self.add_entry("executor", self.ago(60), role="executor")
        self._durable("advisor", self.ago(30))
        self.assertEqual(watch.deadlock_pairs(12, self.now_s), [])

    def test_dead_seat_no_finding(self) -> None:
        # A dead-pid seat with an unpulled inbox is a dark/reaped case, not a
        # mutual wait — only live seats can deadlock.
        self.add_entry("advisor", self.ago(60), role="advisor")  # alive (os.getpid)
        self._write_entry_pid("executor", "executor", pid=999_999, ts=self.ago(60))
        self._durable("advisor", self.ago(30))
        self._durable("executor", self.ago(30))
        with mock.patch.object(watch, "pid_alive", side_effect=lambda p: p != 999_999):
            self.assertEqual(watch.deadlock_pairs(12, self.now_s), [])

    def _write_entry_pid(self, name: str, role: str, pid: int, ts: str) -> None:
        e = Entry(name=name, role=role, repo_root="/repo", cwd="/repo", pid=pid,
                  tty="/dev/ttys001", tmux_pane="%1", session_id=f"{name}-x",
                  started_at=ts, last_seen=ts, transport={"tmux_push": True})
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(json.dumps(e.__dict__), encoding="utf-8")

    def test_appears_in_sweep(self) -> None:
        self._pair()
        self._durable("advisor", self.ago(30))
        self._durable("executor", self.ago(30))
        kinds = {f.kind for f in watch.sweep(now_s=self.now_s)}
        self.assertIn("deadlock-both-parked", kinds)


class WorkingSeatRunner:
    """A runner answering both probes stuck_while_working makes: herdr `pane get`
    -> agent_status; `git log --since` -> a commit hash iff `has_commit`."""

    def __init__(self, status: str | None = "working", has_commit: bool = False) -> None:
        self.status = status
        self.has_commit = has_commit
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], stdin_text: str | None = None):
        self.calls.append(cmd)
        if cmd[:3] == ["herdr", "pane", "get"]:
            if not self.status:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="x")
            body = json.dumps({"result": {"pane": {"agent_status": self.status}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        if cmd[:2] == ["git", "-C"]:
            out = "deadbeefcafe\n" if self.has_commit else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestStuckWhileWorking(WatchTestCase):
    """#149/F10: a live seat that claims herdr status=working but shows no
    progress (stale heartbeat AND no recent commit) — the gizmo 8h stall."""

    def setUp(self) -> None:
        super().setUp()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        p = mock.patch.dict(os.environ, {"HOME": self._home.name})
        p.start()
        self.addCleanup(p.stop)

    def _herdr_seat(self, name: str, last_seen: str, *, role: str | None = None,
                    mux: str = "herdr", herdr_pane: str | None = "2-3") -> None:
        e = Entry(
            name=name, role=role if role is not None else name,
            repo_root="/repo", cwd="/repo", pid=os.getpid(),
            tty=None, tmux_pane="%1" if mux == "tmux" else None,
            session_id=f"{name}-x", started_at=last_seen, last_seen=last_seen,
            transport={"herdr_push": True}, mux=mux, herdr_pane=herdr_pane,
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(json.dumps(e.__dict__), encoding="utf-8")

    def test_working_no_progress_fires(self) -> None:
        self._herdr_seat("executor", self.ago(20), role="executor")
        findings = watch.stuck_while_working(15, self.now_s,
                                             runner=WorkingSeatRunner("working", has_commit=False))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "stuck-while-working")
        self.assertEqual(findings[0].subject, "executor")
        self.assertGreaterEqual(findings[0].age_min, 15)
        self.assertIn("pair repo", findings[0].detail)

    def test_fresh_heartbeat_no_fire(self) -> None:
        # A fresh heartbeat is progress — under threshold never fires.
        self._herdr_seat("executor", self.ago(5), role="executor")
        self.assertEqual(
            watch.stuck_while_working(15, self.now_s, runner=WorkingSeatRunner("working")), []
        )

    def test_recent_commit_disproves_stuck(self) -> None:
        # A productive seat with a fresh commit never fires, even silent + working.
        self._herdr_seat("executor", self.ago(20), role="executor")
        self.assertEqual(
            watch.stuck_while_working(15, self.now_s,
                                      runner=WorkingSeatRunner("working", has_commit=True)), []
        )

    def test_not_working_status_no_fire(self) -> None:
        # idle/blocked/unreadable are dark_sessions' territory, not this finding.
        self._herdr_seat("executor", self.ago(20), role="executor")
        for status in ("idle", "blocked", None):
            with self.subTest(status=status):
                self.assertEqual(
                    watch.stuck_while_working(15, self.now_s,
                                              runner=WorkingSeatRunner(status)), []
                )

    def test_dead_pid_no_fire(self) -> None:
        self._herdr_seat("executor", self.ago(20), role="executor")
        with mock.patch.object(watch, "pid_alive", return_value=False):
            self.assertEqual(
                watch.stuck_while_working(15, self.now_s, runner=WorkingSeatRunner("working")), []
            )

    def test_tmux_seat_no_fire(self) -> None:
        # Not herdr-mux: a tmux seat silent this long is a dark-session, not this.
        self._herdr_seat("executor", self.ago(20), role="executor", mux="tmux", herdr_pane=None)
        self.assertEqual(
            watch.stuck_while_working(15, self.now_s, runner=WorkingSeatRunner("working")), []
        )

    def test_non_bridge_role_no_fire(self) -> None:
        # A non-pair role (e.g. cos) is out of scope for this pair-scoped finding.
        self._herdr_seat("cos", self.ago(20), role="cos")
        self.assertEqual(
            watch.stuck_while_working(15, self.now_s, runner=WorkingSeatRunner("working")), []
        )

    def test_unparseable_heartbeat_fails_loud(self) -> None:
        self._herdr_seat("executor", "not-a-timestamp", role="executor")
        findings = watch.stuck_while_working(15, self.now_s, runner=WorkingSeatRunner("working"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "stuck-while-working")

    def test_appears_in_sweep(self) -> None:
        self._herdr_seat("executor", self.ago(20), role="executor")
        kinds = {f.kind for f in watch.sweep(now_s=self.now_s,
                                             runner=WorkingSeatRunner("working"))}
        self.assertIn("stuck-while-working", kinds)


class RealGitHerdrRunner:
    """Fakes herdr agent_status=working but runs REAL git for git commands, so the
    absolute --since cutoff is exercised against actual commit dates (the mocked
    WorkingSeatRunner ignores the --since value and so cannot catch a wall-clock
    vs injected-clock mismatch — the #181 defect)."""

    def __init__(self, status: str = "working") -> None:
        self.status = status

    def __call__(self, cmd: list[str], stdin_text: str | None = None):
        if cmd[:3] == ["herdr", "pane", "get"]:
            body = json.dumps({"result": {"pane": {"agent_status": self.status}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        return subprocess.run(cmd, capture_output=True, text=True)


class TestStuckWhileWorkingRealGit(WatchTestCase):
    """The absolute-cutoff regression (#181): the commit window must be evaluated
    against the injected now_s, not git's wall-clock 'N minutes ago'. All commits
    are backdated RELATIVE TO now_s; under the old wall-clock code every 2026-dated
    commit sorts before real-now-15min and the finding would fire even when a
    commit sits inside the window."""

    def setUp(self) -> None:
        super().setUp()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        p = mock.patch.dict(os.environ, {"HOME": self._home.name})
        p.start()
        self.addCleanup(p.stop)
        self._repo = tempfile.TemporaryDirectory()
        self.addCleanup(self._repo.cleanup)
        self.repo = self._repo.name
        self._git(["init", "-b", "main", self.repo])
        self._git(["-C", self.repo, "config", "user.name", "t"])
        self._git(["-C", self.repo, "config", "user.email", "t@t"])
        self._git(["-C", self.repo, "config", "commit.gpgsign", "false"])
        # A seed commit well OUTSIDE the window (now_s - 90 min) so it never on its
        # own clears the finding — the test's own commit is the only in-window one.
        self._commit_at(self.now_s - 90 * 60, "seed")

    def _git(self, args: list[str]) -> None:
        subprocess.run(["git", *args], capture_output=True, text=True, check=True)

    def _commit_at(self, epoch: float, msg: str) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(epoch))
        env = {**os.environ, "GIT_COMMITTER_DATE": ts, "GIT_AUTHOR_DATE": ts}
        subprocess.run(["git", "-C", self.repo, "commit", "--allow-empty", "-m", msg],
                       env=env, capture_output=True, text=True, check=True)

    def _seat(self) -> None:
        e = Entry(
            name="executor", role="executor", repo_root=self.repo, cwd=self.repo,
            pid=os.getpid(), tty=None, tmux_pane=None, session_id="executor-x",
            started_at=self.ago(20), last_seen=self.ago(20),
            transport={"herdr_push": True}, mux="herdr", herdr_pane="2-3",
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "executor.json").write_text(json.dumps(e.__dict__), encoding="utf-8")

    def test_commit_inside_window_clears(self) -> None:
        # A commit 5 min before now_s is INSIDE the 15-min window -> visible work
        # -> the finding must NOT fire. (This is the direction the old wall-clock
        # code got WRONG: a 2026 commit is never "within 15 minutes ago" of the
        # real clock, so it would have fired.)
        self._seat()
        self._commit_at(self.now_s - 5 * 60, "recent work")
        findings = watch.stuck_while_working(15, self.now_s, runner=RealGitHerdrRunner("working"))
        self.assertEqual(findings, [])

    def test_commit_outside_window_fires(self) -> None:
        # Latest commit 30 min before now_s is OUTSIDE the 15-min window -> no
        # visible progress -> the finding fires.
        self._seat()
        self._commit_at(self.now_s - 30 * 60, "stale work")
        findings = watch.stuck_while_working(15, self.now_s, runner=RealGitHerdrRunner("working"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "stuck-while-working")


class TestPairScoping(WatchTestCase):
    """Lane 2a: a per-pair watchdog scopes the sweep to its OWN pair (seat + bridge
    peer + slug), never the whole fleet's findings."""

    def setUp(self) -> None:
        super().setUp()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        p = mock.patch.dict(os.environ, {"HOME": self._home.name})
        p.start()
        self.addCleanup(p.stop)

    def _seat(self, name: str, role: str, repo_root: str) -> None:
        e = Entry(
            name=name, role=role, repo_root=repo_root, cwd=repo_root,
            pid=os.getpid(), tty=None, tmux_pane=None, session_id=f"{name}-x",
            started_at=self.ago(1), last_seen=self.ago(1),
            transport={"herdr_push": True}, mux="herdr", herdr_pane="1-1",
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(json.dumps(e.__dict__), encoding="utf-8")

    def test_pair_subjects_resolves_seat_peer_slug(self) -> None:
        self._seat("advisor", "advisor", "/repoA")
        self._seat("executor", "executor", "/repoA")
        self.assertEqual(watch.pair_subjects("advisor"), {"advisor", "executor", "repoA"})

    def test_pair_subjects_unknown_seat_fails_narrow(self) -> None:
        # An unknown seat resolves to just its own name — never widens to the fleet.
        self.assertEqual(watch.pair_subjects("ghost"), {"ghost"})

    def test_findings_for_pair_excludes_other_pair(self) -> None:
        self._seat("advisor", "advisor", "/repoA")
        self._seat("executor", "executor", "/repoA")
        self._seat("executor2", "executor", "/repoB")   # a different pair (slug repoB)
        findings = [
            watch.Finding("stuck-while-working", "executor", 20, "mine"),
            watch.Finding("stuck-while-working", "executor2", 20, "other pair"),
            watch.Finding("deadlock-both-parked", "repoA", 20, "my slug"),
            watch.Finding("deadlock-both-parked", "repoB", 20, "other slug"),
        ]
        kept = {f.subject for f in watch.findings_for_pair(findings, "advisor")}
        self.assertEqual(kept, {"executor", "repoA"})

    def test_cli_for_scopes_and_knob_parses(self) -> None:
        # The CLI --for + --stuck-working-min parse and run (empty registry -> 0).
        rc = watch.main(["--for", "advisor", "--stuck-working-min", "20", "--quiet-when-clear"])
        self.assertEqual(rc, 0)


class TestOrphanedDrafts(WatchTestCase):
    # The two real box-non-empty wordings the transports write into the ledger.
    HERDR = "push: none (input busy: box non-empty)"
    TMUX = (
        "advisor's input box is not empty (unsubmitted draft) — refusing to "
        "fuse; parked on wire for recv"
    )

    def stage(
        self,
        to: str,
        detail: str,
        minutes_ago: int,
        *,
        pushed: bool = False,
        sender: str = "executor",
    ) -> None:
        """Append a wire turn with a controlled ts + push header, faithful to
        record_delivery's on-disk audit-body format."""
        file = wire_file(to, sender)
        ts = self.ago(minutes_ago)
        audit_body = (
            f"to: {to}\n"
            f"push: {'pasted+enter' if pushed else 'none'} ({detail})\n"
            f"---\n"
            f"body @ {ts}"
        )
        turn = make_turn(
            turn_id=next_turn_id(file),
            role=sender,
            session_id=f"{sender}-x",
            body=audit_body,
            ts=ts,
            consumed_at=ts if pushed else None,
        )
        append_turn(file, turn)

    def test_stuck_herdr_draft_flagged(self) -> None:
        # Three box-non-empty parks over 25 min, latest still a park.
        self.stage("advisor", self.HERDR, 25)
        self.stage("advisor", self.HERDR, 18)
        self.stage("advisor", self.HERDR, 2)
        findings = watch.orphaned_drafts(15, self.now_s)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "orphaned-draft")
        self.assertEqual(findings[0].subject, "advisor")
        self.assertEqual(findings[0].age_min, 25)
        self.assertIn("3 box-non-empty park(s)", findings[0].detail)
        self.assertIn("SURFACE ONLY", findings[0].detail)

    def test_tmux_wording_detected(self) -> None:
        self.stage("advisor", self.TMUX, 30)
        self.stage("advisor", self.TMUX, 4)
        findings = watch.orphaned_drafts(15, self.now_s)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].subject, "advisor")

    def test_normal_submitted_prompt_not_flagged(self) -> None:
        # A prompt that landed (box was empty, text submitted) — the #110 verify
        # negative: a normally-submitted prompt must NOT be flagged.
        self.stage("advisor", "herdr: text confirmed in box + Enter sent",
                   3, pushed=True)
        self.assertEqual(watch.orphaned_drafts(15, self.now_s), [])

    def test_single_park_not_flagged(self) -> None:
        # One park is not "repeated" — could be a human mid-keystroke.
        self.stage("advisor", self.HERDR, 40)
        self.assertEqual(watch.orphaned_drafts(15, self.now_s), [])

    def test_recent_parks_below_threshold_not_flagged(self) -> None:
        # Two parks, but the oldest is only 8 min old — live typing, not orphaned.
        self.stage("advisor", self.HERDR, 8)
        self.stage("advisor", self.HERDR, 1)
        self.assertEqual(watch.orphaned_drafts(15, self.now_s), [])

    def test_cleared_draft_not_flagged(self) -> None:
        # Parks, then a successful push landed (draft gone) — latest is not a park.
        self.stage("advisor", self.HERDR, 40)
        self.stage("advisor", self.HERDR, 30)
        self.stage("advisor", "herdr: text confirmed in box + Enter sent",
                   2, pushed=True)
        self.assertEqual(watch.orphaned_drafts(15, self.now_s), [])

    def test_success_resets_streak(self) -> None:
        # Old parks, a success, then a single fresh park: the open region (after
        # the success) has only ONE park, so nothing is flagged yet.
        self.stage("advisor", self.HERDR, 60)
        self.stage("advisor", self.HERDR, 55)
        self.stage("advisor", "herdr: text confirmed in box + Enter sent",
                   40, pushed=True)
        self.stage("advisor", self.HERDR, 20)
        self.assertEqual(watch.orphaned_drafts(15, self.now_s), [])

    def test_peer_busy_latest_not_flagged(self) -> None:
        # A peer-busy park is push:none but NOT a draft marker; if it is the
        # latest delivery the pane is actively working, not orphaned.
        self.stage("advisor", self.HERDR, 40)
        self.stage("advisor", self.HERDR, 30)
        self.stage(
            "advisor",
            "push: none (peer busy mid-turn — durable turn holds it, not a failure)",
            2,
        )
        self.assertEqual(watch.orphaned_drafts(15, self.now_s), [])

    def test_per_recipient_grouping(self) -> None:
        # Two stuck panes surface as two distinct findings.
        self.stage("advisor", self.HERDR, 25)
        self.stage("advisor", self.HERDR, 3)
        self.stage("cos", self.HERDR, 25)
        self.stage("cos", self.HERDR, 3)
        findings = watch.orphaned_drafts(15, self.now_s)
        self.assertEqual({f.subject for f in findings}, {"advisor", "cos"})

    def test_folded_into_sweep(self) -> None:
        self.stage("advisor", self.HERDR, 25)
        self.stage("advisor", self.HERDR, 3)
        findings = watch.sweep(now_s=self.now_s, runner=FakeTmux([]))
        kinds = {f.kind for f in findings}
        self.assertIn("orphaned-draft", kinds)


class TestRenderAndCli(WatchTestCase):
    def test_render_all_clear(self) -> None:
        self.assertEqual(watch.render([]), "net.watch: all clear")

    def test_render_lists_findings(self) -> None:
        f = watch.Finding("dark-session", "advisor", 42, "silent")
        out = watch.render([f])
        self.assertIn("1 finding(s)", out)
        self.assertIn("[dark-session] advisor: silent (42 min)", out)

    def test_render_no_header_is_stable_across_calls(self) -> None:
        # #149 field note: the header's timestamp changes every sweep, so an
        # edge-trigger loop re-fires on identical findings. --no-header drops it,
        # keeping the dedup stable — the two renders must be byte-identical.
        f = watch.Finding("stuck-while-working", "executor", 20, "no progress")
        a = watch.render([f], header=False)
        b = watch.render([f], header=False)
        self.assertEqual(a, b)
        self.assertNotIn("net.watch:", a)
        self.assertIn("[stuck-while-working] executor", a)
        self.assertEqual(watch.render([], header=False), "")

    def test_cli_no_header_flag(self) -> None:
        # empty registry, clear -> both header and no-header print nothing at exit 0
        rc = watch.main(["--no-header", "--quiet-when-clear"])
        self.assertEqual(rc, 0)

    def test_ids_only_emits_stable_kind_subject_lines(self) -> None:
        # --ids-only is the loop's dedup KEY: sorted kind:subject, no '(N min)' age
        # (which increments every minute and would wake-storm a raw-line dedup).
        self.add_entry("advisor", self.ago(60))  # a dark session
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            watch.main(["--ids-only", "--quiet-when-clear"])
        out = buf.getvalue().strip()
        self.assertIn("dark-session:advisor", out)
        self.assertNotIn("min)", out)       # no age -> stable across sweeps
        self.assertNotIn("net.watch:", out)  # no header either

    def test_main_exit_codes(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_clear = watch.main([])
        self.assertEqual(rc_clear, 0)
        self.assertIn("all clear", buf.getvalue())

        self.park_for_main()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_finding = watch.main(["--parked-min", "0", "--dark-min", "99999"])
        # a just-written turn has age 0, which is not > 0 — so backdate it
        # via the dark path instead: use an old registry entry.
        self.add_entry("advisor", "2026-06-11T00:00:00Z")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_finding = watch.main(["--dark-min", "1"])
        self.assertEqual(rc_finding, 1)
        self.assertIn("dark-session", buf.getvalue())

    def park_for_main(self) -> None:
        record_delivery(
            sender_name="executor",
            sender_session_id="executor-x",
            to_name="advisor",
            body="x",
            pushed=False,
            detail="parked",
        )

    def test_quiet_when_clear(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = watch.main(["--quiet-when-clear"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")


class TestNotifyImessage(unittest.TestCase):
    def test_text_and_handle_travel_as_argv_data(self) -> None:
        calls: list[list[str]] = []

        def runner(cmd, stdin_text=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        hostile = 'alert"; do shell script "rm -rf ~"; "'
        ok = watch.notify_imessage(hostile, "+15551234567", runner=runner)
        self.assertTrue(ok)
        cmd = calls[0]
        self.assertEqual(cmd[0], "osascript")
        # message + handle are argv items consumed via `on run argv`,
        # never interpolated into the script source
        self.assertIn(hostile, cmd)
        self.assertIn("+15551234567", cmd)
        self.assertNotIn(hostile, cmd[2])

    def test_failure_returns_false(self) -> None:
        def runner(cmd, stdin_text=None):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no buddy")

        self.assertFalse(watch.notify_imessage("hi", "nobody", runner=runner))




class RefusedHeartbeatTest(WatchTestCase):
    """Third detection: heartbeat_lineage refusal streams (PR #18 audit
    artifact), thresholded so a stray record is noise and a stream alarms."""

    def _refusals_path(self):
        from net.registry import registry_refusals_file
        return registry_refusals_file()

    def _stage(self, count: int, ago_min: int, kind: str = "heartbeat_lineage",
               name: str = "executor") -> None:
        for _ in range(count):
            _write_refusal(self._refusals_path(), self.ago(ago_min), kind, name)

    def test_no_file_no_findings(self) -> None:
        self.assertEqual(watch.refused_heartbeats(now_s=self.now_s), [])

    def test_below_threshold_is_noise(self) -> None:
        self._stage(2, ago_min=5)
        self.assertEqual(
            watch.refused_heartbeats(min_count=3, now_s=self.now_s), []
        )

    def test_stream_at_threshold_alarms(self) -> None:
        self._stage(3, ago_min=5)
        findings = watch.refused_heartbeats(min_count=3, now_s=self.now_s)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.kind, "refused-heartbeat")
        self.assertEqual(f.subject, "executor")
        self.assertIn("3 lineage-refused heartbeat(s)", f.detail)
        self.assertIn("env/seat mismatch", f.detail)

    def test_old_refusals_outside_window_ignored(self) -> None:
        self._stage(5, ago_min=45)  # window default 30
        self.assertEqual(
            watch.refused_heartbeats(window_min=30, min_count=3,
                                     now_s=self.now_s),
            [],
        )

    def test_register_collisions_not_surfaced(self) -> None:
        self._stage(5, ago_min=5, kind="register_name_collision")
        self.assertEqual(
            watch.refused_heartbeats(min_count=3, now_s=self.now_s), []
        )

    def test_names_grouped_independently(self) -> None:
        self._stage(3, ago_min=5, name="executor")
        self._stage(2, ago_min=5, name="advisor")  # below threshold
        findings = watch.refused_heartbeats(min_count=3, now_s=self.now_s)
        self.assertEqual([f.subject for f in findings], ["executor"])

    def test_malformed_lines_skipped(self) -> None:
        path = self._refusals_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        self._stage(3, ago_min=5)
        findings = watch.refused_heartbeats(min_count=3, now_s=self.now_s)
        self.assertEqual(len(findings), 1)

    def test_age_is_most_recent_refusal(self) -> None:
        self._stage(2, ago_min=20)
        self._stage(1, ago_min=4)
        findings = watch.refused_heartbeats(min_count=3, now_s=self.now_s)
        self.assertEqual(findings[0].age_min, 4)

    def test_sweep_includes_refusals(self) -> None:
        self._stage(3, ago_min=5)
        findings = watch.sweep(now_s=self.now_s, runner=FakeTmux([]))
        self.assertIn("refused-heartbeat", [f.kind for f in findings])


def _commit_runner(has_commits: bool, error: bool = False):
    def run(cmd, stdin_text=None):
        if error:
            raise OSError("git unavailable")
        if len(cmd) > 1 and cmd[1] == "-C":  # git -C <repo> log ...
            rc = 0
            return subprocess.CompletedProcess(cmd, rc, stdout=("deadbeef\n" if has_commits else ""), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return run


class TestGenuinelyDark(WatchTestCase):
    """Coarse, git-progress-aware darkness (item 9), opt-in."""

    def test_disabled_by_default(self) -> None:
        self.add_entry("advisor", self.ago(7 * 60))
        self.assertEqual(watch.genuinely_dark_sessions(0, now_s=self.now_s), [])

    def test_flags_silent_with_no_commits(self) -> None:
        self.add_entry("advisor", self.ago(7 * 60))  # 7h silent
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False))
        self.assertEqual([f.kind for f in findings], ["genuinely-dark"])

    def test_suppressed_by_recent_commits(self) -> None:
        self.add_entry("advisor", self.ago(7 * 60))
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(True))
        self.assertEqual(findings, [])  # progress in the repo → not dark

    def test_not_silent_long_enough(self) -> None:
        self.add_entry("advisor", self.ago(60))  # only 1h
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False))
        self.assertEqual(findings, [])

    def test_git_error_flags(self) -> None:
        # can't confirm progress → an hours-silent seat fails toward flagging
        self.add_entry("advisor", self.ago(7 * 60))
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False, error=True))
        self.assertEqual([f.kind for f in findings], ["genuinely-dark"])

    def test_repo_has_recent_commits_helper(self) -> None:
        self.assertTrue(watch.repo_has_recent_commits("/r", 6, _commit_runner(True)))
        self.assertFalse(watch.repo_has_recent_commits("/r", 6, _commit_runner(False)))
        self.assertFalse(watch.repo_has_recent_commits("/r", 6, _commit_runner(False, error=True)))

    def test_progress_check_is_repo_wide_across_local_branches(self) -> None:
        # Per-seat worktrees (backlog item 1): each seat's repo_root is its own
        # linked worktree on its own seat/<role> branch. The progress signal
        # MUST be repo-wide across LOCAL branches (--branches) or an idle advisor
        # would be false-flagged genuinely-dark while the executor commits on
        # another branch. --branches (not --all) so remote-tracking refs/tags
        # can't keep a fetched-but-idle seat falsely 'alive'. Assert the actual
        # git invocation carries --branches and NOT --all (behavior, not presence).
        captured: list[list[str]] = []

        def capturing_runner(cmd, stdin_text=None):
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        watch.repo_has_recent_commits("/seat-worktree", 6, capturing_runner)
        self.assertEqual(len(captured), 1)
        self.assertIn("--branches", captured[0])
        self.assertNotIn("--all", captured[0])
        # And it targets the seat's own worktree path (the registered repo_root).
        self.assertIn("/seat-worktree", captured[0])

    def test_sweep_armed_includes_genuinely_dark(self) -> None:
        self.add_entry("advisor", self.ago(7 * 60))
        findings = watch.sweep(now_s=self.now_s, runner=_commit_runner(False), genuinely_dark_hours=6)
        self.assertIn("genuinely-dark", [f.kind for f in findings])

    def test_sweep_off_excludes_genuinely_dark(self) -> None:
        self.add_entry("advisor", self.ago(7 * 60))
        findings = watch.sweep(now_s=self.now_s, runner=_commit_runner(False), genuinely_dark_hours=0)
        self.assertNotIn("genuinely-dark", [f.kind for f in findings])


class TestWatcherRoleExemption(WatchTestCase):
    """MAW-PULSE-COS: a watcher role is quiet by design, so commit cadence
    cannot mean 'abandoned' for it. It reclassifies to a non-paging record."""

    def test_cos_reclassified_not_genuinely_dark(self) -> None:
        # The live false page: cos silent 4h with no commits in homebase.
        self.add_entry("cos", self.ago(7 * 60))
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False))
        self.assertEqual([f.kind for f in findings], ["watcher-quiet"])

    def test_watcher_quiet_is_not_a_page_kind(self) -> None:
        # The whole point: pulse must not page on it. Guards the page path.
        self.assertNotIn("watcher-quiet", pulse.DARK_KINDS)

    def test_exemption_leaves_a_record(self) -> None:
        # Gate #8 — a suppressed page that leaves no trace cannot be proven.
        self.add_entry("cos", self.ago(7 * 60))
        finding = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False))[0]
        self.assertEqual(finding.subject, "cos")
        self.assertIn("watcher role", finding.detail)

    def test_executor_still_genuinely_dark(self) -> None:
        # THE regression to protect: a real dark executor must still page.
        self.add_entry("executor", self.ago(7 * 60))
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False))
        self.assertEqual([f.kind for f in findings], ["genuinely-dark"])

    def test_unknown_role_fails_toward_paging(self) -> None:
        # A misspelled/unknown role must NOT inherit the exemption.
        self.add_entry("cos-typo", self.ago(7 * 60), role="coss")
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False))
        self.assertEqual([f.kind for f in findings], ["genuinely-dark"])

    def test_keys_on_role_not_name(self) -> None:
        # A seat NAMED cos but holding the executor role is an executor.
        self.add_entry("cos", self.ago(7 * 60), role="executor")
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(False))
        self.assertEqual([f.kind for f in findings], ["genuinely-dark"])

    def test_commits_still_suppress_entirely_for_a_watcher(self) -> None:
        # A committing watcher is plainly alive — no finding at all, not even a record.
        self.add_entry("cos", self.ago(7 * 60))
        findings = watch.genuinely_dark_sessions(6, now_s=self.now_s, runner=_commit_runner(True))
        self.assertEqual(findings, [])


class TestReapDead(WatchTestCase):
    """Explicit, reported reaping of confirmed-dead seats (item 8)."""

    def _add(self, name: str, pid: int) -> None:
        entry = Entry(
            name=name, role=name, repo_root="/r", cwd="/r", pid=pid,
            tty=None, tmux_pane=None, session_id=f"{name}-x",
            started_at=self.ago(1), last_seen=self.ago(1), transport={},
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(json.dumps(entry.__dict__), encoding="utf-8")

    def test_reaps_dead_keeps_live(self) -> None:
        from net.registry import load_entry
        self._add("zombie", 2**22 + 1)   # no such pid → dead
        self._add("alive", os.getpid())  # this process → alive
        reaped = watch.reap_dead()
        self.assertEqual(reaped, ["zombie"])
        self.assertIsNone(load_entry("zombie"))
        self.assertIsNotNone(load_entry("alive"))

    def test_no_dead_returns_empty(self) -> None:
        self._add("alive", os.getpid())
        self.assertEqual(watch.reap_dead(), [])

    def test_main_reports_reaped_without_alarming(self) -> None:
        self._add("zombie", 2**22 + 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = watch.main(["--quiet-when-clear"])
        self.assertIn("reaped 1 dead seat(s): zombie", out.getvalue())
        self.assertEqual(rc, 0)  # reaping is cleanup, not an alarm


class TestModelFlip(WatchTestCase):
    """B3 — surface a seat whose live model diverges from the pinned model."""

    def add_model_entry(self, name: str, model: str, cwd: str = "/repo") -> None:
        entry = Entry(
            name=name, role=name, repo_root=cwd, cwd=cwd, pid=os.getpid(),
            tty=None, tmux_pane="%1", session_id=f"{name}-x",
            started_at=self.ago(1), last_seen=self.ago(1),
            transport={"tmux_push": True}, model=model,
        )
        rdir = registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{name}.json").write_text(json.dumps(entry.__dict__), encoding="utf-8")

    def test_flip_surfaces_and_names_both_values(self) -> None:
        self.add_model_entry("advisor", "claude-fable-5")
        # the contract is now PERSISTENCE: the last MODEL_FLIP_CONSECUTIVE
        # main-loop records must agree (#188), so the fixture supplies a run
        findings = watch.model_flip_sessions(
            live_models_fn=lambda e: ["claude-opus-4-8"] * 3)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "model-flip")
        self.assertIn("pinned claude-fable-5", findings[0].detail)
        self.assertIn("live claude-opus-4-8", findings[0].detail)

    def test_same_family_is_not_a_flip(self) -> None:
        self.add_model_entry("executor", "claude-opus-4-8")
        findings = watch.model_flip_sessions(
            live_models_fn=lambda e: ["claude-opus-4-8[1m]"] * 3
        )
        self.assertEqual(findings, [])

    def test_unpinned_seat_never_checked(self) -> None:
        self.add_model_entry("bare", "")
        called = {"n": 0}

        def fake(e):
            called["n"] += 1
            return ["whatever"] * 3

        self.assertEqual(watch.model_flip_sessions(live_models_fn=fake), [])
        self.assertEqual(called["n"], 0)  # no pinned expectation -> never checks live

    def test_unknown_live_model_fails_open(self) -> None:
        self.add_model_entry("advisor", "claude-fable-5")
        self.assertEqual(
            watch.model_flip_sessions(live_models_fn=lambda e: None), []
        )

    def test_is_flip_helper(self) -> None:
        self.assertTrue(watch._is_flip("claude-fable-5", "claude-opus-4-8"))
        self.assertFalse(watch._is_flip("claude-opus-4-8", "claude-opus-4-8[1m]"))
        self.assertFalse(watch._is_flip("claude-fable-5", "CLAUDE-FABLE-5"))

    def test_opus_alias_intent_matches_live_opus(self) -> None:
        # 2026-09-22 pairing default: the launcher pins the ALIAS 'opus' and the
        # transcript records the resolved id, often with a context suffix.
        self.assertFalse(watch._is_flip("opus", "claude-opus-5-5[1m]"))
        self.assertFalse(watch._is_flip("opus", "claude-opus-5-5"))
        self.assertFalse(watch._is_flip("fable", "claude-fable-5-1"))
        self.assertTrue(watch._is_flip("opus", "claude-sonnet-5"))
        self.assertTrue(watch._is_flip("opus", "claude-fable-5-1"))
        self.assertTrue(watch._is_flip("fable", "claude-opus-5-5[1m]"))

    def test_opus_seats_on_opus_raise_no_finding(self) -> None:
        self.add_model_entry("advisor", "opus")
        self.add_model_entry("executor", "opus")
        live = lambda e: ["claude-opus-5-5[1m]"] * 3  # noqa: E731
        self.assertEqual(watch.model_flip_sessions(live_models_fn=live), [])

    def test_opus_seat_falling_back_to_sonnet_is_a_flip(self) -> None:
        self.add_model_entry("advisor", "opus")
        live = lambda e: ["claude-sonnet-5"] * 3  # noqa: E731
        [f] = watch.model_flip_sessions(live_models_fn=live)
        self.assertIn("pinned opus", f.detail)

    def test_live_model_reads_freshest_transcript(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        proj = root / "-x-y"  # cwd "/x/y" encoded
        proj.mkdir(parents=True)
        (proj / "old.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"model": "claude-fable-5"}}) + "\n"
        )
        new = proj / "new.jsonl"
        new.write_text(
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}}) + "\n"
        )
        os.utime(new, (time.time() + 10, time.time() + 10))  # freshest wins
        entry = Entry(
            name="a", role="a", repo_root="/x/y", cwd="/x/y", pid=1, tty=None,
            tmux_pane=None, session_id="s", started_at="t", last_seen="t",
        )
        self.assertEqual(watch.live_model(entry, projects_root=root), "claude-opus-4-8")

    def test_live_model_missing_dir_is_none(self) -> None:
        entry = Entry(
            name="a", role="a", repo_root="/no/such", cwd="/no/such", pid=1, tty=None,
            tmux_pane=None, session_id="s", started_at="t", last_seen="t",
        )
        self.assertIsNone(watch.live_model(entry, projects_root=Path("/nonexistent")))

    def test_sidechain_newer_record_does_not_flip(self) -> None:
        # CR1 (through watch.live_model -> bridge.livemodel): a subagent record
        # NEWER than the main-loop one must not be read as the seat's model, or
        # a seat that just ran a haiku subagent fires a phantom flip.
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        proj = root / "-x-y"
        proj.mkdir(parents=True)
        (proj / "t.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}})
            + "\n"
            + json.dumps(
                {"type": "assistant", "isSidechain": True,
                 "message": {"model": "claude-haiku-4-5"}}
            )
            + "\n",
            encoding="utf-8",
        )
        entry = Entry(
            name="a", role="a", repo_root="/x/y", cwd="/x/y", pid=1, tty=None,
            tmux_pane=None, session_id="s", started_at="t", last_seen="t",
        )
        self.assertEqual(watch.live_model(entry, projects_root=root), "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
