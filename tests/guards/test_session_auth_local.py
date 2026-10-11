"""`--local-branch` mode — the audited merge rail for no-remote repos (#146-1).

Two notes-mcp arcs (2026-07-18, 2026-07-20) merged OUTSIDE the wrapper entirely —
nine merges as bare `git merge --ff-only`, no recorded verdicts, no audit trail —
because merge/check/record-verdict are gh-PR-bound and that repo has no remote.
The operator approved building this (issue #146): that repo is an internal project with no remote.

THE LOAD-BEARING PROPERTY IS THE REFUSAL, not the merge. On a repo with a remote,
branch protection is the guarantee; a local mode that ran there would be a way
around it. So local mode REFUSES the moment any remote exists, and every merge it
does perform is stamped `local-unprotected` by the code — the honesty that says
this rail has no branch protection under it.

These tests use a REAL throwaway git repo: the thing under test is git state
(a fast-forward, a ref that moved, a remote's existence), and a mocked runner
would assert my model of git rather than git.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import session_auth  # noqa: E402


def _git(root: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(root), *args],
                        capture_output=True, text=True)
    if cp.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {cp.stderr}")
    return cp.stdout.strip()


def _yes(_prompt: str) -> bool:
    return True


class LocalModeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.now = time.time()
        self.sid = "sess-local"
        self.pid = os.getpid()

        _git(self.root, "init", "-q", "-b", "main", ".")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "t")
        (self.root / "f.txt").write_text("one\n", encoding="utf-8")
        _git(self.root, "add", "f.txt")
        _git(self.root, "commit", "-q", "-m", "one")
        self.main_sha = _git(self.root, "rev-parse", "HEAD")

    def feature(self, name: str = "feature/x", text: str = "two\n") -> str:
        """A branch one commit ahead of main; leaves main checked out."""
        _git(self.root, "checkout", "-q", "-b", name)
        (self.root / "f.txt").write_text(text, encoding="utf-8")
        _git(self.root, "commit", "-q", "-am", "two")
        sha = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "-q", "main")
        return sha

    def add_remote(self) -> None:
        _git(self.root, "remote", "add", "origin", "git@github.com:x/y.git")

    def arm(self) -> None:
        ok, msg = session_auth.arm(
            "merge the notes-mcp local arc", self.root, seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"}, session_id=self.sid,
            pid=self.pid, now_s=self.now, gate_fn=_yes)
        self.assertTrue(ok, msg)

    def sign(self, branch: str = "feature/x", verdict: str = "approved",
             expect_sha: str | None = None):
        return session_auth.record_verdict_local(
            self.root, branch, verdict, seat="advisor", now_s=self.now,
            expect_sha=expect_sha)

    def check(self, branch: str = "feature/x"):
        return session_auth.check_local(
            branch, "notes-mcp", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now)

    def audit_records(self) -> list[dict]:
        path = self.root / session_auth.AUDIT_REL
        if not path.is_file():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


class TestRefusesWhereProtectionExists(LocalModeTestCase):
    def test_record_verdict_local_refuses_when_a_remote_exists(self) -> None:
        self.feature()
        self.add_remote()
        ok, msg = self.sign()
        self.assertFalse(ok)
        self.assertIn("remote", msg.lower())

    def test_check_local_refuses_when_a_remote_exists(self) -> None:
        self.feature()
        self.arm()
        self.sign()
        self.add_remote()
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "remote-exists")

    def test_merge_local_refuses_when_a_remote_exists(self) -> None:
        sha = self.feature()
        self.arm()
        self.sign()
        self.add_remote()
        ok, msg = session_auth.merge_local(
            "feature/x", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("remote", msg.lower())
        self.assertEqual(_git(self.root, "rev-parse", "main"), self.main_sha)
        self.assertNotEqual(_git(self.root, "rev-parse", "main"), sha)


class TestVerdictBinding(LocalModeTestCase):
    def test_a_local_verdict_binds_the_branch_head_sha(self) -> None:
        sha = self.feature()
        ok, msg = self.sign()
        self.assertTrue(ok, msg)
        rec = session_auth.load_verdict_local(self.root, "feature/x")
        self.assertEqual(rec["sha"], sha)
        self.assertEqual(rec["local_branch"], "feature/x")
        self.assertEqual(rec["protection"], session_auth.LOCAL_TAG)

    def test_expect_sha_mismatch_refuses(self) -> None:
        self.feature()
        ok, msg = self.sign(expect_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.assertFalse(ok)
        self.assertIn("expect-sha", msg)

    def test_an_unknown_branch_cannot_be_signed(self) -> None:
        ok, msg = self.sign(branch="feature/nope")
        self.assertFalse(ok)
        self.assertIn("feature/nope", msg)

    def test_a_local_verdict_does_not_authorize_a_PR(self) -> None:
        """Cross-authorization isolation: the two ledgers share a file, so a
        local verdict must never be readable as a PR verdict or vice versa."""
        self.feature()
        self.sign()
        self.assertIsNone(session_auth.load_verdict(self.root, 1))

    def test_a_PR_verdict_does_not_authorize_a_LOCAL_branch(self) -> None:
        path = self.root / session_auth.VERDICT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "now", "pr": 1, "sha": self.main_sha,
                                "verdict": "approved", "seat": "advisor"}) + "\n")
        self.assertIsNone(session_auth.load_verdict_local(self.root, "feature/x"))


class TestCheckLocalBounds(LocalModeTestCase):
    def test_the_happy_path_authorizes(self) -> None:
        self.feature()
        self.arm()
        self.sign()
        ok, refusal = self.check()
        self.assertTrue(ok, refusal.line() if refusal else "")

    def test_no_verdict_refuses(self) -> None:
        self.feature()
        self.arm()
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "no-verdict")

    def test_a_non_approved_verdict_refuses(self) -> None:
        self.feature()
        self.arm()
        self.sign(verdict="changes-requested")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "not-approved")

    def test_sha_drift_after_the_verdict_refuses(self) -> None:
        """The executor pushes; the verdict must not follow the code."""
        self.feature()
        self.arm()
        self.sign()
        _git(self.root, "checkout", "-q", "feature/x")
        (self.root / "f.txt").write_text("three\n", encoding="utf-8")
        _git(self.root, "commit", "-q", "-am", "three")
        _git(self.root, "checkout", "-q", "main")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "sha-drift")

    def test_no_token_refuses(self) -> None:
        self.feature()
        self.sign()
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "no-token")

    def test_a_token_from_another_session_refuses(self) -> None:
        self.feature()
        self.arm()
        self.sign()
        ok, refusal = session_auth.check_local(
            "feature/x", "notes-mcp", self.root, session_id="someone-else",
            pid=self.pid, now_s=self.now)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "wrong-session")

    def test_the_six_hour_cap_still_applies(self) -> None:
        self.feature()
        self.arm()
        self.sign()
        ok, refusal = session_auth.check_local(
            "feature/x", "notes-mcp", self.root, session_id=self.sid,
            pid=self.pid, now_s=self.now + (session_auth.MAX_TOKEN_AGE_H + 1) * 3600)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "cap-reached")


class TestLocalReviewerRole(LocalModeTestCase):
    """L0 #3, local rail: same reviewer-role rule at record and at consume."""

    def test_a_non_reviewer_seat_cannot_record_a_local_verdict(self) -> None:
        self.feature()
        ok, msg = session_auth.record_verdict_local(
            self.root, "feature/x", "approved", seat="executor", now_s=self.now)
        self.assertFalse(ok, msg)
        self.assertIsNone(session_auth.load_verdict_local(self.root, "feature/x"))

    def test_an_executor_local_verdict_does_not_authorize(self) -> None:
        sha = self.feature()
        self.arm()
        path = self.root / session_auth.VERDICT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "ts": "2026-09-25T00:00:00Z", "local_branch": "feature/x", "sha": sha,
            "verdict": "approved", "seat": "executor",
            "protection": session_auth.LOCAL_TAG}) + "\n", encoding="utf-8")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "verdict-not-reviewer")


