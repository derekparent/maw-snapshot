"""Behavioral tests for orchestrate.pulse — the advisor PULSE decision.

The contract under test is the page-or-quiet DECISION, not importability:

  * page=True on each trigger: a blocked-summary turn, a dark executor, a
    merge-pending gate event.
  * page=False when quiet (pending non-blocked turns + benign watch + non-gate
    new PRs do NOT page).
  * the PR cursor ADVANCES, so a PR is 'new' exactly once.

Hermetic: HOME is a tmpdir, the advisor inbox is a staged bridge file, watch is a
fake sweep, gh/git run through an injected runner. Nothing is consumed or merged.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from bridge.format import append_turn, make_turn  # noqa: E402
from net import registry, watch  # noqa: E402
from orchestrate import pulse  # noqa: E402


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def _adv_entry(repo_root: str) -> registry.Entry:
    now = "2026-06-17T00:00:00Z"
    return registry.Entry(
        name="advisor", role="advisor", repo_root=repo_root, cwd=repo_root,
        pid=os.getpid(), tty=None, tmux_pane="%1", session_id="advisor-1",
        started_at=now, last_seen=now, transport={"tmux_push": True},
        repo_common_dir=repo_root + "/.git",
    )


def _dark_finding(name: str = "executor") -> watch.Finding:
    return watch.Finding("dark-session", name, 42, "registered + pid alive but silent")


def _genuinely_dark_finding(name: str = "executor") -> watch.Finding:
    return watch.Finding(
        "genuinely-dark", name, 180,
        "silent 3h + no commits over 2h — likely abandoned (coarse)",
    )


class PulseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".maw").mkdir(parents=True)
        (self.repo / ".maw" / "config.yaml").write_text("repo_slug: orch-slug\n")
        # HOME governs both _resolve_repo_slug's bridge dir and the cursor file.
        # MAW_NET_DIR too: a seat exports it, and the sweep then read the LIVE
        # switchboard (an open net.question there flipped dark-session to
        # waiting-on-counterpart).
        env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                           "MAW_NET_DIR": str(self.home / "net")})
        env.start()
        self.addCleanup(env.stop)
        # The advisor reads from-executor.md under the shared slug.
        self.inbox = self.home / ".claude" / "bridge" / "orch-slug" / "from-executor.md"
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        # Resolve the advisor's repo_root via a faked registry (not a live one).
        self._who = mock.patch.object(
            pulse, "who", return_value=[_adv_entry(str(self.repo))]
        )
        self._who.start()
        self.addCleanup(self._who.stop)
        # Default: benign watch (no findings). Individual tests override.
        # Keep a handle to the REAL sweep so the end-to-end liveness tests can
        # drive it through the injected runner instead of the stub.
        self._real_sweep = watch.sweep
        self._sweep = mock.patch.object(watch, "sweep", return_value=[])
        self._sweep.start()
        self.addCleanup(self._sweep.stop)

    def _stage_turn(self, body: str, turn_id: int = 1) -> None:
        turn = make_turn(turn_id=turn_id, role="executor",
                         session_id="executor-1", body=body)
        append_turn(self.inbox, turn)

    def _gh_runner(self, prs: list[dict]):
        import json

        def runner(cmd):
            if cmd[:3] == ["gh", "pr", "list"]:
                return _cp(json.dumps(prs))
            if cmd[:2] == ["git", "rev-parse"]:
                return _cp(str(self.repo))
            return _cp("")

        return runner

    # --- page triggers ---------------------------------------------------

    def test_page_on_blocked_summary(self) -> None:
        self._stage_turn("BLOCKED: schema migration needs the operator's call\n"
                         "done: A, B\noutstanding: C\nrecommended next: decide schema")
        p = pulse.pulse(home=self.home, runner=self._gh_runner([]))
        self.assertTrue(p.page)
        self.assertTrue(p.inbox.blocked_summary)
        self.assertTrue(any("blocked-summary" in r for r in p.reasons))

    def test_page_on_genuinely_dark_executor(self) -> None:
        # genuinely-dark (hours-grained, commit-aware) IS a page trigger — the
        # real abandoned-seat signal.
        with mock.patch.object(
            watch, "sweep", return_value=[_genuinely_dark_finding()]
        ):
            p = pulse.pulse(home=self.home, runner=self._gh_runner([]))
        self.assertTrue(p.page)
        self.assertTrue(any("dark/frozen" in r for r in p.reasons))

    def test_page_on_orphaned_draft(self) -> None:
        # Operator ruling 2026-07-18: an orphaned /rc draft parks a pane and cost him
        # five rulings — it PAGES on its own (d) reason, not as dark/frozen.
        with mock.patch.object(
            watch, "sweep",
            return_value=[
                watch.Finding(
                    "orphaned-draft", "advisor", 22,
                    "2 box-non-empty park(s) from executor; input box has held an "
                    "unsent draft for 22 min — SURFACE ONLY",
                )
            ],
        ):
            p = pulse.pulse(home=self.home, runner=self._gh_runner([]))
        self.assertTrue(p.page)
        self.assertTrue(any("orphaned draft" in r for r in p.reasons))

    def test_bare_dark_session_does_not_page_synchronous_build_trap(self) -> None:
        # THE AWAY-MODE LIVENESS FALSE-PAGE TRAP (advisor_ios lived it): a live
        # executor mid synchronous build (xcodebuild/test) doesn't tick the
        # UserPromptSubmit heartbeat, so last_seen goes stale at DEFAULT_DARK_MIN
        # (15 min) of LEGITIMATE work and dark_sessions fires. Paging on that
        # cries wolf. A bare dark-session (NOT genuinely-dark) must NOT page —
        # it is surfaced for the advisor to reason on, never a page.
        with mock.patch.object(
            watch, "sweep",
            return_value=[
                watch.Finding(
                    "dark-session", "executor", 17,
                    "registered + pid alive but silent; pane %2 still exists",
                )
            ],
        ):
            p = pulse.pulse(home=self.home, runner=self._gh_runner([]))
        self.assertFalse(p.page)  # the trap is closed
        self.assertEqual(len(p.watch), 1)  # still surfaced, just not paged
        self.assertEqual(p.reasons, [])

    def test_page_on_merge_pending_gate_event(self) -> None:
        prs = [{"number": 40, "title": "feat: x", "headRefName": "feat/vetted"}]
        p = pulse.pulse(
            home=self.home,
            runner=self._gh_runner(prs),
            merge_pending_branches={"feat/vetted"},
        )
        self.assertTrue(p.page)
        self.assertTrue(any("merge-pending gate event" in r for r in p.reasons))

    # --- quiet -----------------------------------------------------------

    def test_quiet_when_nothing_triggers(self) -> None:
        # A pending but NON-blocked turn + benign watch + a new PR that is NOT on
        # a merge-pending branch => quiet (the loop reviews it but does not page).
        self._stage_turn("status: built feature X, opened PR #41, continuing")
        prs = [{"number": 41, "title": "feat: x", "headRefName": "feat/x"}]
        p = pulse.pulse(home=self.home, runner=self._gh_runner(prs))
        self.assertFalse(p.page)
        self.assertEqual(p.reasons, [])
        # The non-blocked turn is still surfaced (count>0), just not a page.
        self.assertEqual(p.inbox.count, 1)
        self.assertFalse(p.inbox.blocked_summary)
        self.assertEqual(len(p.prs.new), 1)

    def test_parked_wire_alone_does_not_page(self) -> None:
        # parked-wire is a finding but NOT a DARK_KIND — surfaced, not paged.
        with mock.patch.object(
            watch, "sweep",
            return_value=[watch.Finding("parked-wire", "executor", 12, "1 unconsumed")],
        ):
            p = pulse.pulse(home=self.home, runner=self._gh_runner([]))
        self.assertFalse(p.page)
        self.assertEqual(len(p.watch), 1)

    # --- cursor ----------------------------------------------------------

    def test_cursor_advances_so_pr_is_new_once(self) -> None:
        prs = [{"number": 50, "title": "feat: a", "headRefName": "feat/a"}]
        runner = self._gh_runner(prs)
        first = pulse.pulse(home=self.home, runner=runner)
        self.assertEqual([d["number"] for d in first.prs.new], [50])
        # Same open PR list on the next cycle: it is no longer NEW.
        second = pulse.pulse(home=self.home, runner=runner)
        self.assertEqual(second.prs.new, [])
        self.assertEqual(second.prs.cursor_advanced_to, 50)

    def test_no_advance_leaves_cursor(self) -> None:
        prs = [{"number": 60, "title": "feat: b", "headRefName": "feat/b"}]
        runner = self._gh_runner(prs)
        peek = pulse.pulse(home=self.home, runner=runner, advance_cursor=False)
        self.assertEqual([d["number"] for d in peek.prs.new], [60])
        # Cursor not written → still new on the next (advancing) cycle.
        real = pulse.pulse(home=self.home, runner=runner)
        self.assertEqual([d["number"] for d in real.prs.new], [60])

    def test_blocked_and_dark_both_named_in_reasons(self) -> None:
        self._stage_turn("BLOCKED: need credentials decision")
        with mock.patch.object(
            watch, "sweep", return_value=[_genuinely_dark_finding()]
        ):
            p = pulse.pulse(home=self.home, runner=self._gh_runner([]))
        self.assertTrue(p.page)
        self.assertGreaterEqual(len(p.reasons), 2)

    # --- end-to-end liveness wiring (drives the REAL watch.sweep, not a fake) --
    #
    # These prove pulse ARMS genuinely-dark (gather_watch passes the hours
    # threshold) AND that the repo-wide commit check suppresses it. They do NOT
    # mock watch.sweep — they mock who()/pane_exists and drive the injected
    # runner so git log / gh run through it. This is the regression that pins the
    # false-page trap closed at the wiring level, not just in _decide.

    def _liveness_entry(self, last_seen_min: int) -> watch.Entry:
        from net.registry import Entry
        ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - last_seen_min * 60),
        )
        return Entry(
            name="executor", role="executor", repo_root=str(self.repo),
            cwd=str(self.repo), pid=os.getpid(), tty=None, tmux_pane="%2",
            session_id="exec-1", started_at=ago, last_seen=ago,
            transport={"tmux_push": True}, repo_common_dir=str(self.repo) + "/.git",
        )

    def _liveness_runner(self, has_recent_commit: bool):
        import json

        def runner(cmd):
            if cmd[:3] == ["gh", "pr", "list"]:
                return _cp(json.dumps([]))
            if cmd[:2] == ["git", "rev-parse"]:
                return _cp(str(self.repo))
            if "log" in cmd:  # repo_has_recent_commits
                return _cp("abc123\n" if has_recent_commit else "")
            return _cp("")

        return runner

    def test_scenario_A_synchronous_build_with_recent_commit_no_page(self) -> None:
        # A) executor last_seen 17 min ago (synchronous build, no heartbeat) BUT a
        # recent commit exists on a seat branch within the window -> page False.
        entry = self._liveness_entry(17)
        with mock.patch.object(watch, "sweep", self._real_sweep), \
             mock.patch.object(watch, "who", return_value=[entry]), \
             mock.patch.object(watch, "pane_exists", return_value=True):
            p = pulse.pulse(
                home=self.home, runner=self._liveness_runner(has_recent_commit=True)
            )
        self.assertFalse(p.page)  # build in progress is NOT dark
        self.assertEqual(p.reasons, [])
        # dark-session surfaced; genuinely-dark suppressed by the recent commit.
        kinds = {f.kind for f in p.watch}
        self.assertIn("dark-session", kinds)
        self.assertNotIn("genuinely-dark", kinds)

    def test_scenario_B_hours_silent_no_commits_pages(self) -> None:
        # B) executor last_seen 3+ hours ago AND zero commits in the window ->
        # page True (genuinely abandoned — the real signal).
        entry = self._liveness_entry(200)  # 3h20m
        with mock.patch.object(watch, "sweep", self._real_sweep), \
             mock.patch.object(watch, "who", return_value=[entry]), \
             mock.patch.object(watch, "pane_exists", return_value=True):
            p = pulse.pulse(
                home=self.home, runner=self._liveness_runner(has_recent_commit=False)
            )
        self.assertTrue(p.page)
        self.assertTrue(any("genuinely-dark" in f.kind for f in p.watch))
        self.assertTrue(any("dark/frozen" in r for r in p.reasons))

    def test_scenario_C_early_build_no_commits_does_not_page(self) -> None:
        # C) executor last_seen 17 min ago, no commits yet (early in a build) ->
        # must NOT page either: 17 min is not hours, so genuinely-dark (2h) never
        # trips regardless of commit state.
        entry = self._liveness_entry(17)
        with mock.patch.object(watch, "sweep", self._real_sweep), \
             mock.patch.object(watch, "who", return_value=[entry]), \
             mock.patch.object(watch, "pane_exists", return_value=True):
            p = pulse.pulse(
                home=self.home, runner=self._liveness_runner(has_recent_commit=False)
            )
        self.assertFalse(p.page)
        self.assertEqual(p.reasons, [])
        self.assertNotIn("genuinely-dark", {f.kind for f in p.watch})

    def test_peek_does_not_consume_inbox(self) -> None:
        self._stage_turn("BLOCKED: x")
        pulse.pulse(home=self.home, runner=self._gh_runner([]))
        # Turn is still unconsumed after a pulse (peek-only).
        from bridge.format import find_unconsumed_oldest
        self.assertIsNotNone(find_unconsumed_oldest(self.inbox))

    def test_gh_failure_yields_empty_queue_not_crash(self) -> None:
        def failing(cmd):
            if cmd[:3] == ["gh", "pr", "list"]:
                return _cp("", returncode=1)
            return _cp(str(self.repo))

        p = pulse.pulse(home=self.home, runner=failing)
        self.assertEqual(p.prs.new, [])
        self.assertFalse(p.page)


if __name__ == "__main__":
    unittest.main()
