"""Lane-safety for net.worktree (#354): upstream tracking, the merged-PR push
guard, the deleted-base warning, and restack's upstream handling.

The danger these guard: a lane whose upstream is origin/main (so a bare push
fails or, under push.default=upstream, lands on main), a push to a branch whose
PR already merged (the commits reach no PR), and a `reset --soft` lane whose
tree silently reverts merged main work while its suite stays green.

Real git against a throwaway bare remote; gh is stubbed through the runner
(never the real CLI). `git remote get-url origin` is answered with a GitHub URL
only by the stubbing runner, so the gh lookups run only where a test asks.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from net import worktree  # noqa: E402

GH_URL = "git@github.com:example-org/repo.git\n"


def _git(args: list[str], cwd: str, check: bool = True) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)
    return cp.stdout.strip()


class LaneBase(unittest.TestCase):
    """A working repo with a REAL bare `origin` (so push/fetch are real)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.remote = str(base / "remote.git")
        self.root = str(base / "repo")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false"),
                     ("push.default", "simple")):
            _git(["config", k, v], self.root)
        self.seed = self._commit("README", "seed\n", "seed")
        _git(["remote", "add", "origin", self.remote], self.root)
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], self.root)
        env = mock.patch.dict(os.environ, {"MAW_WORKTREE_DIR": str(base / "wts")})
        env.start()
        self.addCleanup(env.stop)
        self.gh_calls: list[list[str]] = []

    def _commit(self, fname: str, content: str, msg: str) -> str:
        p = Path(self.root) / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _git(["add", fname], self.root)
        _git(["commit", "-q", "-m", msg], self.root)
        return _git(["rev-parse", "HEAD"], self.root)

    def _advance_remote_main(self, fname: str, content: str, msg: str) -> str:
        """A sibling PR merges: a commit lands on the remote's main."""
        cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], self.root)
        _git(["checkout", "-q", "main"], self.root)
        _git(["merge", "-q", "--ff-only", "origin/main"], self.root)
        sha = self._commit(fname, content, msg)
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        _git(["checkout", "-q", cur], self.root)
        return sha

    def _upstream(self, branch: str = "HEAD") -> str:
        """The configured upstream as remote/branch ('' when none). Read from
        config, not `@{u}`: an upstream not yet pushed does not resolve as a rev."""
        if branch == "HEAD":
            branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], self.root)
        remote = _git(["config", f"branch.{branch}.remote"], self.root, check=False)
        merge = _git(["config", f"branch.{branch}.merge"], self.root, check=False)
        return f"{remote}/{merge.removeprefix('refs/heads/')}" if remote else ""

    def _gh_runner(self, pr: "dict | None" = None, remote_heads: "set[str] | None" = None):
        """Real git, except: origin reads as GitHub; `gh pr view` answers `pr`
        (None = no PR); `git ls-remote --heads origin <b>` answers from
        `remote_heads` when given (else the real remote)."""
        def run(cmd, cwd=None):
            if cmd[:4] == ["git", "remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=GH_URL, stderr="")
            if cmd[0] == "gh":
                self.gh_calls.append(list(cmd))
                if cmd[1:3] == ["pr", "view"]:
                    if pr is None:
                        return subprocess.CompletedProcess(cmd, 1, stdout="",
                                                           stderr="no pull requests found")
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pr), stderr="")
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unstubbed")
            if cmd[:2] == ["git", "ls-remote"] and remote_heads is not None:
                name = cmd[-1].removeprefix("refs/heads/")
                if name in remote_heads:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=f"{'0' * 40}\trefs/heads/{name}\n", stderr="")
                return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="")
            return worktree._run(cmd, cwd)
        return run


