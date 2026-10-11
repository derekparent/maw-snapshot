"""Tests for the pre-file freshness gate on `review.cli request` (#197).

Real git against throwaway repos with a bare local "origin": the risk here is
the git plumbing (first-parent raw logs, merge diffs, ancestry), which a fake
runner would only echo back. The runner is passed explicitly and refuses `gh`.
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import cli, freshness  # noqa: E402


def _git(args: list[str], cwd: str) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return cp.stdout.strip()


def _runner(cmd, cwd=None):
    if cmd and cmd[0] == "gh":
        raise AssertionError(f"gh must not be called by the freshness gate: {cmd}")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd)


class FreshnessRepo(unittest.TestCase):
    """A work repo + bare origin. Seed: a.txt=v1, b.txt=b1. Then main gains
    c1 `feat: a to v2` (a.txt v1 -> v2), pushed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin = str(base / "origin.git")
        self.root = str(base / "work")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], self.root)
        _git(["remote", "add", "origin", self.origin], self.root)
        self.seed = self.commit({"a.txt": "v1\n", "b.txt": "b1\n"}, "seed")
        self.c1 = self.commit({"a.txt": "v2\n"}, "feat: a to v2")
        self.push_main()

    def write(self, files: dict) -> None:
        for name, content in files.items():
            p = Path(self.root) / name
            if content is None:
                _git(["rm", "-q", name], self.root)
            else:
                p.write_text(content, encoding="utf-8")
                _git(["add", name], self.root)

    def commit(self, files: dict, msg: str) -> str:
        self.write(files)
        _git(["commit", "-q", "-m", msg], self.root)
        return _git(["rev-parse", "HEAD"], self.root)

    def push_main(self) -> None:
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], self.root)

    def prepare(self, base=None, allow_revert=False):
        err = io.StringIO()
        rc, text = cli.prepare_request(base, _runner, self.root,
                                       allow_revert=allow_revert, err=err)
        return rc, text, err.getvalue()


class StaleBaseTest(FreshnessRepo):
    def test_branch_cut_from_a_moved_main_is_refused_before_assembly(self) -> None:
        _git(["checkout", "-q", "-b", "feat/stale", self.c1], self.root)
        self.commit({"b.txt": "b2\n"}, "feat: b")
        # main moves on after the branch was cut (a sibling PR merges)
        _git(["checkout", "-q", "main"], self.root)
        self.commit({"z.txt": "z\n"}, "sibling merge")
        self.push_main()
        _git(["checkout", "-q", "feat/stale"], self.root)

        rc, text, err = self.prepare()
        self.assertEqual(rc, freshness.REFUSE_EXIT)
        self.assertEqual(text, "", "no request may be assembled on a refusal")
        self.assertIn("1 commit(s) behind", err)
        self.assertIn(f"{freshness.RESTACK_CMD}", err)
        self.assertIn("/bin/maw-run net.worktree restack", err)

    def test_branch_on_current_main_passes(self) -> None:
        _git(["checkout", "-q", "-b", "feat/ok", "origin/main"], self.root)
        self.commit({"b.txt": "b2\n"}, "feat: b")
        rc, text, err = self.prepare()
        self.assertEqual(rc, 0, err)
        self.assertIn(f"freshness: current with origin/main@{self.c1[:7]}", text)
        self.assertIn("base: origin/main", text)  # default base is the fresh remote default
        self.assertNotIn("REVERTS:", text)

    def test_fetch_sees_a_remote_move_the_local_ref_has_not(self) -> None:
        # The branch is current with the STALE local origin/main; only the
        # fetch reveals that origin moved — the default must be FRESH.
        _git(["checkout", "-q", "-b", "feat/x", "origin/main"], self.root)
        self.commit({"b.txt": "b2\n"}, "feat: b")
        other = str(Path(self.tmp.name) / "other")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], other)
        (Path(other) / "y.txt").write_text("y\n", encoding="utf-8")
        _git(["add", "y.txt"], other)
        _git(["commit", "-q", "-m", "landed elsewhere"], other)
        _git(["push", "-q", "origin", "main"], other)

        rc, _, err = self.prepare()
        self.assertEqual(rc, freshness.REFUSE_EXIT, err)

    def test_explicit_base_wins_for_the_diff(self) -> None:
        _git(["checkout", "-q", "-b", "feat/ok", "origin/main"], self.root)
        self.commit({"b.txt": "b2\n"}, "feat: b")
        rc, text, _ = self.prepare(base=self.c1)
        self.assertEqual(rc, 0)
        self.assertIn(f"base: {self.c1}", text)

    def test_offline_no_origin_ref_warns_and_proceeds(self) -> None:
        _git(["remote", "remove", "origin"], self.root)
        _git(["checkout", "-q", "-b", "feat/local"], self.root)
        self.commit({"b.txt": "b2\n"}, "feat: b")
        rc, text, err = self.prepare()
        self.assertEqual(rc, 0, err)
        self.assertIn("WARNING", err)
        self.assertIn("freshness: unchecked", text)


def _fetch_fails(cmd, cwd=None):
    if cmd[:2] == ["git", "fetch"]:
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: unable to access origin\n")
    return _runner(cmd, cwd)


