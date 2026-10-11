"""#349 review round 1 (F4, N1): words the shell may rewrite are never taken
as literal, and a stash drop that can repeat is not one drop.

F4: the seats' shell is zsh. A pathspec, a worktree-remove target or a stash
ref counts as literal only when it matches an allowlist; everything else
(`b.py(N)`, `(a|b).py`, a glob, `$F`, `~`) falls back to the whole-tree
check. N1: a literal-ref drop inside a loop or xargs is gated, and stash ops
in a scratchpad worktree are not exempt (the stash is shared).

Real git, real repos, no network (the gh runner is a stub passed as an
argument).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import destructive  # noqa: E402

_ID = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
RESET = "git reset --hard"


def _git(*args: str, cwd: str | None = None) -> None:
    subprocess.run(["git", *_ID, *args], check=True, cwd=cwd,
                   capture_output=True, text=True)


def _no_prs(args):
    return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")


class _DirtyRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        _git("init", "-q", "-b", "main", self.repo)
        for name in ("a.py", "b.py"):
            with open(os.path.join(self.repo, name), "w") as f:
                f.write("orig\n")
        _git("add", "a.py", "b.py", cwd=self.repo)
        _git("commit", "-q", "-m", "init", cwd=self.repo)
        with open(os.path.join(self.repo, "b.py"), "a") as f:
            f.write("uncommitted work\n")

    def decide(self, cmd: str, cwd: str | None = None) -> destructive.Decision:
        cwd = cwd or self.repo

        def run(args):
            return subprocess.run(args, capture_output=True, text=True, cwd=cwd,
                                  timeout=10)
        return destructive.evaluate(
            {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": cmd}},
            env={}, git_runner=run, gh_runner=_no_prs)

    def assertAllowed(self, cmd: str, cwd: str | None = None) -> None:
        d = self.decide(cmd, cwd)
        self.assertEqual(d.exit_code, destructive.ALLOW, f"{cmd!r}: {d.message}")

    def assertBlocked(self, cmd: str, cwd: str | None = None) -> None:
        d = self.decide(cmd, cwd)
        self.assertEqual(d.exit_code, destructive.BLOCK, f"{cmd!r} was allowed")


class LiteralPathTest(_DirtyRepoCase):
    """F4: a pathspec zsh expands is judged on the whole tree. b.py is dirty."""

    def test_zsh_glob_forms_fall_back_to_the_whole_tree(self) -> None:
        for cmd in ("git checkout -- b.py(N)", "git checkout -- (a|b).py",
                    "git restore a.py(N)", "git checkout -- a.py b.py(.)",
                    "git checkout -- ^a.py", "git checkout -- a.py#", "git checkout -- <1-9>.py",
                    "git restore =a.py", "git restore ~/a.py", "git checkout -- a.py~",
                    "git checkout -- a.p[y]", "git checkout -- a*.py", "git checkout -- a?py",
                    "git checkout -- {a,b}.py", "git checkout -- $F", "git restore 'a b.py'"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_a_plain_path_to_a_clean_file_is_still_allowed(self) -> None:
        for cmd in ("git checkout -- a.py", "git restore ./a.py", "git checkout -- 'a'.py",
                    f"git -C {self.repo} checkout -- a.py"):
            with self.subTest(cmd):
                self.assertAllowed(cmd)

    def test_a_worktree_remove_target_the_shell_expands_is_refused(self) -> None:
        seat = os.path.join(self.root, "seat")
        _git("worktree", "add", "-q", "--detach", seat, "HEAD", cwd=self.repo)
        with open(os.path.join(seat, "a.py"), "a") as f:
            f.write("seat work\n")
        for cmd in ("git worktree remove --force ../seat(N)", "git worktree remove --force ../sea[t]",
                    "git worktree remove --force ../(seat|x)", "git worktree remove --force ../seat"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)


class PathspecSettingsTest(_DirtyRepoCase):
    """Review round 4, F7: the guard's status call must match paths the way
    the command will. b.py is dirty; git's case-insensitive pathspec setting
    makes `B.PY` name it."""

    def test_a_pathspec_setting_falls_back_to_the_whole_tree(self) -> None:
        for cmd in ("git --icase-pathspecs checkout -- B.PY",
                    "git --icase-pathspecs restore B.PY",
                    "GIT_ICASE_PATHSPECS=1 git checkout -- B.PY",
                    "export GIT_ICASE_PATHSPECS=1; git checkout -- B.PY",
                    "git --glob-pathspecs checkout -- a.py",
                    "git --literal-pathspecs restore a.py",
                    "GIT_LITERAL_PATHSPECS=0 git restore a.py",
                    "env GIT_ICASE_PATHSPECS=1 git checkout -- a.py",
                    "git -c core.ignorecase=true checkout -- a.py",
                    "git --no-pager checkout -- a.py",
                    "GIT_CONFIG_COUNT=1 git checkout -- a.py",
                    "export GIT_INDEX_FILE=x; git restore a.py"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_the_modelled_git_options_still_scope_the_check(self) -> None:
        for cmd in ("git checkout -- a.py", f"git -C {self.repo} restore a.py",
                    f"git --work-tree={self.repo} --git-dir={self.repo}/.git checkout -- a.py"):
            with self.subTest(cmd):
                self.assertAllowed(cmd)


class RestoreFromRevisionTest(_DirtyRepoCase):
    """Review round 4, F8: a path-scoped restore from another revision
    overwrites an untracked file of that name."""

    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(self.repo, "b.py"), "w") as f:
            f.write("orig\n")                      # the tree has no tracked dirt
        with open(os.path.join(self.repo, "u.txt"), "w") as f:
            f.write("committed once\n")
        _git("add", "u.txt", cwd=self.repo)
        _git("commit", "-q", "-m", "add u", cwd=self.repo)
        _git("rm", "-q", "u.txt", cwd=self.repo)
        _git("commit", "-q", "-m", "drop u", cwd=self.repo)
        with open(os.path.join(self.repo, "u.txt"), "w") as f:
            f.write("untracked work\n")

    def test_a_sourced_restore_over_an_untracked_file_is_refused(self) -> None:
        for cmd in ("git checkout HEAD~1 -- u.txt", "git restore -s HEAD~1 u.txt",
                    "git restore --source HEAD~1 --worktree -- u.txt",
                    "git restore --source=HEAD~1 u.txt", "git restore -sHEAD~1 u.txt"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_a_sourced_restore_over_an_ignored_file_is_refused(self) -> None:
        # Review round 5, F9: a once-committed file that is now ignored and
        # holds local content. `git status` does not list it unless asked.
        os.rename(os.path.join(self.repo, "u.txt"), os.path.join(self.repo, "u.keep"))
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write("u.txt\n")
        with open(os.path.join(self.repo, "u.txt"), "w") as f:
            f.write("local secret\n")
        os.remove(os.path.join(self.repo, "u.keep"))
        for cmd in ("git checkout HEAD~1 -- u.txt", "git restore -s HEAD~1 u.txt",
                    f"git -C {self.repo} restore --source=HEAD~1 u.txt"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)
        self.assertAllowed("git restore -s HEAD~1 a.py")

    def test_a_sourced_restore_of_a_clean_path_is_still_allowed(self) -> None:
        for cmd in ("git checkout HEAD~1 -- a.py", "git restore -s HEAD~1 a.py",
                    "git checkout HEAD -- a.py"):
            with self.subTest(cmd):
                self.assertAllowed(cmd)

    def test_a_discard_from_the_index_ignores_untracked_files(self) -> None:
        # It cannot touch one: git refuses the pathspec.
        self.assertAllowed("git checkout -- a.py")
        self.assertAllowed("git restore a.py")


class StashTest(_DirtyRepoCase):
    """N1: a literal-ref drop that repeats, and stash ops in a scratch worktree."""

    def setUp(self) -> None:
        super().setUp()
        _git("stash", "push", "-q", "-m", "one", cwd=self.repo)
        with open(os.path.join(self.repo, "b.py"), "a") as f:
            f.write("dirty again\n")

    def test_a_literal_ref_drop_that_can_repeat_is_refused(self) -> None:
        for cmd in ("while git stash drop stash@{0}; do :; done",
                    "for i in 1 2 3; do git stash drop stash@{0}; done",
                    "until ! git stash drop stash@{0}; do :; done",
                    "seq 5 | xargs -I@ git stash drop stash@{0}",
                    "while true; do bash -c 'git stash drop stash@{0}'; done",
                    "git stash drop stash@{0}(N)"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_one_drop_by_literal_ref_is_still_allowed(self) -> None:
        self.assertAllowed("git stash drop stash@{0}")

    def test_stash_ops_in_a_scratch_worktree_are_not_exempt(self) -> None:
        scratch = os.path.join(os.path.realpath(tempfile.gettempdir()),
                               f"claude-{90000 + os.getpid() % 9999}", "-tmp-example-proj",
                               str(uuid.uuid4()), "scratchpad")
        self.addCleanup(shutil.rmtree,
                        os.path.dirname(os.path.dirname(os.path.dirname(scratch))), True)
        os.makedirs(scratch)
        review = os.path.join(scratch, "rv")
        _git("worktree", "add", "-q", "--detach", review, "HEAD", cwd=self.repo)
        with open(os.path.join(review, "a.py"), "a") as f:
            f.write("mutant\n")
        # The discard exemption still holds there; the shared stash is not throwaway.
        self.assertAllowed(RESET, cwd=review)
        for cmd in ("git stash clear", "git stash drop"):
            with self.subTest(cmd):
                self.assertBlocked(cmd, cwd=review)
        self.assertBlocked(f"git -C {review} stash clear")


if __name__ == "__main__":
    unittest.main()
