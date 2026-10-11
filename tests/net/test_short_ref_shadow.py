"""A tag named like the remote default branch must not be what maw measures
against.

git resolves a short name through refs/tags/ BEFORE refs/remotes/, so once a
tag called `origin/main` exists (a plain fetch follows tags, and anyone with
push access can push one), `git rev-parse origin/main` answers the TAG. Every
freshness, scope, base and deploy step that handed git the short name then ran
against a commit the tag's author chose.

Real git, real bare remote, a real tag named `origin/main`. Each test first
relies on `test_fixture_really_shadows`: the short name resolves to the tag.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import session_auth  # noqa: E402
from net import worktree  # noqa: E402
from repin import check as repin_check  # noqa: E402
from review import freshness  # noqa: E402

FULL = "refs/remotes/origin/main"


def _git(args: list[str], cwd: str, check: bool = True) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)
    return cp.stdout.strip()


class ShadowBase(unittest.TestCase):
    """repo (clone of a bare remote) on a lane cut from the seed; the remote's
    main then advances by one commit, so the lane is 1 behind."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.remote = str(self.base / "remote.git")
        self.root = str(self.base / "repo")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        self._identity(self.root)
        self.seed = self._commit("README", "seed\n", "seed")
        _git(["remote", "add", "origin", self.remote], self.root)
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        _git(["symbolic-ref", "refs/remotes/origin/HEAD", FULL], self.root)

        _git(["checkout", "-q", "-b", "lane", self.seed], self.root)
        self.lane_head = self._commit("feature.txt", "new\n", "lane work")
        _git(["checkout", "-q", "main"], self.root)
        self.main_sha = self._commit("sibling.txt", "s\n", "sibling merged")
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        _git(["checkout", "-q", "lane"], self.root)

    def _identity(self, cwd: str) -> None:
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], cwd)

    def _commit(self, fname: str, content: str, msg: str, cwd: "str | None" = None) -> str:
        cwd = cwd or self.root
        (Path(cwd) / fname).write_text(content, encoding="utf-8")
        _git(["add", fname], cwd)
        _git(["commit", "-q", "-m", msg], cwd)
        return _git(["rev-parse", "HEAD"], cwd)

    def _shadow(self, sha: str, cwd: "str | None" = None) -> None:
        """Push a tag named origin/main at `sha`, as an attacker with push
        access would; it is then present locally too."""
        cwd = cwd or self.root
        _git(["tag", "origin/main", sha], cwd)
        _git(["push", "-q", "origin", "refs/tags/origin/main"], cwd)
        self.assertEqual(_git(["rev-parse", "origin/main"], cwd), sha, "fixture must shadow")
        self.assertEqual(_git(["rev-parse", FULL], cwd), self.main_sha)


class TestFixture(ShadowBase):
    def test_fixture_really_shadows(self) -> None:
        self._shadow(self.lane_head)
        self.assertNotEqual(_git(["rev-parse", "origin/main"], self.root),
                            _git(["rev-parse", FULL], self.root))


class TestFreshness(ShadowBase):
    def test_a_stale_lane_is_still_stale_with_the_tag_at_its_head(self) -> None:
        self._shadow(self.lane_head)
        fr = freshness.check(worktree._run, self.root)
        self.assertFalse(fr.current)
        self.assertEqual(fr.behind, 1)
        self.assertEqual(fr.ref_sha, self.main_sha)
        self.assertEqual(fr.ref, "origin/main")  # printed name stays short
        self.assertEqual(fr.git_ref, FULL)

    def test_control_without_the_tag_reads_the_same(self) -> None:
        fr = freshness.check(worktree._run, self.root)
        self.assertEqual((fr.current, fr.behind, fr.ref_sha), (False, 1, self.main_sha))


class TestReviewRequest(ShadowBase):
    def test_the_request_diff_stat_and_tier_ignore_the_tag(self) -> None:
        # Current lane: restack it first so the freshness gate passes, then
        # put the tag on the lane head. By the short name the diff is empty
        # and the tier is computed from no files.
        from review import cli as review_cli
        _git(["rebase", "-q", FULL, "lane"], self.root)
        (Path(self.root) / "maw").mkdir()
        self._commit("maw/guards.py", "x = 1\n", "code change")
        self._shadow(_git(["rev-parse", "HEAD"], self.root))
        text = review_cli.assemble_request("origin/main", worktree._run, self.root)
        self.assertIn("base: origin/main\n", text)
        self.assertIn("feature.txt", text)
        self.assertIn("maw/guards.py", text)
        self.assertIn("2 files changed", text)