class FetchFailureTest(FreshnessRepo):
    """A failed fetch leaves the LAST-fetched origin/main. Measured against it, a
    branch the remote has moved past read as current with no warning."""

    def _remote_moves_on(self) -> None:
        other = str(Path(self.tmp.name) / "other")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], other)
        (Path(other) / "b.txt").write_text("remote moved\n", encoding="utf-8")
        _git(["commit", "-q", "-am", "sibling merge"], other)
        _git(["push", "-q", "origin", "main"], other)

    def setUp(self) -> None:
        super().setUp()
        _git(["checkout", "-q", "-b", "feat/x", "origin/main"], self.root)
        self.commit({"a.txt": "v3\n"}, "feat: a to v3")
        self._remote_moves_on()

    def test_failed_fetch_warns_and_marks_the_freshness_line(self) -> None:
        err = io.StringIO()
        rc, text = cli.prepare_request(None, _fetch_fails, self.root, err=err)
        self.assertEqual(rc, 0)
        self.assertIn("git fetch origin failed", err.getvalue())
        self.assertIn("unable to access origin", err.getvalue())
        self.assertIn("FETCH FAILED", text)

    def test_a_working_fetch_refuses_the_same_branch(self) -> None:
        err = io.StringIO()
        rc, text = cli.prepare_request(None, _runner, self.root, err=err)
        self.assertEqual(rc, freshness.REFUSE_EXIT)
        self.assertIn("NOT based on the latest", err.getvalue())


class RevertSniffTest(FreshnessRepo):
    def _reset_soft_branch(self, extra: dict | None = None) -> None:
        """The trap: parent IS current main, tree carries a.txt's OLD content."""
        _git(["checkout", "-q", "-b", "feat/trap", "origin/main"], self.root)
        self.write({"a.txt": "v1\n", **(extra or {})})
        _git(["commit", "-q", "-m", "feat: my work"], self.root)

    def test_reset_soft_shape_is_refused_and_names_the_commit(self) -> None:
        self._reset_soft_branch({"b.txt": "b2\n"})
        # the ancestry check alone would pass — that is the point of the sniff
        self.assertEqual(subprocess.run(
            ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=self.root,
        ).returncode, 0)
        rc, text, err = self.prepare()
        self.assertEqual(rc, freshness.REFUSE_EXIT)
        self.assertEqual(text, "")
        self.assertIn(f"a.txt reverts {self.c1[:7]} (feat: a to v2)", err)
        self.assertNotIn("b.txt", err, "an unrelated edit must not be flagged")
        self.assertIn("--allow-revert", err)

    def test_unrelated_edit_alone_is_not_flagged(self) -> None:
        _git(["checkout", "-q", "-b", "feat/b", "origin/main"], self.root)
        self.commit({"b.txt": "b2\n", "a.txt": "v3\n"}, "feat: b + a forward")
        rc, _, err = self.prepare()
        self.assertEqual(rc, 0, err)

    def test_allow_revert_passes_with_a_reverts_line(self) -> None:
        self._reset_soft_branch()
        rc, text, err = self.prepare(allow_revert=True)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"REVERTS: a.txt reverts {self.c1[:7]} (feat: a to v2)", text)
        self.assertIn("freshness: current with origin/main", text)

    def test_deleting_a_file_main_recently_added_is_flagged(self) -> None:
        added = self.commit({"new.txt": "fresh\n"}, "feat: add new.txt")
        self.push_main()
        _git(["checkout", "-q", "-b", "feat/del", "origin/main"], self.root)
        self.commit({"new.txt": None, "b.txt": "b2\n"}, "feat: work")
        rc, _, err = self.prepare()
        self.assertEqual(rc, freshness.REFUSE_EXIT)
        self.assertIn(f"new.txt deletes the file {added[:7]}", err)

    def test_branch_adding_a_new_file_is_not_flagged(self) -> None:
        _git(["checkout", "-q", "-b", "feat/new", "origin/main"], self.root)
        self.commit({"brand-new.txt": "x\n"}, "feat: new file")
        rc, _, err = self.prepare()
        self.assertEqual(rc, 0, err)

    def test_revert_of_a_merge_commit_is_named_by_the_merge(self) -> None:
        # A PR lands as a --no-ff merge; the first-parent diff of the MERGE
        # carries the pre-image, so the sniff names the merge commit.
        _git(["checkout", "-q", "-b", "pr", "main"], self.root)
        self.commit({"a.txt": "v3\n"}, "pr: a to v3")
        _git(["checkout", "-q", "main"], self.root)
        _git(["merge", "-q", "--no-ff", "-m", "Merge pull request #9", "pr"], self.root)
        merge = _git(["rev-parse", "HEAD"], self.root)
        self.push_main()
        _git(["checkout", "-q", "-b", "feat/trap", "origin/main"], self.root)
        self.commit({"a.txt": "v2\n"}, "feat: my work")
        rc, _, err = self.prepare()
        self.assertEqual(rc, freshness.REFUSE_EXIT)
        self.assertIn(f"a.txt reverts {merge[:7]} (Merge pull request #9)", err)

    def test_depth_bounds_the_sniff(self) -> None:
        for i in range(3):
            self.commit({"a.txt": f"v{3 + i}\n"}, f"bump {i}")
        self.push_main()
        _git(["checkout", "-q", "-b", "feat/old", "origin/main"], self.root)
        self.commit({"a.txt": "v1\n"}, "feat: ancient a")
        err = io.StringIO()
        rc, _ = cli.prepare_request(None, _runner, self.root, depth=2, err=err)
        self.assertEqual(rc, 0, "v1 is the pre-image of c1, older than depth=2")
        rc, _ = cli.prepare_request(None, _runner, self.root, depth=30, err=io.StringIO())
        self.assertEqual(rc, freshness.REFUSE_EXIT)


class CliSurfaceTest(unittest.TestCase):
    def test_request_flags(self) -> None:
        args = cli.build_parser().parse_args(["request", "--allow-revert"])
        self.assertIsNone(args.base)
        self.assertTrue(args.allow_revert)
        self.assertEqual(args.depth, freshness.DEFAULT_DEPTH)


if __name__ == "__main__":
    unittest.main()
