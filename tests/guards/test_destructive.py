"""Tests for guards.destructive — the structural destructive-op guard (item 4)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import destructive  # noqa: E402


def _cp(rc: int = 0, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


def _git_runner(clean: bool = True, error: bool = False):
    def run(args):
        if error:
            raise OSError("git unavailable")
        return _cp(0, "" if clean else " M file.py\n")
    return run


def _gh_runner(prs: list[int] | None = None, error: bool = False):
    def run(args):
        if error:
            raise OSError("gh unavailable")
        import json
        return _cp(0, json.dumps([{"number": n} for n in (prs or [])]))
    return run


def _event(cmd: str, tool: str = "Bash") -> dict:
    return {"tool_name": tool, "tool_input": {"command": cmd}}


class FindBranchDeletionsTest(unittest.TestCase):
    def test_branch_delete_flags(self) -> None:
        self.assertEqual(destructive.find_branch_deletions("git branch -D foo"), ["foo"])
        self.assertEqual(destructive.find_branch_deletions("git branch -d foo bar"), ["foo", "bar"])
        self.assertEqual(destructive.find_branch_deletions("git branch --delete foo"), ["foo"])

    def test_push_delete(self) -> None:
        self.assertEqual(destructive.find_branch_deletions("git push origin --delete foo"), ["foo"])
        self.assertEqual(destructive.find_branch_deletions("git push origin :foo"), ["foo"])
        self.assertEqual(
            destructive.find_branch_deletions("git push origin :refs/heads/foo"), ["foo"]
        )

    def test_global_opts_skipped(self) -> None:
        self.assertEqual(destructive.find_branch_deletions("git -c user.name=x branch -D foo"), ["foo"])

    def test_combined_short_flag(self) -> None:
        self.assertIn("foo", destructive.find_branch_deletions("git branch -Dr foo"))

    def test_non_deletions_ignored(self) -> None:
        self.assertEqual(destructive.find_branch_deletions("git branch foo"), [])
        self.assertEqual(destructive.find_branch_deletions("git push origin main"), [])
        self.assertEqual(destructive.find_branch_deletions("git checkout -b foo"), [])


class FindDirtyDestructiveTest(unittest.TestCase):
    def test_reset_hard(self) -> None:
        self.assertIn("git reset --hard", destructive.find_dirty_destructive("git reset --hard"))
        self.assertEqual(destructive.find_dirty_destructive("git reset --soft HEAD~1"), [])
        self.assertEqual(destructive.find_dirty_destructive("git reset HEAD"), [])

    def test_clean(self) -> None:
        self.assertIn("git clean -f", destructive.find_dirty_destructive("git clean -fd"))
        self.assertEqual(destructive.find_dirty_destructive("git clean -n"), [])  # dry-run

    def test_clean_long_force(self) -> None:
        # Advisor Finding 2: --force is the long form of -f and clobbers too.
        self.assertIn("git clean -f", destructive.find_dirty_destructive("git clean --force -d"))

    def test_checkout_discard(self) -> None:
        self.assertIn("git checkout (discard)", destructive.find_dirty_destructive("git checkout -- file"))
        self.assertIn("git checkout (discard)", destructive.find_dirty_destructive("git checkout ."))
        self.assertEqual(destructive.find_dirty_destructive("git checkout main"), [])

    def test_checkout_and_switch_force_flags(self) -> None:
        # Deferred note covered: -f/--force/--discard-changes discard local work.
        self.assertIn("git checkout (discard)", destructive.find_dirty_destructive("git checkout -f other"))
        self.assertIn("git checkout (discard)", destructive.find_dirty_destructive("git checkout --force other"))
        self.assertIn("git switch (discard)", destructive.find_dirty_destructive("git switch -f main"))
        self.assertIn("git switch (discard)", destructive.find_dirty_destructive("git switch --discard-changes main"))
        self.assertEqual(destructive.find_dirty_destructive("git switch main"), [])  # normal switch ok

    def test_restore(self) -> None:
        self.assertIn("git restore", destructive.find_dirty_destructive("git restore file"))
        self.assertEqual(destructive.find_dirty_destructive("git restore --staged file"), [])
        self.assertIn(
            "git restore", destructive.find_dirty_destructive("git restore --staged --worktree f")
        )

    def test_stash_drop_clear(self) -> None:
        self.assertIn("git stash drop/clear", destructive.find_dirty_destructive("git stash drop"))
        self.assertIn("git stash drop/clear", destructive.find_dirty_destructive("git stash clear"))
        self.assertEqual(destructive.find_dirty_destructive("git stash pop"), [])

    def test_quoted_command_in_message_not_a_false_positive(self) -> None:
        # shlex keeps the message as one token → subcommand is 'commit', not 'reset'
        self.assertEqual(
            destructive.find_dirty_destructive('git commit -m "use git reset --hard carefully"'),
            [],
        )

    def test_malformed_quotes_fail_open(self) -> None:
        # unbalanced quote → segment dropped, no false block
        self.assertEqual(destructive.find_dirty_destructive('git reset --hard "unbalanced'), [])

    def test_git_as_data_not_command_no_false_positive(self) -> None:
        # Advisor Finding 1: git appearing as an ARGUMENT (echo/printf) must not
        # parse as the command — anchored to command position.
        self.assertEqual(destructive.find_dirty_destructive("echo git reset --hard"), [])
        self.assertEqual(destructive.find_dirty_destructive("printf git checkout ."), [])
        self.assertEqual(destructive.find_branch_deletions("echo git branch -D main"), [])

    def test_command_prefix_wrappers_still_detected(self) -> None:
        # git at command position behind benign wrappers is still the command.
        self.assertIn("git reset --hard", destructive.find_dirty_destructive("FOO=bar git reset --hard"))
        self.assertIn("git clean -f", destructive.find_dirty_destructive("sudo git clean -fd"))
        self.assertEqual(destructive.find_branch_deletions("env X=1 git branch -D feature/x"), ["feature/x"])


class EvaluateTest(unittest.TestCase):
    def test_non_bash_allows(self) -> None:
        d = destructive.evaluate(_event("git branch -D main", tool="Read"), env={})
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_empty_command_allows(self) -> None:
        self.assertEqual(destructive.evaluate(_event("   "), env={}).exit_code, destructive.ALLOW)

    def test_protected_base_blocked(self) -> None:
        d = destructive.evaluate(
            _event("git branch -D main"), env={}, git_runner=_git_runner(), gh_runner=_gh_runner()
        )
        self.assertEqual(d.exit_code, destructive.BLOCK)
        self.assertIn("protected base", d.message)

    def test_protected_base_override(self) -> None:
        d = destructive.evaluate(
            _event("git branch -D main"),
            env={"MAW_ALLOW_BASE_DELETE": "1"},
            git_runner=_git_runner(), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_feature_with_open_prs_blocked(self) -> None:
        d = destructive.evaluate(
            _event("git branch -D feature/x"),
            env={}, git_runner=_git_runner(), gh_runner=_gh_runner(prs=[42, 43]),
        )
        self.assertEqual(d.exit_code, destructive.BLOCK)
        self.assertIn("#42", d.message)

    def test_feature_no_prs_allowed(self) -> None:
        d = destructive.evaluate(
            _event("git branch -D feature/x"),
            env={}, git_runner=_git_runner(), gh_runner=_gh_runner(prs=[]),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_gh_error_warns_but_allows(self) -> None:
        d = destructive.evaluate(
            _event("git branch -D feature/x"),
            env={}, git_runner=_git_runner(), gh_runner=_gh_runner(error=True),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)
        self.assertIn("could not verify", d.message)

    def test_reset_hard_dirty_blocked(self) -> None:
        d = destructive.evaluate(
            _event("git reset --hard"),
            env={}, git_runner=_git_runner(clean=False), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.BLOCK)
        self.assertIn("DIRTY", d.message)

    def test_reset_hard_clean_allowed(self) -> None:
        d = destructive.evaluate(
            _event("git reset --hard"),
            env={}, git_runner=_git_runner(clean=True), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_reset_hard_untracked_only_allowed(self) -> None:
        # #137: the only dirt is UNTRACKED (?? .maw/) — reset --hard destroys
        # nothing tracked, so no override should be needed.
        def run(args):
            if "--untracked-files=no" in args:
                return _cp(0, "")             # no TRACKED changes
            return _cp(0, "?? .maw/\n")       # untracked present
        d = destructive.evaluate(
            _event("git reset --hard"), env={}, git_runner=run, gh_runner=_gh_runner()
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_reset_hard_tracked_changes_still_blocked(self) -> None:
        # A tracked modification present -> still blocked (the reset would discard it).
        def run(args):
            if "--untracked-files=no" in args:
                return _cp(0, " M keep.py\n")     # TRACKED change
            return _cp(0, " M keep.py\n?? .maw/\n")
        d = destructive.evaluate(
            _event("git reset --hard"), env={}, git_runner=run, gh_runner=_gh_runner()
        )
        self.assertEqual(d.exit_code, destructive.BLOCK)

    def test_clean_untracked_only_still_blocked(self) -> None:
        # `git clean -f` DOES destroy untracked files, so untracked-only dirt must
        # still block it — the #137 relaxation is for reset/checkout/switch/restore.
        def run(args):
            if "--untracked-files=no" in args:
                return _cp(0, "")             # no tracked changes...
            return _cp(0, "?? data.bin\n")    # ...but untracked present
        d = destructive.evaluate(
            _event("git clean -fd"), env={}, git_runner=run, gh_runner=_gh_runner()
        )
        self.assertEqual(d.exit_code, destructive.BLOCK)

    def test_destructive_override(self) -> None:
        d = destructive.evaluate(
            _event("git clean -fd"),
            env={"MAW_ALLOW_DESTRUCTIVE": "1"},
            git_runner=_git_runner(clean=False), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_git_status_allowed(self) -> None:
        d = destructive.evaluate(
            _event("git status"), env={}, git_runner=_git_runner(), gh_runner=_gh_runner()
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_git_as_data_allowed_on_dirty_tree(self) -> None:
        # Finding 1 at the evaluate layer: even on a DIRTY tree, echoing the
        # words must not block.
        d = destructive.evaluate(
            _event("echo git reset --hard"),
            env={}, git_runner=_git_runner(clean=False), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_dirty_check_fails_open_on_git_error(self) -> None:
        # if git status can't run, do NOT block (fail-open)
        d = destructive.evaluate(
            _event("git reset --hard"),
            env={}, git_runner=_git_runner(error=True), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)


def _routing_git_runner(toplevel: str = "/repo", dirty: bool = True):
    """git runner that answers rev-parse (toplevel) and status (dirty?) distinctly."""
    def run(args):
        if "--show-superproject-working-tree" in args:
            return _cp(0, "")
        if "rev-parse" in args:
            return _cp(0, toplevel + "\n")
        if "status" in args:
            return _cp(0, " M file.py\n" if dirty else "")
        return _cp(0, "")
    return run


class CommandSetsOverrideTest(unittest.TestCase):
    def test_inline_leading_assignment(self) -> None:
        self.assertTrue(destructive._command_sets(
            "MAW_ALLOW_DESTRUCTIVE=1 git checkout -- f", "MAW_ALLOW_DESTRUCTIVE"))

    def test_exported_assignment(self) -> None:
        self.assertTrue(destructive._command_sets(
            "export MAW_ALLOW_DESTRUCTIVE=1; git checkout -- f", "MAW_ALLOW_DESTRUCTIVE"))

    def test_true_and_yes_accepted(self) -> None:
        self.assertTrue(destructive._command_sets("MAW_ALLOW_DESTRUCTIVE=true git reset --hard", "MAW_ALLOW_DESTRUCTIVE"))
        self.assertTrue(destructive._command_sets("MAW_ALLOW_DESTRUCTIVE=YES git reset --hard", "MAW_ALLOW_DESTRUCTIVE"))

    def test_zero_is_not_an_override(self) -> None:
        self.assertFalse(destructive._command_sets(
            "MAW_ALLOW_DESTRUCTIVE=0 git checkout -- f", "MAW_ALLOW_DESTRUCTIVE"))

    def test_quoted_argument_is_not_an_override(self) -> None:
        # The var name appears only as commit-message DATA, not as an assignment.
        self.assertFalse(destructive._command_sets(
            'git commit -m "set MAW_ALLOW_DESTRUCTIVE=1 to override"',
            "MAW_ALLOW_DESTRUCTIVE"))

    def test_absent(self) -> None:
        self.assertFalse(destructive._command_sets("git checkout -- f", "MAW_ALLOW_DESTRUCTIVE"))


class Issue86OverrideReachableTest(unittest.TestCase):
    """#86: the documented override must work from a Bash tool call."""

    def test_inline_override_permits_dirty_destructive(self) -> None:
        d = destructive.evaluate(
            _event("MAW_ALLOW_DESTRUCTIVE=1 git checkout -- file.py"),
            env={}, git_runner=_routing_git_runner(dirty=True), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_export_override_permits_dirty_destructive(self) -> None:
        d = destructive.evaluate(
            _event("export MAW_ALLOW_DESTRUCTIVE=1; git reset --hard"),
            env={}, git_runner=_routing_git_runner(dirty=True), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_no_override_still_blocks_dirty(self) -> None:
        d = destructive.evaluate(
            _event("git checkout -- file.py"),
            env={}, git_runner=_routing_git_runner(dirty=True), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.BLOCK)
        self.assertIn("git show HEAD:", d.message)  # names the working escape hatch

    def test_inline_base_delete_override(self) -> None:
        d = destructive.evaluate(
            _event("MAW_ALLOW_BASE_DELETE=1 git branch -D main"),
            env={}, git_runner=_routing_git_runner(), gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)


class Issue86ReviewWorktreeExemptTest(unittest.TestCase):
    """#86: the exact widget bite — restore inside a throwaway /maw:review worktree."""

    def test_review_worktree_restore_permitted(self) -> None:
        d = destructive.evaluate(
            _event("git checkout -- file.py"),
            env={},
            git_runner=_routing_git_runner(
                toplevel=os.path.join(os.path.realpath(tempfile.gettempdir()),
                                      "maw-review-abc123", "at-0123456789ab"),
                dirty=True),
            gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_non_review_worktree_still_blocks(self) -> None:
        d = destructive.evaluate(
            _event("git checkout -- file.py"),
            env={},
            git_runner=_routing_git_runner(toplevel="/home/user/Projects/maw", dirty=True),
            gh_runner=_gh_runner(),
        )
        self.assertEqual(d.exit_code, destructive.BLOCK)


class ForcePushTest(unittest.TestCase):
    """#206 papercut 2: a lease-less force-push is destructive on any tree."""

    def _eval(self, cmd: str, env: dict | None = None, clean: bool = True):
        return destructive.evaluate(_event(cmd), env=env or {},
                                    git_runner=_git_runner(clean=clean),
                                    gh_runner=_gh_runner())

    def test_bare_force_forms_detected(self) -> None:
        for cmd in ("git push --force origin fix/x", "git push -f origin fix/x",
                    "git push -fu origin fix/x", "git push origin +fix/x",
                    "git push origin +HEAD:refs/heads/fix/x",
                    "git push --force-with-lease --force origin fix/x"):
            self.assertTrue(destructive.find_force_pushes(cmd), cmd)

    def test_safe_forms_pass(self) -> None:
        for cmd in ("git push origin fix/x", "git push -u origin fix/x",
                    "git push --force-with-lease origin fix/x",
                    "git push --force-with-lease=fix/x:abc123 origin fix/x",
                    "git push --force-if-includes --force-with-lease origin fix/x",
                    "git push --dry-run --force origin fix/x",
                    "git push -nf origin fix/x",
                    "git commit -m 'never git push --force here'",
                    "git fetch -f origin"):
            self.assertEqual(destructive.find_force_pushes(cmd), [], cmd)

    def test_blocked_on_a_clean_tree(self) -> None:
        d = self._eval("git push --force origin fix/x", clean=True)
        self.assertEqual(d.exit_code, destructive.BLOCK)
        self.assertIn("force-with-lease", d.message)

    def test_lease_allowed(self) -> None:
        d = self._eval("git push --force-with-lease origin fix/x")
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_override_inline_and_env(self) -> None:
        self.assertEqual(self._eval("MAW_ALLOW_DESTRUCTIVE=1 git push -f origin x").exit_code,
                         destructive.ALLOW)
        self.assertEqual(self._eval("git push -f origin x",
                                    env={"MAW_ALLOW_DESTRUCTIVE": "1"}).exit_code,
                         destructive.ALLOW)

    def test_force_push_after_heredoc_is_caught(self) -> None:
        # The shape the advisor probed on PR #216.
        cmd = "cat > f.txt <<'EOF'\ndon't\nEOF\ngit push --force origin fix/x"
        self.assertEqual(self._eval(cmd).exit_code, destructive.BLOCK)

    def test_chained_after_safe_command_is_caught(self) -> None:
        cmd = "git add -A && git commit -m 'wip' && git push -f origin fix/x"
        self.assertEqual(self._eval(cmd).exit_code, destructive.BLOCK)


if __name__ == "__main__":
    unittest.main()


class L0DestructiveTargetsTheRightRepoTest(unittest.TestCase):
    """L0 #10: the dirt check runs in the repo the command acts on."""

    def _runner(self, dirty_dirs: "set[str]", calls: list):
        def run(args):
            calls.append(args)
            where = args[2] if args[:2] == ["git", "-C"] else "<hook-cwd>"
            if "rev-parse" in args:
                return _cp(0, where + "\n")
            if "status" in args:
                return _cp(0, " M f.py\n" if where in dirty_dirs else "")
            return _cp(0, "")
        return run

    def test_git_dash_c_is_assessed_in_that_repo(self) -> None:
        # Codex repro: from a clean checkout, `git -C /other/dirty reset --hard`
        # was assessed against the clean checkout.
        calls: list = []
        d = destructive.evaluate(
            {"tool_name": "Bash", "cwd": "/clean",
             "tool_input": {"command": "git -C /other/dirty reset --hard"}},
            env={}, git_runner=self._runner({"/other/dirty"}, calls),
            gh_runner=_gh_runner())
        self.assertEqual(d.exit_code, destructive.BLOCK)

    def test_relative_dash_c_and_cd_resolve_against_the_event_cwd(self) -> None:
        for cmd in ("git -C sub reset --hard", "cd sub && git reset --hard"):
            with self.subTest(cmd):
                calls: list = []
                d = destructive.evaluate(
                    {"tool_name": "Bash", "cwd": "/work",
                     "tool_input": {"command": cmd}},
                    env={}, git_runner=self._runner({"/work/sub"}, calls),
                    gh_runner=_gh_runner())
                self.assertEqual(d.exit_code, destructive.BLOCK, calls)

    def test_the_event_cwd_is_used_when_there_is_no_dash_c(self) -> None:
        calls: list = []
        d = destructive.evaluate(
            {"tool_name": "Bash", "cwd": "/dirty-here",
             "tool_input": {"command": "git reset --hard"}},
            env={}, git_runner=self._runner({"/dirty-here"}, calls),
            gh_runner=_gh_runner())
        self.assertEqual(d.exit_code, destructive.BLOCK)


class L0CdDriftFailsClosedTest(unittest.TestCase):
    """A `cd` may not take effect (it failed, or ran in a pipe or subshell), so
    the guard must not assume it did: every directory the op could run in is
    checked, and any dirty one blocks."""

    def _runner(self, dirty_dirs: "set[str]"):
        def run(args):
            where = args[2] if args[:2] == ["git", "-C"] else "<hook-cwd>"
            if "rev-parse" in args:
                return _cp(0, where + "\n")
            if "status" in args:
                return _cp(0, " M f.py\n" if where in dirty_dirs else "")
            return _cp(0, "")
        return run

    def test_a_cd_that_may_not_happen_does_not_hide_a_dirty_cwd(self) -> None:
        for cmd in ("cd /clean || git reset --hard",
                    "cd /clean | git reset --hard",
                    "(cd /clean); git reset --hard",
                    "cd /clean; git reset --hard",
                    "cd /nonexistent && git reset --hard"):
            with self.subTest(cmd):
                d = destructive.evaluate(
                    {"tool_name": "Bash", "cwd": "/dirty",
                     "tool_input": {"command": cmd}},
                    env={}, git_runner=self._runner({"/dirty"}),
                    gh_runner=_gh_runner())
                self.assertEqual(d.exit_code, destructive.BLOCK)

    def test_the_cd_target_is_still_checked(self) -> None:
        d = destructive.evaluate(
            {"tool_name": "Bash", "cwd": "/clean",
             "tool_input": {"command": "cd /dirty && git reset --hard"}},
            env={}, git_runner=self._runner({"/dirty"}), gh_runner=_gh_runner())
        self.assertEqual(d.exit_code, destructive.BLOCK)


class L0GitRedirectionTest(unittest.TestCase):
    """A destructive op pointed at another tree by --work-tree / --git-dir or
    GIT_WORK_TREE / GIT_DIR is checked there, or blocked when the tree is
    unknowable."""

    def _runner(self, dirty_dirs: "set[str]"):
        def run(args):
            where = args[2] if args[:2] == ["git", "-C"] else "<hook-cwd>"
            if "rev-parse" in args:
                return _cp(0, where + "\n")
            if "status" in args:
                return _cp(0, " M f.py\n" if where in dirty_dirs else "")
            return _cp(0, "")
        return run

    def _eval(self, cmd: str, dirty: "set[str]"):
        return destructive.evaluate(
            {"tool_name": "Bash", "cwd": "/clean", "tool_input": {"command": cmd}},
            env={}, git_runner=self._runner(dirty), gh_runner=_gh_runner()).exit_code

    def test_work_tree_is_checked_in_the_named_tree(self) -> None:
        for cmd in ("git --work-tree=/dirty reset --hard",
                    "git --work-tree /dirty reset --hard",
                    "GIT_WORK_TREE=/dirty git reset --hard",
                    "env GIT_WORK_TREE=/dirty git reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, {"/dirty"}), destructive.BLOCK)

    def test_a_git_dir_redirect_blocks_on_a_clean_cwd(self) -> None:
        for cmd in ("git --git-dir=/elsewhere/.git reset --hard",
                    "GIT_DIR=/elsewhere/.git git reset --hard",
                    "env GIT_DIR=/elsewhere/.git git clean -fd"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, set()), destructive.BLOCK)

    def test_more_cd_shapes(self) -> None:
        for cmd in ("cd /a\ngit reset --hard", "cd /a; cd /b; git reset --hard",
                    'cd "/a b" && git reset --hard'):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, {"/clean"}), destructive.BLOCK)
        self.assertEqual(self._eval('cd "/a b" && git reset --hard', {"/a b"}),
                         destructive.BLOCK)


class L0ReviewWorktreeMustBeProvenTest(unittest.TestCase):
    """L0 #10: a `maw-review-` substring no longer disables the guard."""

    def _runner(self, toplevel: str, linked: bool = True):
        import tempfile as _t
        def run(args):
            if "--show-toplevel" in args:
                return _cp(0, toplevel + "\n")
            if "--git-common-dir" in args:
                return _cp(0, ("/some/main/.git\n" if linked else toplevel + "/.git\n"))
            if "--absolute-git-dir" in args:
                return _cp(0, toplevel + "/.git\n")
            if "status" in args:
                return _cp(0, " M file.py\n")
            return _cp(0, "")
        return run

    def _eval(self, runner):
        return destructive.evaluate(_event("git checkout -- file.py"), env={},
                                    git_runner=runner, gh_runner=_gh_runner())

    def test_a_path_merely_containing_maw_review_still_blocks(self) -> None:
        for top in ("/Users/x/Projects/maw-review-notes", "/tmp/x/maw-review-1/app",
                    "/Users/x/maw-review-abc/at-0123456789ab"):
            with self.subTest(top):
                self.assertEqual(self._eval(self._runner(top)).exit_code,
                                 destructive.BLOCK)

    def test_the_real_review_worktree_shape_is_exempt(self) -> None:
        import os, tempfile
        top = os.path.join(os.path.realpath(tempfile.gettempdir()),
                           "maw-review-k2j3h4", "at-0123456789ab")
        self.assertEqual(self._eval(self._runner(top)).exit_code, destructive.ALLOW)

    def test_a_submodule_at_the_review_shape_still_blocks(self) -> None:
        import os, tempfile
        top = os.path.join(os.path.realpath(tempfile.gettempdir()),
                           "maw-review-k2j3h4", "at-0123456789ab")
        base = self._runner(top)

        def run(args):
            if "--show-superproject-working-tree" in args:
                return _cp(0, "/some/superproject\n")
            return base(args)
        self.assertEqual(self._eval(run).exit_code, destructive.BLOCK)

    def test_the_shape_without_a_linked_worktree_still_blocks(self) -> None:
        import os, tempfile
        top = os.path.join(os.path.realpath(tempfile.gettempdir()),
                           "maw-review-k2j3h4", "at-0123456789ab")
        self.assertEqual(self._eval(self._runner(top, linked=False)).exit_code,
                         destructive.BLOCK)


class L0AmbiguityFailsTowardBlockTest(unittest.TestCase):
    """Review F1 on #256: real repos, the hook's own git runner. A cd/-C target
    the guard cannot resolve counts as DIRTY; it never makes git error its way
    into "clean"."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.realpath(self.tmp.name)
        self.dirty = os.path.join(root, "dirty")
        self.clean = os.path.join(root, "clean")
        for d in (self.dirty, self.clean):
            os.makedirs(d)
            subprocess.run(["git", "init", "-q", d], check=True)
            subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-q", "--allow-empty", "-m", "i"], check=True)
            with open(os.path.join(d, "f"), "w") as f:
                f.write("x")
            subprocess.run(["git", "-C", d, "add", "f"], check=True)
            subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-q", "-m", "f"], check=True)
        with open(os.path.join(self.dirty, "f"), "w") as f:
            f.write("changed")

    def _eval(self, cmd: str, cwd: str) -> int:
        def run(args):
            return subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=10)
        return destructive.evaluate(
            {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": cmd}},
            env={}, git_runner=run, gh_runner=_gh_runner()).exit_code

    def test_unresolvable_targets_block_from_a_dirty_cwd(self) -> None:
        for cmd in ("cd - && git reset --hard", 'cd "$PWD" && git reset --hard',
                    'git -C "$PWD" reset --hard', "cd $VAR && git reset --hard",
                    "cd nosuch; git reset --hard", "cd ~nosuchuser && git reset --hard",
                    "git -C nosuch reset --hard", "cd && git reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.dirty), destructive.BLOCK)

    def test_the_prior_directory_stays_a_candidate(self) -> None:
        self.assertEqual(self._eval(f"cd {self.clean} && git reset --hard", self.dirty),
                         destructive.BLOCK)

    def test_a_clean_target_from_a_clean_cwd_is_allowed(self) -> None:
        self.assertEqual(self._eval(f"cd {self.clean} && git reset --hard", self.clean),
                         destructive.ALLOW)
        self.assertEqual(self._eval(f"git -C {self.clean} reset --hard", self.clean),
                         destructive.ALLOW)

    def test_a_missing_git_c_target_blocks_from_a_clean_cwd(self) -> None:
        self.assertEqual(self._eval("git -C nosuch reset --hard", self.clean),
                         destructive.BLOCK)


class L0SubshellAndGroupTest(L0AmbiguityFailsTowardBlockTest):
    """L0 #10b: ops inside subshells, groups and conditionals are still found,
    wrappers do not hide a cd, and a shell payload is unknowable."""

    def test_ops_inside_subshells_groups_and_conditionals_block(self) -> None:
        d = self.dirty
        for cmd in (f"(cd {d} && git reset --hard)",
                    f"(git -C {d} reset --hard)",
                    f"{{ cd {d}; git reset --hard; }}",
                    f"if true; then git -C {d} reset --hard; fi",
                    f"for x in 1; do git -C {d} clean -fd; done",
                    f"! git -C {d} reset --hard",
                    f"command cd {d} && git reset --hard",
                    f"builtin cd {d} && git reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_core_worktree_config_is_a_work_tree_redirect(self) -> None:
        cmd = f"git -c core.worktree={self.dirty} reset --hard"
        self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_symlinked_and_dotdot_targets_resolve_to_the_real_tree(self) -> None:
        link = os.path.join(os.path.dirname(self.dirty), "link-to-dirty")
        os.symlink(self.dirty, link)
        for cmd in (f"git -C {link} reset --hard",
                    f"git -C {self.clean}/../dirty reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_a_shell_payload_with_a_destructive_git_op_blocks(self) -> None:
        for cmd in ("sh -c 'git reset --hard'", 'bash -c "cd /x && git clean -fd"',
                    "zsh -c 'git -C /x reset --hard'"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_a_harmless_shell_payload_is_allowed(self) -> None:
        self.assertEqual(self._eval("sh -c 'git status'", self.clean), destructive.ALLOW)

    def test_clean_to_clean_still_allowed(self) -> None:
        self.assertEqual(self._eval(f"(cd {self.clean} && git reset --hard)", self.clean),
                         destructive.ALLOW)


class L0WrapperAndPathBypassTest(L0AmbiguityFailsTowardBlockTest):
    """Background security review on #257: wrappers with options, a
    path-qualified git, and combined shell flags must not hide the op."""

    def test_wrapped_and_path_qualified_git_is_found(self) -> None:
        d = self.dirty
        git = shutil.which("git") or "/usr/bin/git"
        for cmd in (f"{git} -C {d} reset --hard",
                    f"sudo -u nobody git -C {d} reset --hard",
                    f"env -i git -C {d} reset --hard",
                    f"command -p git -C {d} reset --hard",
                    f"nice -n 5 git -C {d} reset --hard",
                    f"time -p git -C {d} reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_combined_shell_flags_and_path_qualified_shells_are_scanned(self) -> None:
        for cmd in ("bash -lc 'git reset --hard'", "sh -ec 'git clean -fd'",
                    "/bin/sh -c 'git reset --hard'", "env bash -c 'git reset --hard'",
                    "xargs sh -c 'git reset --hard'"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_git_as_data_after_a_wrapper_is_not_a_command(self) -> None:
        self.assertEqual(self._eval("env FOO=bar echo git reset --hard", self.dirty),
                         destructive.ALLOW)


class L0OverrideScopeTest(L0AmbiguityFailsTowardBlockTest):
    """Security review on #257 (control regression): an override counts only
    where the shell would actually give it to the guarded command."""

    def test_an_override_that_never_reaches_the_op_does_not_count(self) -> None:
        for cmd in ("(MAW_ALLOW_DESTRUCTIVE=1 true); git reset --hard",
                    "MAW_ALLOW_DESTRUCTIVE=1 true; git reset --hard",
                    "(export MAW_ALLOW_DESTRUCTIVE=1); git reset --hard",
                    "{ export MAW_ALLOW_DESTRUCTIVE=1; }; git reset --hard",
                    "if MAW_ALLOW_DESTRUCTIVE=1 true; then git reset --hard; fi",
                    "MAW_ALLOW_DESTRUCTIVE=1 git status && git reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.dirty), destructive.BLOCK)

    def test_an_override_on_the_op_or_exported_first_still_counts(self) -> None:
        for cmd in ("MAW_ALLOW_DESTRUCTIVE=1 git reset --hard",
                    "export MAW_ALLOW_DESTRUCTIVE=1; git reset --hard",
                    "(MAW_ALLOW_DESTRUCTIVE=1 git reset --hard)",
                    "env MAW_ALLOW_DESTRUCTIVE=1 git reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.dirty), destructive.ALLOW)


class L0ReviewRound2Test(L0AmbiguityFailsTowardBlockTest):
    """Review on #257 @ 79d429d: nesting depth, `-c --`, more wrappers,
    `time (…)`, and export only as its own statement."""

    def test_deep_eval_nesting_blocks_instead_of_crashing(self) -> None:
        cmd = "eval " * 1000 + f"git -C {self.dirty} reset --hard"
        self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_a_double_dash_after_dash_c_is_not_the_script(self) -> None:
        cmd = f"bash -c -- 'git -C {self.dirty} reset --hard'"
        self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_timeout_stdbuf_and_time_group_are_seen_through(self) -> None:
        d = self.dirty
        for cmd in (f"timeout 5 git -C {d} reset --hard",
                    f"timeout -s KILL 5 git -C {d} reset --hard",
                    f"stdbuf -oL git -C {d} reset --hard",
                    f"time (git -C {d} reset --hard)"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.clean), destructive.BLOCK)

    def test_export_counts_only_as_its_own_statement(self) -> None:
        for cmd in ("export MAW_ALLOW_DESTRUCTIVE=1 | true; git reset --hard",
                    "false && export MAW_ALLOW_DESTRUCTIVE=1; git reset --hard",
                    "true || export MAW_ALLOW_DESTRUCTIVE=1; git reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.dirty), destructive.BLOCK)
        for cmd in ("export MAW_ALLOW_DESTRUCTIVE=1; git reset --hard",
                    "export MAW_ALLOW_DESTRUCTIVE=1\ngit reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, self.dirty), destructive.ALLOW)


class HookFailsClosedOnVisibleDestructiveTest(unittest.TestCase):
    """If evaluation itself errors, a command that visibly runs a destructive
    git op is blocked; anything else is still allowed."""

    def _hook(self, cmd: str, env: "dict | None" = None,
              command_sets_raises: bool = False) -> int:
        import importlib.util, io, json as _json
        path = Path(__file__).resolve().parents[2] / "maw" / "hooks" / "destructive-guard.py"
        spec = importlib.util.spec_from_file_location("dg_hook", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        env_patch = {k: v for k, v in os.environ.items()
                     if k not in ("MAW_ALLOW_DESTRUCTIVE", "MAW_ALLOW_BASE_DELETE")}
        env_patch.update(env or {})
        sets = (mock.patch.object(destructive, "_command_sets", side_effect=RuntimeError("x"))
                if command_sets_raises else mock.patch.object(
                    destructive, "_command_sets", wraps=destructive._command_sets))
        with mock.patch.object(destructive, "evaluate", side_effect=RuntimeError("boom")), \
                mock.patch.dict(os.environ, env_patch, clear=True), sets, \
                mock.patch("sys.stdin", io.StringIO(_json.dumps(event))), \
                mock.patch("sys.stderr", io.StringIO()):
            return mod.main()

    def test_the_named_override_clears_an_error_path_block(self) -> None:
        # The block message says "re-run with MAW_ALLOW_DESTRUCTIVE=1"; doing
        # that must work, from the env or inline, like evaluate()'s own paths.
        self.assertEqual(self._hook("git reset --hard", env={"MAW_ALLOW_DESTRUCTIVE": "1"}),
                         destructive.ALLOW)
        for cmd in ("MAW_ALLOW_DESTRUCTIVE=1 git reset --hard",
                    "export MAW_ALLOW_DESTRUCTIVE=1; git clean -fdx"):
            with self.subTest(cmd):
                self.assertEqual(self._hook(cmd), destructive.ALLOW)

    def test_the_override_is_not_widened_by_an_error(self) -> None:
        # Quoted data is not an override, and if the inline reader also breaks,
        # only the env counts.
        self.assertEqual(self._hook('git commit -m "MAW_ALLOW_DESTRUCTIVE=1" && git reset --hard'),
                         destructive.BLOCK)
        self.assertEqual(self._hook("MAW_ALLOW_DESTRUCTIVE=1 git reset --hard",
                                    command_sets_raises=True), destructive.BLOCK)
        self.assertEqual(self._hook("MAW_ALLOW_BASE_DELETE=1 git reset --hard"),
                         destructive.BLOCK)
        self.assertEqual(self._hook("git reset --hard", env={"MAW_ALLOW_DESTRUCTIVE": "0"}),
                         destructive.BLOCK)

    def test_an_evaluation_error_blocks_a_visible_destructive_op(self) -> None:
        self.assertEqual(self._hook("git reset --hard"), destructive.BLOCK)
        self.assertEqual(self._hook("cd x && git clean -fdx"), destructive.BLOCK)

    def test_an_evaluation_error_allows_a_harmless_command(self) -> None:
        self.assertEqual(self._hook("git status"), destructive.ALLOW)



class ShellOptionPayloadTest(unittest.TestCase):
    """Lane 4: `bash -o pipefail -c '...'` hid its script (the word after -o was
    read as the end of the options), and the force-push and branch-deletion
    checks never looked inside a shell payload at all."""

    def _eval(self, cmd: str, clean: bool = True, prs: "list[int] | None" = None):
        return destructive.evaluate(_event(cmd), env={},
                                    git_runner=_git_runner(clean=clean),
                                    gh_runner=_gh_runner(prs)).exit_code

    def test_script_found_past_value_taking_options(self) -> None:
        for cmd, script in (
            ("bash -o pipefail -c 'git reset --hard'", "git reset --hard"),
            ("bash -euo pipefail -c 'git reset --hard'", "git reset --hard"),
            ("bash +o history -c 'x'", "x"),
            ("bash -O extglob -c 'x'", "x"),
            ("bash -c -e 'x'", "x"),
            ("bash --norc -c 'x'", "x"),
            ("bash --rcfile /dev/null -c 'x'", "x"),
            ("zsh -o errexit -c 'x'", "x"),
            ("sh -c -- 'x'", "x"),
        ):
            with self.subTest(cmd):
                toks = destructive._segments(cmd)[0]
                self.assertEqual(destructive._shell_payload(toks), script)

    def test_a_script_file_is_not_a_payload(self) -> None:
        for cmd in ("bash script.sh", "bash -o pipefail script.sh", "bash -e"):
            with self.subTest(cmd):
                toks = destructive._segments(cmd)[0]
                self.assertIsNone(destructive._shell_payload(toks))

    def test_dirty_destructive_behind_options_blocks(self) -> None:
        for cmd in ("bash -o pipefail -c 'git reset --hard'",
                    "bash -euo pipefail -c 'git clean -fd'",
                    "sh -c -e 'git stash drop'"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, clean=False), destructive.BLOCK)

    def test_force_push_in_a_payload_blocks(self) -> None:
        for cmd in ("bash -c 'git push --force origin x'",
                    "bash -o pipefail -c 'git push -f origin x'",
                    "eval git push origin +x",
                    "sh -c \"bash -c 'git push --force origin x'\""):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd), destructive.BLOCK)

    def test_protected_branch_delete_in_a_payload_blocks(self) -> None:
        for cmd in ("bash -c 'git push origin --delete main'",
                    "bash -o pipefail -c 'git branch -D main'"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd), destructive.BLOCK)

    def test_safe_payloads_still_pass(self) -> None:
        for cmd in ("bash -o pipefail -c 'git status'",
                    "bash -c 'git push --force-with-lease origin x'",
                    "bash -c \"echo 'git push --force'\""):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd), destructive.ALLOW)


class CommandSubstitutionTest(unittest.TestCase):
    """Lane 4: a destructive op inside `$(...)` or backticks ran unguarded; the
    everyday heredoc commit message must stay quiet."""

    def _eval(self, cmd: str, clean: bool = False) -> int:
        return destructive.evaluate(_event(cmd), env={},
                                    git_runner=_git_runner(clean=clean),
                                    gh_runner=_gh_runner()).exit_code

    def test_substituted_ops_block_on_a_dirty_tree(self) -> None:
        for cmd in ("echo $(git reset --hard)", "x=$(git stash drop)",
                    "echo `git clean -fd`", 'echo "$(git checkout -- .)"',
                    'echo "$(echo $(git reset --hard))"',
                    "x=$(bash <<EOF\ngit reset --hard\nEOF\n)",
                    "cat <<EOF\n$(git reset --hard)\nEOF",
                    "bash -c 'echo $(git reset --hard)'"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd), destructive.BLOCK)

    def test_substituted_ops_pass_on_a_clean_tree(self) -> None:
        # It runs in the calling shell's tree, so it is gated on that tree's
        # dirt like a plain command, not blocked as unknowable.
        for cmd in ("echo $(git reset --hard)", "x=`git stash drop`"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd, clean=True), destructive.ALLOW)

    def test_substituted_force_push_blocks_on_any_tree(self) -> None:
        self.assertEqual(self._eval("echo $(git push -f origin x)", clean=True),
                         destructive.BLOCK)

    def test_data_that_mentions_an_op_stays_quiet(self) -> None:
        for cmd in (
            "git commit -m \"$(cat <<'EOF'\nfix: never git reset --hard (1) or a)\n"
            "git push --force here\nEOF\n)\"",
            "git commit -m 'uses $(git reset --hard)'",
            "cat <<'EOF'\n$(git reset --hard)\nEOF",
            "echo $((1 + 2))",
            "echo $(git status)",
        ):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd), destructive.ALLOW)


class NestingOverflowTest(unittest.TestCase):
    """Past MAX_PAYLOAD_DEPTH no check reads the payload. It used to be blocked
    only via the dirty-tree path, with a message claiming a DIRTY tree; the
    force-push and deletion finders saw nothing."""

    def _nest(self, inner: str, levels: int) -> str:
        import shlex
        for _ in range(levels):
            inner = "bash -c " + shlex.quote(inner)
        return inner

    def _eval(self, cmd: str, env: "dict | None" = None):
        return destructive.evaluate(_event(cmd), env=env or {},
                                    git_runner=_git_runner(clean=True),
                                    gh_runner=_gh_runner())

    def test_depth_10_push_and_delete_block_with_an_honest_message(self) -> None:
        for inner in ("git push --force origin x", "git push origin --delete main",
                      "echo $(git push -f origin x)"):
            with self.subTest(inner):
                d = self._eval(self._nest(inner, 10))
                self.assertEqual(d.exit_code, destructive.BLOCK)
                self.assertIn("nested deeper than", d.message)
                self.assertNotIn("DIRTY", d.message)

    def test_depth_within_the_cap_reads_through(self) -> None:
        cmd = self._nest("git status", destructive.MAX_PAYLOAD_DEPTH)
        self.assertFalse(destructive.nests_too_deep(cmd))
        self.assertEqual(self._eval(cmd).exit_code, destructive.ALLOW)
        self.assertTrue(destructive.nests_too_deep(
            self._nest("git status", destructive.MAX_PAYLOAD_DEPTH + 1)))

    def test_override_passes_overflow(self) -> None:
        d = self._eval(self._nest("git status", 10), env={"MAW_ALLOW_DESTRUCTIVE": "1"})
        self.assertEqual(d.exit_code, destructive.ALLOW)


class FindExecTest(unittest.TestCase):
    """Lane 4: `find ... -exec git ... \\;` ran its git op unguarded."""

    def _eval(self, cmd: str, clean: bool = False) -> int:
        return destructive.evaluate(_event(cmd), env={},
                                    git_runner=_git_runner(clean=clean),
                                    gh_runner=_gh_runner()).exit_code

    def test_exec_ops_block_on_a_dirty_tree(self) -> None:
        for cmd in ("find . -name '*.py' -exec git checkout -- {} \\;",
                    "find . -exec git reset --hard ';'",
                    "find . -type f -ok git restore {} \\;",
                    "find . -exec bash -c 'git clean -fd' \\;",
                    "find . -maxdepth 1 -print -exec git stash drop \\;"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd), destructive.BLOCK)

    def test_exec_is_gated_on_the_tree_but_execdir_is_unknowable(self) -> None:
        self.assertEqual(self._eval("find . -exec git checkout -- {} \\;", clean=True),
                         destructive.ALLOW)
        self.assertEqual(self._eval("find . -execdir git checkout -- {} +", clean=True),
                         destructive.BLOCK)

    def test_exec_force_push_blocks_on_any_tree(self) -> None:
        self.assertEqual(self._eval("find . -maxdepth 0 -exec git push -f origin x \\;",
                                    clean=True), destructive.BLOCK)

    def test_harmless_find_passes(self) -> None:
        for cmd in ("find . -name '*.pyc' -exec rm {} +",
                    "find . -exec git status \\;",
                    "find . -name '-exec'",
                    "echo find . -exec git reset --hard"):
            with self.subTest(cmd):
                self.assertEqual(self._eval(cmd), destructive.ALLOW)


class AnsiCQuoteTest(unittest.TestCase):
    """#266 review gap (c): $'..\\'..' read as a plain single quote closed early,
    then the scan treated the rest of the string as quoted and missed a real
    substitution after it."""

    def test_substitution_after_an_ansi_c_quote_is_seen(self) -> None:
        cmd = "printf $'it\\'s' ; x=$(git stash drop)"
        self.assertEqual(destructive._nested_scripts(cmd), ["git stash drop"])

    def test_ansi_c_quote_content_is_data(self) -> None:
        self.assertEqual(destructive._nested_scripts("echo $'$(git reset --hard)'"), [])


class MirrorPruneAndGrammarGapsTest(ForcePushTest):
    """Lane-3 debt (Lane D, #222 review): `--mirror` and `--prune` delete remote
    refs, and `case` arms / function bodies hid a command from the tokenizer.
    The subshell form of the same report was already fixed (d833d30) and is
    pinned by L0SubshellAndGroupTest; `:branch` deletion is pinned by
    FindBranchDeletionsTest."""

    def test_mirror_and_prune_block(self) -> None:
        for cmd in ("git push --mirror origin", "git push origin --mirror",
                    "git push --mirror --force-with-lease origin",
                    "git push --prune origin", "git push --prune origin 'refs/heads/*'"):
            d = self._eval(cmd)
            self.assertEqual(d.exit_code, destructive.BLOCK, cmd)
            self.assertIn("--mirror or --prune", d.message)

    def test_falsifier_dry_run_and_plain_push_pass(self) -> None:
        for cmd in ("git push --dry-run --mirror origin", "git push -n --prune origin",
                    "git push origin fix/x", "git fetch --prune origin",
                    "git remote prune origin"):
            self.assertEqual(self._eval(cmd).exit_code, destructive.ALLOW, cmd)

    def test_override_allows_mirror(self) -> None:
        d = self._eval("git push --mirror origin", env={"MAW_ALLOW_DESTRUCTIVE": "1"})
        self.assertEqual(d.exit_code, destructive.ALLOW)

    def test_case_arm_and_function_bodies_are_seen(self) -> None:
        for cmd in ("case a in a) git push -f o x;; esac",
                    "case $b in main) echo;; *) git push --force o x;; esac",
                    "case a in (a) git push -f o x;; esac",
                    "function f { git push -f o x; }; f",
                    "f() { git push -f o x; }", "f () { git push -f o x; }"):
            self.assertEqual(self._eval(cmd).exit_code, destructive.BLOCK, cmd)
        self.assertEqual(
            destructive.find_branch_deletions("case a in b) echo;; a) git branch -D main;; esac"),
            ["main"])
        self.assertEqual(destructive.find_dirty_destructive("f() { git reset --hard; }"),
                         ["git reset --hard"])

    def test_falsifier_quoted_case_text_is_not_a_command(self) -> None:
        for cmd in ('git commit -m "case a in a) git push -f o x;; esac"',
                    'echo "a)" git push -f', "git log --oneline a)"):
            self.assertEqual(self._eval(cmd).exit_code, destructive.ALLOW, cmd)