class TestLocalRailHasNoPrSet(LocalModeTestCase):
    """Review F1 on #254: a PR set means nothing on the no-remote rail."""

    def test_arm_refuses_a_pr_set_with_no_repo(self) -> None:
        ok, msg = session_auth.arm(
            "merge the notes-mcp local arc", self.root, seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"}, session_id=self.sid,
            pid=self.pid, now_s=self.now, gate_fn=_yes, prs=[5])
        self.assertFalse(ok, msg)
        self.assertIn("local", msg)

    def test_a_token_carrying_a_pr_set_refuses_on_the_local_rail(self) -> None:
        self.feature()
        self.arm()
        self.sign()
        path = self.root / session_auth.TOKEN_REL
        tok = json.loads(path.read_text())
        tok["prs"] = [5]
        path.write_text(json.dumps(tok))
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "out-of-scope")


class TestMergeLocal(LocalModeTestCase):
    def _armed_signed(self) -> str:
        sha = self.feature()
        self.arm()
        self.sign()
        return sha

    def merge(self, branch: str = "feature/x", **kw):
        return session_auth.merge_local(
            branch, self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, **kw)

    def test_a_fast_forward_merge_lands_and_is_audited(self) -> None:
        sha = self._armed_signed()
        ok, msg = self.merge()
        self.assertTrue(ok, msg)
        self.assertEqual(_git(self.root, "rev-parse", "main"), sha)
        merges = [r for r in self.audit_records() if r.get("event") == "merge"]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["local_branch"], "feature/x")
        self.assertEqual(merges[0]["protection"], session_auth.LOCAL_TAG)

    def test_the_unprotected_tag_is_written_by_the_code_not_the_caller(self) -> None:
        """The tag is load-bearing honesty: branch protection does not exist on
        this rail, and no flag may drop the stamp."""
        self._armed_signed()
        ok, msg = self.merge()
        self.assertTrue(ok, msg)
        self.assertIn(session_auth.LOCAL_TAG, msg)
        with self.assertRaises(TypeError):
            self.merge(protection="")  # no such parameter exists

    def test_a_refused_check_aborts_before_anything_mutating(self) -> None:
        self.feature()
        self.arm()  # armed but never signed
        ok, msg = self.merge()
        self.assertFalse(ok)
        self.assertEqual(_git(self.root, "rev-parse", "main"), self.main_sha)
        self.assertEqual([r for r in self.audit_records() if r.get("event") == "merge"], [])

    def test_a_non_fast_forward_refuses(self) -> None:
        """main moved after the branch forked: --ff-only would fail, and the
        wrapper must refuse it as a refusal, not as a git error after the fact."""
        self._armed_signed()
        (self.root / "other.txt").write_text("x\n", encoding="utf-8")
        _git(self.root, "add", "other.txt")
        _git(self.root, "commit", "-q", "-m", "main moved")
        moved = _git(self.root, "rev-parse", "main")
        ok, msg = self.merge()
        self.assertFalse(ok)
        self.assertIn("fast-forward", msg.lower())
        self.assertEqual(_git(self.root, "rev-parse", "main"), moved)

    def test_a_branch_that_moves_after_check_does_not_merge_unreviewed_code(self) -> None:
        """L0 #1, local rail: check_local reads the branch head, then the merge
        must use THAT immutable SHA, not the mutable branch name. The branch is
        advanced to an unreviewed commit between the check and the merge."""
        reviewed = self._armed_signed()
        moved: dict[str, str] = {}

        def racing_runner(cmd, *a, **k):
            if "merge-base" in cmd and not moved:
                tree = _git(self.root, "rev-parse", f"{reviewed}^{{tree}}")
                unreviewed = _git(self.root, "commit-tree", tree, "-p", reviewed,
                                  "-m", "unreviewed")
                _git(self.root, "update-ref", "refs/heads/feature/x", unreviewed)
                moved["sha"] = unreviewed
            return session_auth._run(cmd)

        ok, msg = self.merge(runner=racing_runner)
        self.assertTrue(moved, "the race never fired; the test proves nothing")
        self.assertNotEqual(_git(self.root, "rev-parse", "main"), moved["sha"],
                            "the unreviewed commit reached main")
        if ok:
            self.assertEqual(_git(self.root, "rev-parse", "main"), reviewed)

    def test_it_refuses_unless_the_default_branch_is_checked_out(self) -> None:
        self._armed_signed()
        _git(self.root, "checkout", "-q", "feature/x")
        ok, msg = self.merge()
        self.assertFalse(ok)
        self.assertIn("checked out", msg.lower())

    def test_merging_the_default_branch_into_itself_is_refused(self) -> None:
        self.arm()
        ok, msg = self.merge(branch="main")
        self.assertFalse(ok)
        self.assertIn("itself", msg.lower())

    def test_dry_run_mutates_nothing(self) -> None:
        self._armed_signed()
        ok, msg = self.merge(dry_run=True)
        self.assertTrue(ok, msg)
        self.assertIn("DRY-RUN", msg)
        self.assertEqual(_git(self.root, "rev-parse", "main"), self.main_sha)


