"""#349: safe forms the destructive guard still refused, and the dangerous
forms right next to them that it must keep refusing.

Every test runs the real guard against real git repos (the hook's own git
runner), so a "clean" or "dirty" answer is git's, not a fake's.

Shapes fixed here:
  1. Whole-compound refusal: a path-scoped discard (`git checkout -- f`,
     `git restore f`) refused a whole compound because some OTHER file was
     dirty, so the safe segments around it never ran either. A path-scoped
     discard is now gated on dirt in the paths it names.
  2. A tagged `git stash drop stash@{n}` (an explicit, literal ref) was
     refused on a dirty tree. A drop with no ref, or a ref the shell expands,
     and `git stash clear`, still are.
  3. Throwaway review worktrees in the session scratchpad were guarded like
     a seat's own tree. `git worktree remove --force` of a dirty worktree
     that is NOT scratch is now refused (it deletes uncommitted work);
     removing a scratch review or ~/.claude-builds worktree is not.
  4. (Not fixed here.) A data heredoc whose prose mentions a gated op is
     still split into lines and refused as if it ran it. Stripping such a
     body was tried and removed: no rule the reviews accepted could prove
     the body was not also run. `HeredocProseTest` pins main's behavior.
  5. A refused compound now names the segment that matched and says that no
     segment ran.
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import destructive  # noqa: E402

_ID = ["-c", "user.email=t@example.com", "-c", "user.name=t"]


def _git(*args: str, cwd: str | None = None) -> None:
    subprocess.run(["git", *_ID, *args], check=True, cwd=cwd,
                   capture_output=True, text=True)


def _no_prs(args):
    return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")


def _make_repo(path: str) -> str:
    os.makedirs(path)
    _git("init", "-q", "-b", "main", path)
    for name in ("a.py", "b.py"):
        with open(os.path.join(path, name), "w") as f:
            f.write("orig\n")
    _git("add", "a.py", "b.py", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


def _write(path: str, text: str = "changed\n") -> None:
    with open(path, "w") as f:
        f.write(text)


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.repo = _make_repo(os.path.join(self.root, "repo"))

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

    def assertBlocked(self, cmd: str, cwd: str | None = None) -> destructive.Decision:
        d = self.decide(cmd, cwd)
        self.assertEqual(d.exit_code, destructive.BLOCK, f"{cmd!r} was allowed")
        return d


class PathScopedDiscardTest(_RepoCase):
    """Shape 1: the mutation-test compound. b.py holds real uncommitted work;
    the compound mutates a.py, runs the tests and restores a.py."""

    def setUp(self) -> None:
        super().setUp()
        _write(os.path.join(self.repo, "b.py"), "real work in progress\n")

    def test_restoring_a_clean_file_inside_a_compound_is_allowed(self) -> None:
        for cmd in (
            "sed -i.bak s/orig/mutant/ a.py && python3 -c 'print(1)'; git checkout -- a.py",
            "printf 'mutant' > a.py; python3 -m pytest -q; git restore a.py",
            "git checkout HEAD -- a.py",
            "git restore --source=HEAD --staged --worktree -- a.py",
            "git -C . checkout -- a.py && echo restored",
        ):
            with self.subTest(cmd):
                self.assertAllowed(cmd)

    def test_restoring_the_file_that_holds_the_work_is_still_refused(self) -> None:
        for cmd in ("git checkout -- b.py", "git restore b.py",
                    "echo hi; git checkout -- a.py b.py",
                    "git restore --staged --worktree b.py"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_whole_tree_discards_are_still_refused(self) -> None:
        for cmd in ("git checkout -- .", "git checkout .", "git restore .",
                    "git reset --hard", "git checkout -f main",
                    "git restore --pathspec-from-file=list.txt",
                    "git checkout -- '*.py'", "git restore :/"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_a_path_the_shell_expands_is_judged_on_the_whole_tree(self) -> None:
        # sh expands these before git sees them; status would read the literal.
        for cmd in ("git checkout -- {a,b}.py", 'git checkout -- "$F"',
                    "git restore ~/x.py", "git checkout -- $(echo b.py)"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_a_staged_change_in_the_named_path_is_refused(self) -> None:
        _write(os.path.join(self.repo, "a.py"), "staged work\n")
        _git("add", "a.py", cwd=self.repo)
        self.assertBlocked("git checkout HEAD -- a.py")

    def test_the_path_resolves_in_the_directory_the_op_runs_in(self) -> None:
        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        _write(os.path.join(sub, "b.py"), "orig\n")
        _git("add", "sub/b.py", cwd=self.repo)
        _git("commit", "-q", "-m", "sub", cwd=self.repo)
        # b.py at the top is dirty; sub/b.py is clean.
        self.assertAllowed("git checkout -- b.py", cwd=sub)
        # A cd may fail and leave the op at the top, where b.py is dirty.
        self.assertBlocked("cd sub && git checkout -- b.py")
        self.assertAllowed("git -C sub checkout -- b.py")
        self.assertBlocked("git -C sub checkout -- ../b.py")


class StashDropTest(_RepoCase):
    """Shape 2. The tree is dirty, the condition the guard gates stash ops on."""

    def setUp(self) -> None:
        super().setUp()
        _write(os.path.join(self.repo, "a.py"), "to stash\n")
        _git("stash", "push", "-q", "-m", "review-tag", cwd=self.repo)
        _write(os.path.join(self.repo, "b.py"), "dirty\n")

    def test_a_drop_by_explicit_ref_is_allowed(self) -> None:
        for cmd in ("git stash drop stash@{0}", "git stash drop 'stash@{1}'",
                    "git stash apply stash@{0} && git stash drop stash@{0}",
                    "git stash drop -q stash@{0}"):
            with self.subTest(cmd):
                self.assertAllowed(cmd)

    def test_a_drop_with_no_ref_or_an_expanded_ref_and_clear_are_refused(self) -> None:
        for cmd in ("git stash drop", "git stash drop -q",
                    "git stash apply abc123; echo ok; git stash drop",
                    'git stash drop "$REF"', "git stash drop $(git stash list | head -1)",
                    "git stash clear"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)


def _scratchpad_root() -> str:
    """A path of the Claude Code session-scratchpad shape, under the temp dir."""
    return os.path.join(os.path.realpath(tempfile.gettempdir()),
                        f"claude-{90000 + os.getpid() % 9999}", "-tmp-example-proj",
                        str(uuid.uuid4()), "scratchpad")


class ScratchWorktreeTest(_RepoCase):
    """Shape 3."""

    def setUp(self) -> None:
        super().setUp()
        self.scratch = _scratchpad_root()
        self.addCleanup(shutil.rmtree, os.path.dirname(os.path.dirname(
            os.path.dirname(self.scratch))), True)
        os.makedirs(self.scratch)
        self.review = os.path.join(self.scratch, "rv78")
        _git("worktree", "add", "-q", "--detach", self.review, "HEAD", cwd=self.repo)
        _write(os.path.join(self.review, "a.py"), "falsifier mutant\n")
        # A seat worktree that is NOT scratch, holding uncommitted work.
        self.seat = os.path.join(self.root, "seat-wt")
        _git("worktree", "add", "-q", "-b", "seat", self.seat, "HEAD", cwd=self.repo)
        _write(os.path.join(self.seat, "a.py"), "uncommitted seat work\n")

    def test_discards_inside_a_scratchpad_review_worktree_are_allowed(self) -> None:
        for cmd in ("git checkout -- .", "git checkout -- a.py", "git reset --hard",
                    "git clean -fd"):
            with self.subTest(cmd):
                self.assertAllowed(cmd, cwd=self.review)
        self.assertAllowed(f"git -C {self.review} checkout -- .")

    def test_the_same_discards_in_a_non_scratch_worktree_are_refused(self) -> None:
        for cmd in ("git checkout -- .", "git checkout -- a.py", "git reset --hard"):
            with self.subTest(cmd):
                self.assertBlocked(cmd, cwd=self.seat)

    def test_a_scratchpad_path_that_is_not_a_linked_worktree_is_not_exempt(self) -> None:
        own = _make_repo(os.path.join(self.scratch, "own-repo"))
        _write(os.path.join(own, "a.py"))
        self.assertBlocked("git checkout -- a.py", cwd=own)

    def test_removing_a_scratch_review_worktree_is_allowed(self) -> None:
        self.assertAllowed(f"git worktree remove --force {self.review}")
        self.assertAllowed(f"git worktree remove -f {self.review} && git worktree prune")

    def test_removing_a_dirty_non_scratch_worktree_is_refused(self) -> None:
        for cmd in (f"git worktree remove --force {self.seat}",
                    f"git worktree remove -f {self.seat}",
                    f"git worktree remove -f -f {self.seat}",
                    f"echo cleanup; git worktree remove --force {self.seat}",
                    "git worktree remove --force ../seat-wt",
                    'git worktree remove --force "$WT"'):
            with self.subTest(cmd):
                self.assertBlocked(cmd)

    def test_removing_a_clean_or_unforced_worktree_is_allowed(self) -> None:
        # Without --force git itself refuses a dirty worktree.
        self.assertAllowed(f"git worktree remove {self.seat}")
        clean = os.path.join(self.root, "clean-wt")
        _git("worktree", "add", "-q", "--detach", clean, "HEAD", cwd=self.repo)
        self.assertAllowed(f"git worktree remove --force {clean}")

    def test_a_claude_builds_worktree_is_removable_but_still_guarded_inside(self) -> None:
        builds = os.path.join(self.root, "home", ".claude-builds")
        os.makedirs(builds)
        wt = os.path.join(builds, "example-123")
        _git("worktree", "add", "-q", "-b", "fix/example", wt, "HEAD", cwd=self.repo)
        _write(os.path.join(wt, "a.py"), "builder work\n")
        with mock.patch.object(destructive, "BUILD_WORKTREE_ROOTS", (builds,)):
            self.assertAllowed(f"git worktree remove --force {wt}")
            # A builder's own work in progress is not throwaway.
            self.assertBlocked("git checkout -- a.py", cwd=wt)
            self.assertBlocked("git reset --hard", cwd=wt)
            # The root itself, or a path beside it, is not a build worktree.
            self.assertBlocked(f"git worktree remove --force {self.seat}")


class HeredocProseTest(_RepoCase):
    """A heredoc body is still read line by line, as on main. Treating a
    data heredoc's prose as data was tried in this change and taken out: three
    review rounds each ran a "data" body through real zsh or bash."""

    def setUp(self) -> None:
        super().setUp()
        _write(os.path.join(self.repo, "b.py"), "dirty\n")

    def test_a_heredoc_body_naming_a_gated_op_is_refused_as_on_main(self) -> None:
        for cmd in ("cat > notes.md <<'EOF'\nworkaround; git reset --hard\nEOF\necho ok",
                    "bash <<'EOF'\necho x; git reset --hard\nEOF",
                    "cat <<'EOF' | sh\ngit checkout -- b.py\nEOF",
                    "cat <<EOF > f\n$(git reset --hard)\nEOF",
                    "cat > notes.md <<'EOF'\ndon't do this\nEOF\ngit stash drop",
                    "cat > notes.md <<'EOF'\nok; git reset --hard"):
            with self.subTest(cmd):
                self.assertBlocked(cmd)


class BlockMessageTest(_RepoCase):
    """Shape 5: say which segment matched, and that nothing ran."""

    def test_the_message_names_the_segment_and_says_nothing_ran(self) -> None:
        _write(os.path.join(self.repo, "a.py"))
        d = self.assertBlocked("python3 -m pytest -q && git stash drop && echo done")
        self.assertIn("git stash drop", d.message)
        self.assertIn("No part of this command ran", d.message)
        self.assertIn("stash@{N}", d.message)

    def test_the_message_names_each_blocked_segment(self) -> None:
        _write(os.path.join(self.repo, "a.py"))
        d = self.assertBlocked("git checkout -- a.py; echo mid; git reset --hard")
        self.assertIn("`git checkout -- a.py`", d.message)
        self.assertIn("`git reset --hard`", d.message)


class CompoundsHidingADestructivePartTest(_RepoCase):
    """The fixes above must not open a hiding place."""

    def setUp(self) -> None:
        super().setUp()
        _write(os.path.join(self.repo, "a.py"), "work\n")

    def test_destructive_parts_behind_safe_ones_are_refused(self) -> None:
        for cmd in (
            "git stash drop stash@{0}; git stash drop",
            "git checkout -- b.py && git checkout -- a.py",
            "echo ok | sh -c 'git checkout -- a.py'",
            "bash -c 'git restore a.py'",
            "x=$(git reset --hard)",
            "(cd . && git checkout -- a.py)",
            "true && { git restore a.py; }",
            "find . -name a.py -exec git checkout -- {} \\;",
            "cat > n.md <<'EOF'\nprose\nEOF\ngit restore a.py",
        ):
            with self.subTest(cmd):
                self.assertBlocked(cmd)


if __name__ == "__main__":
    unittest.main()