class TestWorktree(ShadowBase):
    def test_new_branch_is_based_on_the_remote_branch_not_the_tag(self) -> None:
        self._shadow(self.lane_head)
        base = worktree.new_branch("lane2", self.root)
        self.assertEqual(base, "origin/main")
        self.assertEqual(_git(["rev-parse", "HEAD"], self.root), self.main_sha)

    def test_base_refs_fall_back_to_the_local_branch_by_full_name(self) -> None:
        _git(["update-ref", "-d", FULL], self.root)
        _git(["tag", "main", self.lane_head], self.root)  # shadows the short local name too
        self.assertEqual(worktree.base_refs(self.root), ("main", "refs/heads/main"))
        self.assertEqual(worktree.base_ref(self.root), "main")

    def test_push_still_warns_the_lane_is_behind(self) -> None:
        self._shadow(self.lane_head)
        res = worktree.push(self.root)
        self.assertTrue(res.ok, res.lines)
        self.assertTrue(any("1 commit(s) behind origin/main" in ln for ln in res.lines), res.lines)

    def test_doctor_still_flags_the_stale_base(self) -> None:
        self._shadow(self.lane_head)
        finding = worktree._check_stale_base(self.root, worktree._run)
        self.assertFalse(finding.ok, finding)
        self.assertIn("1 commit(s) behind", finding.detail)

    def test_restack_rebases_onto_the_remote_branch_not_the_tag(self) -> None:
        self._shadow(self.lane_head)
        res = worktree.restack("lane", self.root, push=False)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(res.base, "origin/main")
        self.assertEqual(_git(["merge-base", "lane", FULL], self.root), self.main_sha)

    def test_restack_onto_auto_compares_against_the_remote_branch(self) -> None:
        # The cherry fallback asks which leading commits main already holds. With
        # the tag at the lane head, `git cherry <tag> lane` is empty either way;
        # the assertion is on the git argument it was given.
        self._shadow(self.lane_head)
        seen: list[list[str]] = []

        def runner(cmd, cwd=None):
            seen.append(list(cmd))
            return worktree._run(cmd, cwd)

        try:
            worktree.discover_old_parent("lane", self.root, runner)
        except worktree.WorktreeError:
            pass
        cherry = [c for c in seen if c[:2] == ["git", "cherry"]]
        self.assertEqual(cherry, [["git", "cherry", FULL, "lane"]])


class TestMergeScope(ShadowBase):
    def _scope(self) -> list[str]:
        def runner(cmd):
            return subprocess.run(cmd, capture_output=True, text=True)
        return session_auth.merge_scope(Path(self.root), "main", self.lane_head, 88, runner)

    def test_scope_is_measured_against_the_remote_branch(self) -> None:
        self._shadow(self.lane_head)
        text = "\n".join(self._scope())
        # Against the tag: no changes, 0 behind. Against the branch: the truth.
        self.assertIn("feature.txt", text)
        self.assertNotIn("(no changes)", text)
        self.assertIn("1 commit(s) behind origin/main", text)
        self.assertIn(f"origin/main@{self.main_sha[:7]}", text)
        self.assertNotIn("refs/remotes", text)  # printed text keeps the short name

    def test_control_without_the_tag_reads_the_same(self) -> None:
        with_tagless = "\n".join(self._scope())
        self._shadow(self.lane_head)
        self.assertEqual("\n".join(self._scope()), with_tagless)


class TestRepin(ShadowBase):
    """The deploy step: the pinned worktree fast-forwards to the remote main,
    never to a tag that happens to be a fast-forward of it."""

    def _pinned(self) -> "tuple[Path, str]":
        home = self.base / "home"
        pinned = home / ".maw" / "plugin-main"
        pinned.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", self.remote, str(pinned)], check=True)
        self._identity(str(pinned))
        # A commit that is a fast-forward of the pin but was never on main.
        _git(["checkout", "-q", "-b", "side", self.seed], str(pinned))
        evil = self._commit("evil.txt", "x\n", "never reviewed", cwd=str(pinned))
        _git(["checkout", "-q", "-b", "plugin-pin", self.seed], str(pinned))
        _git(["tag", "origin/main", evil], str(pinned))
        self.assertEqual(_git(["rev-parse", "origin/main"], str(pinned)), evil)
        return home, evil

    def test_fast_forward_lands_on_the_remote_main(self) -> None:
        home, evil = self._pinned()
        repin_check.fast_forward_pinned(home=home)
        head = _git(["rev-parse", "HEAD"], str(home / ".maw" / "plugin-main"))
        self.assertEqual(head, self.main_sha)
        self.assertNotEqual(head, evil)

    def test_status_counts_against_the_remote_main(self) -> None:
        home, _evil = self._pinned()
        # The tag sits on the pin's own HEAD: by the short name the mirror
        # reads 0 ahead / 0 behind while the remote main is one commit on.
        _git(["tag", "-f", "origin/main", self.seed], str(home / ".maw" / "plugin-main"))
        rep = repin_check.pinned_worktree_status(home=home)
        self.assertEqual((rep.ahead, rep.behind), (0, 1))
        self.assertFalse(rep.synced)


if __name__ == "__main__":
    unittest.main()