class TestCliWiring(LocalModeTestCase):
    def test_pr_and_local_branch_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            session_auth.main(["--repo-root", str(self.root), "check",
                               "--pr", "1", "--local-branch", "feature/x"])

    def test_check_requires_one_of_them(self) -> None:
        with self.assertRaises(SystemExit):
            session_auth.main(["--repo-root", str(self.root), "check"])

    def test_merge_local_branch_reaches_the_local_path(self) -> None:
        self.feature()
        rc = session_auth.main(["--repo-root", str(self.root), "merge",
                                "--local-branch", "feature/x", "--dry-run"])
        self.assertEqual(rc, 1)  # unarmed -> refused, and it never touched gh


if __name__ == "__main__":
    unittest.main()


class TestMergeLocalRefusalsAreAudited(LocalModeTestCase):
    """#206 papercut 1: every merge_local refusal leaves an audit record."""

    def merge(self, branch: str = "feature/x", **kw):
        return session_auth.merge_local(
            branch, self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, **kw)

    def refusals(self) -> list[dict]:
        return [r for r in self.audit_records() if r.get("event") == "merge-refused"]

    def test_remote_exists_refusal_is_audited(self) -> None:
        self.feature()
        self.add_remote()
        ok, _ = self.merge()
        self.assertFalse(ok)
        [rec] = self.refusals()
        self.assertEqual(rec["local_branch"], "feature/x")
        self.assertEqual(rec["protection"], session_auth.LOCAL_TAG)
        self.assertEqual(rec["bound"], "refused")
        self.assertIn("remote", rec["detail"].lower())

    def test_auth_refusal_is_audited_with_its_bound(self) -> None:
        self.feature()
        self.arm()  # never signed
        ok, _ = self.merge()
        self.assertFalse(ok)
        [rec] = self.refusals()
        self.assertEqual(rec["bound"], "no-verdict")

    def test_non_fast_forward_refusal_is_audited(self) -> None:
        self.feature()
        self.arm()
        self.sign()
        (self.root / "other.txt").write_text("x\n", encoding="utf-8")
        _git(self.root, "add", "other.txt")
        _git(self.root, "commit", "-q", "-m", "main moved")
        ok, _ = self.merge()
        self.assertFalse(ok)
        [rec] = self.refusals()
        self.assertIn("fast-forward", rec["detail"])
        self.assertFalse(rec["dry_run"])

    def test_self_merge_and_not_checked_out_are_audited(self) -> None:
        self.arm()
        self.merge(branch="main")
        self.feature()
        self.sign()
        _git(self.root, "checkout", "-q", "feature/x")
        self.merge()
        details = [r["detail"] for r in self.refusals()]
        self.assertEqual(len(details), 2)
        self.assertIn("itself", details[0])
        self.assertIn("checked out", details[1])

    def test_refusal_is_stamped_with_the_injected_clock(self) -> None:
        self.feature()
        self.add_remote()
        self.merge()
        [rec] = self.refusals()
        self.assertEqual(rec["ts"], session_auth._iso(self.now))

    def test_a_successful_merge_writes_no_refusal(self) -> None:
        self.feature()
        self.arm()
        self.sign()
        ok, msg = self.merge()
        self.assertTrue(ok, msg)
        self.assertEqual(self.refusals(), [])