class BranchUpstreamTest(LaneBase):
    """(1) `net.worktree branch` tracks origin/<branch>, never origin/main."""

    def test_new_lane_tracks_its_own_name_not_main(self) -> None:
        worktree.new_branch("lane/a", cwd=self.root)
        self.assertEqual(self._upstream(), "origin/lane/a")

    def test_bare_push_from_a_new_lane_lands_on_its_own_branch(self) -> None:
        worktree.new_branch("lane/a", cwd=self.root)
        self._commit("a.txt", "a\n", "lane work")
        cp = subprocess.run(["git", "push", "-q"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(_git(["rev-parse", "origin/lane/a"], self.root),
                         _git(["rev-parse", "HEAD"], self.root))
        self.assertEqual(_git(["ls-remote", self.remote, "refs/heads/main"], self.root).split()[0],
                         self.seed, "a lane push must never move main")

    def test_onto_lane_also_tracks_its_own_name(self) -> None:
        worktree.new_branch("lane/b", cwd=self.root, onto=self.seed)
        self.assertEqual(self._upstream(), "origin/lane/b")


class RestackUpstreamTest(LaneBase):
    """(4) restack pushes to origin/<branch> with -u when the upstream is missing
    or wrong, instead of failing on a bare `git push --force-with-lease`."""

    def test_restack_with_no_upstream_pushes_and_sets_it(self) -> None:
        _git(["checkout", "-q", "-b", "feat/x", self.seed], self.root)
        self._commit("a.txt", "a\n", "feat")
        self._advance_remote_main("b.txt", "b\n", "sibling")
        res = worktree.restack("feat/x", cwd=self.root, push=True)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(self._upstream("feat/x"), "origin/feat/x")
        self.assertEqual(_git(["rev-parse", "origin/feat/x"], self.root),
                         _git(["rev-parse", "feat/x"], self.root))

    def test_restack_of_a_lane_tracking_main_pushes_to_its_own_name(self) -> None:
        # The pre-#354 `branch` shape: upstream = origin/main.
        _git(["checkout", "-q", "-b", "feat/y", "--track", "origin/main"], self.root)
        self._commit("a.txt", "a\n", "feat")
        main_sha = self._advance_remote_main("b.txt", "b\n", "sibling")
        res = worktree.restack("feat/y", cwd=self.root, push=True)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(self._upstream("feat/y"), "origin/feat/y")
        self.assertEqual(_git(["ls-remote", self.remote, "refs/heads/main"], self.root).split()[0],
                         main_sha, "restack must never push onto main")

    def test_restack_refuses_a_branch_whose_pr_merged(self) -> None:
        _git(["checkout", "-q", "-b", "feat/m", self.seed], self.root)
        before = self._commit("a.txt", "a\n", "feat")
        run = self._gh_runner(pr={"state": "MERGED", "number": 7, "baseRefName": "main"})
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.restack("feat/m", cwd=self.root, runner=run, push=True)
        self.assertIn("MERGED", str(cm.exception))
        self.assertIn("#7", str(cm.exception))
        self.assertEqual(_git(["rev-parse", "feat/m"], self.root), before, "nothing rebased")

    def test_bare_restack_refuses_a_pr_stacked_on_an_open_parent(self) -> None:
        _git(["checkout", "-q", "-b", "parent", self.seed], self.root)
        self._commit("p.txt", "p\n", "parent")
        _git(["checkout", "-q", "-b", "child"], self.root)
        before = self._commit("c.txt", "c\n", "child")
        run = self._gh_runner(pr={"state": "OPEN", "number": 9, "baseRefName": "parent"},
                              remote_heads={"parent", "main"})
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.restack("child", cwd=self.root, runner=run, push=False)
        msg = str(cm.exception)
        self.assertIn("parent", msg)
        self.assertIn("--onto", msg)
        self.assertEqual(_git(["rev-parse", "child"], self.root), before)

    def test_bare_restack_names_a_merged_parent_whose_branch_still_exists(self) -> None:
        _git(["checkout", "-q", "-b", "parent", self.seed], self.root)
        self._commit("p.txt", "p\n", "parent")
        _git(["checkout", "-q", "-b", "child"], self.root)
        self._commit("c.txt", "c\n", "child")
        inner = self._gh_runner(pr={"state": "OPEN", "number": 9, "baseRefName": "parent"},
                                remote_heads={"parent", "main"})

        def run(cmd, cwd=None):
            if cmd[1:4] == ["pr", "view", "parent"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
                    {"state": "MERGED", "number": 8, "baseRefName": "main"}), stderr="")
            return inner(cmd, cwd)

        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.restack("child", cwd=self.root, runner=run, push=False)
        msg = str(cm.exception)
        self.assertIn("already merged", msg)
        self.assertNotIn("still open", msg)
        self.assertIn("--onto auto", msg)

    def test_bare_restack_of_a_main_based_pr_still_runs(self) -> None:
        _git(["checkout", "-q", "-b", "feat/z", self.seed], self.root)
        self._commit("a.txt", "a\n", "feat")
        run = self._gh_runner(pr={"state": "OPEN", "number": 3, "baseRefName": "main"})
        res = worktree.restack("feat/z", cwd=self.root, runner=run, push=False)
        self.assertTrue(res.ok, res.detail)


class PushGuardTest(LaneBase):
    """(2)+(3) `net.worktree push`: refuse a merged PR's branch, warn on a
    deleted base, refuse a silent revert, set upstream to origin/<branch>."""

    def test_push_refuses_a_branch_whose_pr_merged(self) -> None:
        worktree.new_branch("lane/m", cwd=self.root)
        self._commit("a.txt", "a\n", "late fix")
        run = self._gh_runner(pr={"state": "MERGED", "number": 12, "baseRefName": "main"})
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.push(cwd=self.root, runner=run)
        msg = str(cm.exception)
        self.assertIn("MERGED", msg)
        self.assertIn("net.worktree branch", msg)  # points at a fresh lane
        self.assertEqual(_git(["ls-remote", self.remote, "refs/heads/lane/m"], self.root), "")

    def test_allow_merged_overrides(self) -> None:
        worktree.new_branch("lane/m2", cwd=self.root)
        self._commit("a.txt", "a\n", "late fix")
        run = self._gh_runner(pr={"state": "MERGED", "number": 12, "baseRefName": "main"})
        res = worktree.push(cwd=self.root, runner=run, allow_merged=True)
        self.assertTrue(res.ok, res.lines)

    def test_push_sets_upstream_to_its_own_name(self) -> None:
        _git(["checkout", "-q", "-b", "lane/p", "--track", "origin/main"], self.root)
        self._commit("a.txt", "a\n", "work")
        res = worktree.push(cwd=self.root)
        self.assertTrue(res.ok, res.lines)
        self.assertEqual(self._upstream(), "origin/lane/p")
        self.assertEqual(_git(["ls-remote", self.remote, "refs/heads/main"], self.root).split()[0],
                         self.seed)

    def test_push_warns_when_the_base_was_deleted(self) -> None:
        worktree.new_branch("lane/c", cwd=self.root)
        self._commit("c.txt", "c\n", "child")
        res = worktree.push(cwd=self.root, base="lane/gone-parent")
        self.assertTrue(res.ok, res.lines)
        text = "\n".join(res.lines)
        self.assertIn("no longer exists on origin", text)
        self.assertIn("restack --onto", text)

    def test_push_warns_when_the_open_prs_base_was_deleted(self) -> None:
        worktree.new_branch("lane/c2", cwd=self.root)
        self._commit("c.txt", "c\n", "child")
        run = self._gh_runner(pr={"state": "OPEN", "number": 4, "baseRefName": "parent-x"},
                              remote_heads={"main"})
        res = worktree.push(cwd=self.root, runner=run)
        self.assertIn("parent-x", "\n".join(res.lines))
        self.assertIn("no longer exists on origin", "\n".join(res.lines))

    def test_an_existing_base_draws_no_warning(self) -> None:
        worktree.new_branch("lane/d", cwd=self.root)
        self._commit("d.txt", "d\n", "work")
        res = worktree.push(cwd=self.root, base="main")
        self.assertTrue(res.ok)
        self.assertNotIn("no longer exists", "\n".join(res.lines))

    def test_push_refuses_the_reset_soft_revert_trap(self) -> None:
        # The stale-tree trap: a lane holds the OLD shared.txt; main then merges
        # a change to it; the lane is "freshened" with reset --soft origin/main +
        # commit. Its parent is current main (ancestry passes), its tree reverts.
        _git(["checkout", "-q", "main"], self.root)
        self._commit("shared.txt", "v1\n", "add shared")
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        worktree.new_branch("lane/r", cwd=self.root)
        self._commit("mine.txt", "mine\n", "my work")
        self._advance_remote_main("shared.txt", "v2\n", "main fixes shared (#50)")
        _git(["reset", "--soft", "origin/main"], self.root)
        _git(["commit", "-q", "-m", "my work, squashed"], self.root)
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.push(cwd=self.root)
        msg = str(cm.exception)
        self.assertIn("shared.txt", msg)
        self.assertIn("--allow-revert", msg)
        self.assertEqual(_git(["ls-remote", self.remote, "refs/heads/lane/r"], self.root), "")
        res = worktree.push(cwd=self.root, allow_revert=True)
        self.assertTrue(res.ok, res.lines)
        self.assertIn("REVERTS", "\n".join(res.lines))

    def test_push_refuses_the_default_branch(self) -> None:
        _git(["checkout", "-q", "main"], self.root)
        with self.assertRaises(worktree.WorktreeError):
            worktree.push(cwd=self.root)

    def test_cli_exit_codes(self) -> None:
        worktree.new_branch("lane/cli", cwd=self.root)
        self._commit("a.txt", "a\n", "work")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = worktree.main(["push", "--base", "gone"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("no longer exists on origin", out.getvalue() + err.getvalue())
        _git(["checkout", "-q", "main"], self.root)
        with contextlib.chdir(self.root), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(worktree.main(["push"]), 1)


if __name__ == "__main__":
    unittest.main()
