"""#355: `session_auth merge` shows WHAT it merges — the three-dot diff-stat,
the merge-tree result — and warns when the PR's base is behind main or its tree
silently reverts recent main work (the #322 sniff, at merge time).

Real git (a working repo with a real bare origin); gh and the repin steps are
fakes; `_authorize` is patched (it has its own suite). The scope block is
advisory: it never blocks a merge, and a git failure reads as "unchecked".
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import session_auth  # noqa: E402


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


class MergeScopeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.remote = str(base / "remote.git")
        self.root = base / "repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], str(self.root))
        self._commit("README", "seed\n", "seed")
        self._commit("shared.txt", "v1\n", "add shared")
        _git(["remote", "add", "origin", self.remote], str(self.root))
        _git(["push", "-q", "origin", "main"], str(self.root))
        _git(["fetch", "-q", "origin"], str(self.root))
        self.calls: list[list[str]] = []

    def _commit(self, fname: str, content: str, msg: str) -> str:
        (self.root / fname).write_text(content, encoding="utf-8")
        _git(["add", fname], str(self.root))
        _git(["commit", "-q", "-m", msg], str(self.root))
        return _git(["rev-parse", "HEAD"], str(self.root))

    def _main_advances(self, fname: str, content: str, msg: str) -> str:
        cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], str(self.root))
        _git(["checkout", "-q", "main"], str(self.root))
        _git(["merge", "-q", "--ff-only", "origin/main"], str(self.root))
        sha = self._commit(fname, content, msg)
        _git(["push", "-q", "origin", "main"], str(self.root))
        _git(["checkout", "-q", cur], str(self.root))
        return sha

    def _pr_branch(self, *files: "tuple[str, str]") -> str:
        _git(["checkout", "-q", "-b", "fix/x", "origin/main"], str(self.root))
        sha = ""
        for fname, content in files:
            sha = self._commit(fname, content, f"edit {fname}")
        _git(["push", "-q", "origin", "fix/x"], str(self.root))
        return sha

    def _runner(self, head: str, git_ok: bool = True, fetch_rc: int = 0,
                fetch_raises: "bool | Exception" = False):
        def run(cmd, *a, **k):
            self.calls.append(cmd)
            if cmd[:1] == ["git"]:
                if "fetch" in cmd and "--quiet" in cmd and fetch_raises:
                    raise fetch_raises if isinstance(fetch_raises, Exception) else OSError("no git")
                if "fetch" in cmd and "--quiet" in cmd and fetch_rc:
                    return subprocess.CompletedProcess(
                        cmd, fetch_rc, stdout="",
                        stderr="fatal: unable to access\n'https://x/': \x1b[31mCould not resolve host")
                if not git_ok and "get-url" not in cmd:
                    return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="boom")
                return subprocess.run(cmd, capture_output=True, text=True)
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "author": {"login": "executor"}, "headRefOid": head,
                    "baseRefName": "main", "headRefName": "fix/x", "title": "a PR"}), stderr="")
            if cmd[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"defaultBranchRef": {"name": "main"}}), stderr="")
            if cmd[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            if cmd[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        return run

    def _merge(self, head: str, dry_run: bool = True, git_ok: bool = True, **kw):
        runner_kw = {k: kw.pop(k) for k in ("fetch_rc", "fetch_raises") if k in kw}
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, head)):
            return session_auth.merge_pr(
                88, self.root, runner=self._runner(head, git_ok, **runner_kw),
                dry_run=dry_run, repo="maw", **kw)


class TestMergeScope(MergeScopeBase):
    def test_dry_run_prints_the_three_dot_diff_stat(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        ok, msg = self._merge(head)
        self.assertTrue(ok, msg)
        self.assertIn("origin/main...", msg)
        self.assertIn("feature.txt", msg)
        self.assertIn("1 file changed", msg)

    def test_three_dot_stat_excludes_main_side_changes(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        self._main_advances("other.txt", "main side\n", "sibling merged")
        ok, msg = self._merge(head)
        self.assertTrue(ok, msg)
        scope = [ln for ln in msg.splitlines() if "|" in ln]
        self.assertTrue(any("feature.txt" in ln for ln in scope), msg)
        self.assertFalse(any("other.txt" in ln for ln in scope), msg)

    def test_warns_when_the_pr_base_is_behind_main(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        self._main_advances("a.txt", "a\n", "sibling one")
        self._main_advances("b.txt", "b\n", "sibling two")
        ok, msg = self._merge(head)
        self.assertTrue(ok, msg)  # advisory: it never blocks
        self.assertIn("2 commit(s) behind origin/main", msg)
        self.assertIn("WARN", msg)

    def test_a_current_base_draws_no_behind_warning(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        ok, msg = self._merge(head)
        self.assertTrue(ok, msg)
        self.assertNotIn("behind origin/main", msg)
        self.assertIn("merge-tree: clean", msg)

    def test_merge_tree_conflict_is_named(self) -> None:
        head = self._pr_branch(("shared.txt", "pr side\n"))
        self._main_advances("shared.txt", "main side\n", "main edits shared")
        ok, msg = self._merge(head)
        self.assertTrue(ok, msg)
        self.assertIn("merge-tree: CONFLICTS", msg)
        self.assertIn("shared.txt", msg)

    def test_silent_revert_is_warned_at_merge_time(self) -> None:
        # The reset --soft trap: parent is current main, tree is old.
        _git(["checkout", "-q", "-b", "fix/x", "origin/main"], str(self.root))
        self._commit("mine.txt", "mine\n", "my work")
        self._main_advances("shared.txt", "v2\n", "main fixes shared (#50)")
        _git(["fetch", "-q", "origin"], str(self.root))
        _git(["reset", "--soft", "origin/main"], str(self.root))
        head = self._commit("mine.txt", "mine\n", "my work, squashed")
        ok, msg = self._merge(head)
        self.assertTrue(ok, msg)
        self.assertIn("REVERTS", msg)
        self.assertIn("shared.txt", msg)

    def test_the_real_merge_output_carries_the_scope_too(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        self._main_advances("a.txt", "a\n", "sibling")
        ok, msg = self._merge(head, dry_run=False)
        self.assertTrue(ok, msg)
        self.assertIn("feature.txt", msg)
        self.assertIn("behind origin/main", msg)
        merge_idx = next(i for i, c in enumerate(self.calls) if c[:3] == ["gh", "pr", "merge"])
        stat_idx = next(i for i, c in enumerate(self.calls) if "--stat" in c)
        self.assertLess(stat_idx, merge_idx, "the scope is read BEFORE the merge")

    def test_git_failure_reads_unchecked_and_never_blocks(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        ok, msg = self._merge(head, git_ok=False)
        self.assertTrue(ok, msg)
        self.assertIn("[scope] unchecked", msg)


    def test_scope_exception_reads_unchecked_and_merge_proceeds(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        with mock.patch.object(session_auth, "merge_scope", side_effect=RuntimeError("kaboom")):
            ok, msg = self._merge(head, dry_run=False)
        self.assertTrue(ok, msg)
        self.assertIn("[scope] unchecked: kaboom", msg)
        self.assertTrue(any(c[:3] == ["gh", "pr", "merge"] for c in self.calls))


class TestFailedFetchIsReported(MergeScopeBase):
    """A scope block measured against a ref the fetch could not refresh must
    say so: main moved on the remote, the local origin/main did not."""

    def _stale_setup(self) -> str:
        head = self._pr_branch(("feature.txt", "new\n"))
        # The remote main advances from ANOTHER clone; this repo never fetches it.
        other = Path(self._tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", self.remote, str(other)], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], str(other))
        (other / "sibling.txt").write_text("s\n", encoding="utf-8")
        _git(["add", "sibling.txt"], str(other))
        _git(["commit", "-q", "-m", "sibling"], str(other))
        _git(["push", "-q", "origin", "main"], str(other))
        return head

    def test_control_a_good_fetch_sees_the_remote_commit(self) -> None:
        head = self._stale_setup()
        ok, msg = self._merge(head)
        self.assertTrue(ok, msg)
        self.assertIn("1 commit(s) behind origin/main", msg)
        self.assertNotIn("fetch origin FAILED", msg)
        self.assertNotIn("STALE", msg)

    def test_failed_fetch_is_a_warn_not_a_clean_block(self) -> None:
        head = self._stale_setup()
        ok, msg = self._merge(head, fetch_rc=128)
        self.assertTrue(ok, msg)  # advisory: it never blocks
        scope = [ln for ln in msg.splitlines() if ln.startswith("[scope]")]
        self.assertIn("WARN git fetch origin FAILED", scope[0])
        self.assertIn("rc=128", scope[0])
        self.assertIn("Could not resolve host", scope[0])
        self.assertIn("may be stale", scope[0])
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("commit(s) behind", msg)  # the stale ref cannot see it
        self.assertTrue(any("unchecked: whether the PR base is behind" in ln for ln in scope), msg)
        self.assertTrue(any("merge-tree: clean (against a possibly STALE ref" in ln
                            for ln in scope), msg)
        self.assertFalse(any(ln.rstrip().endswith("merge-tree: clean") for ln in scope), msg)

    def test_fetch_error_text_never_carries_a_credential(self) -> None:
        token = "ghp_" + "A1b2C3d4" * 4
        cases = [
            f"fatal: unable to access 'https://x-access-token:{token}@github.com/o/r.git/': 403",
            f"fatal: Authentication failed for 'https://someone:hunter2secret@example.com/r.git'",
            f"remote: bad token {token} supplied",
            f"fatal: could not read from git@github.com:o/r.git password=hunter2secret@x",
            f"error: header Authorization:{token}",
        ]
        for raw in cases:
            text = session_auth._fetch_error_text(raw)
            self.assertNotIn(token, text, raw)
            self.assertNotIn("hunter2secret", text, raw)
            self.assertNotIn("://", text, raw)
            self.assertIn("<redacted>", text, raw)
        self.assertEqual(
            session_auth._fetch_error_text("fatal: Could not resolve host: github.com"),
            "fatal: Could not resolve host: github.com")

    def test_a_credential_in_the_fetch_error_stays_out_of_the_block(self) -> None:
        head = self._stale_setup()
        secret = "ghp_" + "Z9y8X7w6" * 4
        inner = self._runner(head)

        def runner(cmd, *a, **k):
            if cmd[:1] == ["git"] and "fetch" in cmd and "--quiet" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 128, stdout="",
                    stderr=f"fatal: unable to access 'https://u:{secret}@github.com/o/r/': 403")
            return inner(cmd, *a, **k)

        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, head)):
            ok, msg = session_auth.merge_pr(88, self.root, runner=runner, dry_run=True, repo="maw")
        self.assertTrue(ok, msg)
        self.assertIn("WARN git fetch origin FAILED (rc=128: fatal: unable to access", msg)
        self.assertNotIn(secret, msg)

    def test_fetch_that_cannot_run_is_reported_too(self) -> None:
        head = self._stale_setup()
        ok, msg = self._merge(head, fetch_raises=True)
        self.assertTrue(ok, msg)
        self.assertIn("WARN git fetch origin FAILED (could not run git)", msg)

    def test_fetch_timeout_says_timed_out(self) -> None:
        head = self._stale_setup()
        ok, msg = self._merge(head, fetch_raises=subprocess.TimeoutExpired(["git"], 30))
        self.assertTrue(ok, msg)
        self.assertIn("WARN git fetch origin FAILED (timed out)", msg)

    def test_failed_fetch_marks_a_conflict_line_as_well(self) -> None:
        head = self._pr_branch(("shared.txt", "pr side\n"))
        self._main_advances("shared.txt", "main side\n", "main edits shared")
        _git(["fetch", "-q", "origin"], str(self.root))
        ok, msg = self._merge(head, fetch_rc=1)
        self.assertTrue(ok, msg)
        self.assertRegex(msg, r"merge-tree: CONFLICTS in shared\.txt \(against a possibly STALE")
        self.assertIn("1 commit(s) behind origin/main", msg)

    def test_failed_fetch_on_a_real_merge_still_merges_and_still_warns(self) -> None:
        head = self._stale_setup()
        ok, msg = self._merge(head, dry_run=False, fetch_rc=128)
        self.assertTrue(ok, msg)
        self.assertIn("WARN git fetch origin FAILED", msg)
        self.assertTrue(any(c[:3] == ["gh", "pr", "merge"] for c in self.calls))


class TestScopeIsShownBeforeTheMerge(MergeScopeBase):
    def _emit(self, text: str) -> None:
        self.calls.append(["EMIT", text])

    def test_real_merge_emits_auth_and_scope_before_gh_pr_merge(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        self._main_advances("a.txt", "a\n", "sibling")
        ok, msg = self._merge(head, dry_run=False, emit=self._emit)
        self.assertTrue(ok, msg)
        emits = [i for i, c in enumerate(self.calls) if c[0] == "EMIT"]
        self.assertEqual(len(emits), 1)
        merge_idx = next(i for i, c in enumerate(self.calls) if c[:3] == ["gh", "pr", "merge"])
        self.assertLess(emits[0], merge_idx, "shown BEFORE gh pr merge runs")
        shown = self.calls[emits[0]][1]
        self.assertIn("[auth] OK", shown)
        self.assertIn("feature.txt", shown)
        self.assertIn("1 commit(s) behind origin/main", shown)
        self.assertIn("merge-tree: clean", shown)
        self.assertNotIn("[merge]", shown)
        # The returned message still carries everything, emitted text first.
        self.assertTrue(msg.startswith(shown), msg)
        self.assertIn("[merge] PR #88", msg[len(shown):])

    def test_dry_run_and_refusals_emit_nothing(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        ok, _ = self._merge(head, dry_run=True, emit=self._emit)
        self.assertTrue(ok)
        with mock.patch.object(session_auth, "gh_pr_facts", return_value=(None, "gone")):
            ok, _ = self._merge(head, dry_run=False, emit=self._emit)
        self.assertFalse(ok)
        self.assertFalse([c for c in self.calls if c[0] == "EMIT"])

    def test_an_emit_that_raises_never_blocks_the_merge(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))

        def boom(text: str) -> None:
            raise BrokenPipeError("stdout closed")

        ok, msg = self._merge(head, dry_run=False, emit=boom)
        self.assertTrue(ok, msg)
        self.assertTrue(any(c[:3] == ["gh", "pr", "merge"] for c in self.calls))

    def test_failed_merge_was_still_preceded_by_the_scope(self) -> None:
        head = self._pr_branch(("feature.txt", "new\n"))
        inner = self._runner(head)

        def runner(cmd, *a, **k):
            if cmd[:3] == ["gh", "pr", "merge"]:
                self.calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="head moved")
            return inner(cmd, *a, **k)

        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, head)):
            ok, msg = session_auth.merge_pr(88, self.root, runner=runner, repo="maw",
                                            emit=self._emit)
        self.assertFalse(ok)
        self.assertIn("FAILED: head moved", msg)
        emit_idx = next(i for i, c in enumerate(self.calls) if c[0] == "EMIT")
        merge_idx = next(i for i, c in enumerate(self.calls) if c[:3] == ["gh", "pr", "merge"])
        self.assertLess(emit_idx, merge_idx)


class TestMergeCliPrintsOnce(unittest.TestCase):
    """`session_auth merge --pr N`: the pre-merge text is printed (flushed)
    when emitted, and the final print carries only the rest."""

    def _run_main(self, fake) -> "tuple[int, str, str]":
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(session_auth, "merge_pr", side_effect=fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = session_auth.main(["--repo-root", d, "merge", "--pr", "88", "--repo", "maw"])
        return rc, out.getvalue(), err.getvalue()

    def test_success_prints_each_line_once_in_order(self) -> None:
        seen_at_merge: list[str] = []

        def fake(pr, root, **kw):
            pre = "[auth] OK\n[scope] merge-tree: clean"
            kw["emit"](pre)
            seen_at_merge.append(sys.stdout.getvalue())  # what is out when gh would run
            return True, pre + "\n[merge] PR #88 merged"

        rc, out, err = self._run_main(fake)
        self.assertEqual(rc, session_auth.PASS)
        self.assertEqual(seen_at_merge, ["[auth] OK\n[scope] merge-tree: clean\n"])
        self.assertEqual(out, "[auth] OK\n[scope] merge-tree: clean\n[merge] PR #88 merged\n")
        self.assertEqual(err, "")

    def test_failed_merge_sends_only_the_failure_to_stderr(self) -> None:
        def fake(pr, root, **kw):
            pre = "[auth] OK\n[scope] merge-tree: clean"
            kw["emit"](pre)
            return False, pre + "\n[merge] gh pr merge 88 FAILED: x"

        rc, out, err = self._run_main(fake)
        self.assertEqual(rc, session_auth.DENY)
        self.assertEqual(out, "[auth] OK\n[scope] merge-tree: clean\n")
        self.assertEqual(err, "[merge] gh pr merge 88 FAILED: x\n")

    def test_no_emit_prints_the_whole_message(self) -> None:
        rc, out, err = self._run_main(lambda pr, root, **kw: (False, "merge REFUSED [x]: y"))
        self.assertEqual(rc, session_auth.DENY)
        self.assertEqual((out, err), ("", "merge REFUSED [x]: y\n"))


if __name__ == "__main__":
    unittest.main()
