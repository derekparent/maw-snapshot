"""The post-merge hook deploys only from the main checkout on the default
branch, and `repin.check hook` reports and refreshes the installed copy.

`.git/hooks` is shared by every linked worktree. Before this, a trial merge in
a throwaway review worktree fired `repin.check sync` (with that worktree's own
repin code) and rewrote the live cache sentinel.

Real git and the real hook template, installed into a temp repo. Two
fixtures: a STUB repin package that records each sync (which worktree fired
it), and one end-to-end fixture with the REAL repin package, a pinned worktree
and a cache under a temp HOME, asserting on the real `.maw-pinned-sha`.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "maw"))

from repin import check  # noqa: E402

TEMPLATE = REPO / "maw" / "init" / "templates" / "git-hooks" / "post-merge"

STUB = '''import os, sys
if sys.argv[1:] == ["sync"]:
    with open(os.environ["HOOK_MARKER"], "a", encoding="utf-8") as fh:
        fh.write(os.getcwd() + "\\n")
    sys.exit(int(os.environ.get("HOOK_SYNC_RC", "0")))
'''


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra)
    return env


class HookRepo(unittest.TestCase):
    """A source repo (clone of a bare remote) with the hook template installed."""

    plugin_files: "dict[str, str]" = {"maw/repin/__init__.py": "", "maw/repin/check.py": STUB}
    default = "main"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.marker = self.base / "sync-marker"
        self.env = _env(HOOK_MARKER=str(self.marker), HOME=str(self.base / "home"))
        (self.base / "home").mkdir()
        self.remote = self.base / "remote.git"
        self.root = self.base / "repo"
        self._run(["git", "init", "-q", "--bare", "-b", self.default, str(self.remote)], self.base)
        self._run(["git", "init", "-q", "-b", self.default, str(self.root)], self.base)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false"),
                     ("pull.rebase", "false")):
            self.git("config", k, v)
        for rel, content in self.plugin_files.items():
            self._write(self.root / rel, content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", self.default)
        self.git("fetch", "-q", "origin")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD",
                 f"refs/remotes/origin/{self.default}")
        hook = self.root / ".git" / "hooks" / "post-merge"
        hook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE, hook)
        hook.chmod(0o755)

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _run(self, cmd: list[str], cwd: Path, check_rc: bool = True):
        cp = subprocess.run(cmd, cwd=str(cwd), env=self.env, capture_output=True, text=True)
        if check_rc and cp.returncode != 0:
            self.fail(f"{cmd} failed rc={cp.returncode}: {cp.stderr}")
        return cp

    def git(self, *args: str, cwd: "Path | None" = None, check_rc: bool = True):
        return self._run(["git", *args], cwd or self.root, check_rc)

    def side_branch(self, name: str = "side", fname: str = "side.txt") -> None:
        """A branch one commit ahead of the seed, ready to be merged."""
        self.git("branch", name, f"refs/heads/{self.default}")
        wt = self.base / f"mk-{name}"
        self.git("worktree", "add", "-q", str(wt), name)
        self._write(wt / fname, "x\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", f"work on {name}", cwd=wt)
        self.git("worktree", "remove", "--force", str(wt))

    def advance_remote(self, fname: str = "remote.txt") -> str:
        other = self.base / "other"
        if not other.exists():
            self._run(["git", "clone", "-q", str(self.remote), str(other)], self.base)
            for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
                self.git("config", k, v, cwd=other)
        self.git("pull", "-q", "--ff-only", cwd=other)
        self._write(other / fname, "r\n")
        self.git("add", "-A", cwd=other)
        self.git("commit", "-q", "-m", f"remote adds {fname}", cwd=other)
        self.git("push", "-q", "origin", self.default, cwd=other)
        return self.git("rev-parse", "HEAD", cwd=other).stdout.strip()

    def synced(self) -> list[str]:
        return self.marker.read_text(encoding="utf-8").splitlines() if self.marker.exists() else []


class TestWhereTheHookActs(HookRepo):
    def test_merge_in_the_main_checkout_on_main_syncs(self) -> None:
        self.side_branch()
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertEqual(self.synced(), [str(self.root)])

    def test_a_real_pull_in_the_main_checkout_syncs(self) -> None:
        self.advance_remote()
        self.git("pull", "-q", "--ff-only")
        self.assertEqual(self.synced(), [str(self.root)])

    def test_merge_in_a_detached_scratch_worktree_does_not_sync(self) -> None:
        self.side_branch()
        wt = self.base / "scratch"
        self.git("worktree", "add", "-q", "--detach", str(wt), f"refs/heads/{self.default}")
        cp = self.git("merge", "-q", "--no-ff", "-m", "trial", "side", cwd=wt)
        self.assertEqual(cp.returncode, 0)
        self.assertTrue((wt / "side.txt").exists(), "the merge itself happened")
        self.assertEqual(self.synced(), [])

    def test_merge_in_a_linked_worktree_on_a_branch_does_not_sync(self) -> None:
        self.side_branch()
        wt = self.base / "seat"
        self.git("worktree", "add", "-q", "-b", "lane", str(wt), f"refs/heads/{self.default}")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side", cwd=wt)
        self.assertEqual(self.synced(), [])

    def test_a_linked_worktree_holding_the_default_branch_does_not_sync(self) -> None:
        # Not the main checkout, even though it is ON the default branch.
        self.side_branch()
        self.git("checkout", "-q", "--detach")
        wt = self.base / "holds-main"
        self.git("worktree", "add", "-q", str(wt), self.default)
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side", cwd=wt)
        self.assertEqual(self.synced(), [])

    def test_main_checkout_on_a_feature_branch_does_not_sync(self) -> None:
        self.side_branch()
        self.git("checkout", "-q", "-b", "feature")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side")
        self.assertEqual(self.synced(), [])

    def test_main_checkout_detached_does_not_sync(self) -> None:
        self.side_branch()
        self.git("checkout", "-q", "--detach")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side")
        self.assertEqual(self.synced(), [])

    def test_a_failing_sync_never_fails_the_merge(self) -> None:
        self.side_branch()
        self.env["HOOK_SYNC_RC"] = "1"
        cp = self.git("merge", "-q", "--no-ff", "-m", "merge side", "side", check_rc=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(self.synced(), [str(self.root)])
        self.assertIn("post-merge: repin sync failed", cp.stderr)

    def test_a_repo_without_the_plugin_source_is_a_noop(self) -> None:
        self.git("rm", "-q", "-r", "maw")
        self.git("commit", "-q", "-m", "not a plugin repo")
        self.side_branch()
        cp = self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(self.synced(), [])

    def test_the_hook_runs_from_a_subdirectory_merge(self) -> None:
        self.side_branch()
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=self.root / "maw")
        self.assertEqual(self.synced(), [str(self.root)])


class TestOtherDefaultBranch(HookRepo):
    default = "trunk"

    def test_default_branch_is_read_from_origin_head(self) -> None:
        self.side_branch()
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertEqual(self.synced(), [str(self.root)])

    def test_a_branch_named_main_is_not_the_default_here(self) -> None:
        self.side_branch()
        self.git("checkout", "-q", "-b", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertEqual(self.synced(), [])


class TestRealSyncEndToEnd(HookRepo):
    """The lane's falsifier, both ways, with the real repin package: a merge in
    a throwaway detached worktree leaves the sentinel untouched; a pull in the
    main checkout rewrites it to the new main."""

    plugin_files = {
        **{f"maw/repin/{p.name}": p.read_text(encoding="utf-8")
           for p in (REPO / "maw" / "repin").glob("*.py")},
        "maw/.claude-plugin/plugin.json": '{"name": "maw", "version": "t"}\n',
    }

    def setUp(self) -> None:
        super().setUp()
        self.home = self.base / "home"
        self.pinned = self.home / ".maw" / "plugin-main"
        self.pinned.parent.mkdir(parents=True)
        self.git("worktree", "add", "-q", "-b", "plugin-pin", str(self.pinned),
                 "refs/remotes/origin/main")
        self._run([sys.executable, "-m", "repin.check", "sync"], self.root)
        found = list((self.home / ".claude" / "plugins" / "cache" / "maw" / "maw").glob(
            f"*/{check.SENTINEL_NAME}"))
        self.assertEqual(len(found), 1, "fixture: one cache version dir with a sentinel")
        self.sentinel = found[0]
        self.seed_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.sentinel.read_text().strip(), self.seed_sha)

    def _run(self, cmd, cwd, check_rc=True):
        self.env["PYTHONPATH"] = str(self.root / "maw")
        return super()._run(cmd, cwd, check_rc)

    def test_scratch_worktree_merge_leaves_the_sentinel_untouched(self) -> None:
        new_main = self.advance_remote()
        self.git("fetch", "-q", "origin")
        before = self.sentinel.stat().st_mtime_ns
        wt = self.base / "scratch"
        self.git("worktree", "add", "-q", "--detach", str(wt), self.seed_sha)
        self.git("merge", "-q", "--ff-only", "refs/remotes/origin/main", cwd=wt)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).stdout.strip(), new_main)
        self.assertEqual(self.sentinel.stat().st_mtime_ns, before)
        self.assertEqual(self.sentinel.read_text().strip(), self.seed_sha)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.pinned).stdout.strip(),
                         self.seed_sha, "the pinned worktree did not move either")

    def test_pull_in_the_main_checkout_still_deploys(self) -> None:
        new_main = self.advance_remote()
        self.git("pull", "-q", "--ff-only")
        self.assertEqual(self.sentinel.read_text().strip(), new_main)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.pinned).stdout.strip(), new_main)


class TestHookCommand(HookRepo):
    """`repin.check hook`: the installed hook is a hand copy; report and refresh it."""

    plugin_files = {
        "maw/repin/__init__.py": "",
        "maw/repin/check.py": STUB,
        "maw/init/templates/git-hooks/post-merge": TEMPLATE.read_text(encoding="utf-8"),
    }

    def setUp(self) -> None:
        super().setUp()
        self.home = self.base / "home"
        self.pinned = self.home / ".maw" / "plugin-main"
        self.pinned.parent.mkdir(parents=True)
        self.git("worktree", "add", "-q", "-b", "plugin-pin", str(self.pinned),
                 "refs/remotes/origin/main")
        self.installed = self.root / ".git" / "hooks" / "post-merge"

    def test_installed_path_is_the_source_repos_hooks_dir(self) -> None:
        self.assertEqual(check.installed_hook_path(home=self.home).resolve(),
                         self.installed.resolve())

    def test_current_stale_missing_and_not_executable(self) -> None:
        self.assertEqual(check.hook_status(home=self.home)[0], "current")
        self.installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertEqual(check.hook_status(home=self.home)[0], "stale")
        self.installed.unlink()
        self.assertEqual(check.hook_status(home=self.home)[0], "missing")
        shutil.copyfile(TEMPLATE, self.installed)
        self.installed.chmod(0o644)
        state, detail = check.hook_status(home=self.home)
        self.assertEqual(state, "stale")
        self.assertIn("not executable", detail)

    def test_install_refreshes_a_stale_hook_from_the_pinned_template(self) -> None:
        self.installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path = check.install_hook(home=self.home)
        self.assertEqual(path.resolve(), self.installed.resolve())
        self.assertEqual(self.installed.read_bytes(), TEMPLATE.read_bytes())
        self.assertTrue(self.installed.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(check.hook_status(home=self.home)[0], "current")
        self.assertEqual([p.name for p in self.installed.parent.glob(".post-merge.*")], [])

    def test_no_pinned_worktree_is_unknown_not_current(self) -> None:
        empty = self.base / "nohome"
        empty.mkdir()
        self.assertEqual(check.hook_status(home=empty)[0], "unknown")
        with self.assertRaises(check.RepinError):
            check.install_hook(home=empty)

    def test_cli_exit_codes(self) -> None:
        env = dict(self.env, PYTHONPATH=str(REPO / "maw"))

        def cli(*args: str):
            return subprocess.run([sys.executable, "-m", "repin.check", "hook", *args],
                                  env=env, capture_output=True, text=True, cwd=str(self.base))

        self.assertEqual(cli().returncode, 0)
        self.installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stale = cli()
        self.assertEqual(stale.returncode, 2)
        self.assertIn("STALE", stale.stdout)
        self.assertIn("hook --install", stale.stdout)
        fixed = cli("--install")
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        self.assertIn("CURRENT", fixed.stdout)


if __name__ == "__main__":
    unittest.main()
