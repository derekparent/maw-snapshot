"""Tests for repin.check — the pinned-worktree COPY model (foundation fix).

These test BEHAVIOR with injected home/runner: cache classification for all four
states (IN_SYNC = sentinel matches pinned HEAD; STALE = sentinel missing/mismatch;
NEEDS_RECOPY = a symlink, which claude removes on session start; BROKEN = missing/
empty/no-manifest), refresh_cache (real dir + sentinel), pinned-worktree status
via a mock runner, fast_forward_pinned's exact git sequence + loud failure, and
the CLI exit codes. The one thing not covered here is the live DURABILITY gate
(does the real copy SURVIVE a Claude Code session start) — provable only in a
fresh live session; see the runbook.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from repin import check  # noqa: E402
from repin.check import CacheState  # noqa: E402

_PINNED_SHA = "a" * 40


def _mk_home() -> Path:
    return Path(tempfile.mkdtemp())


def _cache_base(home: Path) -> Path:
    base = home / ".claude" / "plugins" / "cache" / "maw" / "maw"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _pinned_plugin(home: Path, make: bool = True, manifest: bool = True) -> Path:
    p = home / ".maw" / "plugin-main" / "maw"
    if make:
        p.mkdir(parents=True, exist_ok=True)
        if manifest:
            (p / ".claude-plugin").mkdir(exist_ok=True)
            (p / ".claude-plugin" / "plugin.json").write_text(
                '{"name":"maw","version":"x"}', encoding="utf-8")
    return p


def _sha_runner(sha: str = _PINNED_SHA):
    """A runner that answers `git rev-parse HEAD` with `sha`."""
    def run(cmd, cwd=None):
        if "rev-parse" in cmd and "HEAD" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return run


def _real_copy(base: Path, label: str, *, manifest: bool = True,
               sentinel: str | None = _PINNED_SHA) -> Path:
    """Make a REAL DIRECTORY cache version dir (optionally a manifest + sentinel)."""
    vd = base / label
    vd.mkdir(parents=True, exist_ok=True)
    if manifest:
        (vd / ".claude-plugin").mkdir(exist_ok=True)
        (vd / ".claude-plugin" / "plugin.json").write_text(
            '{"name":"maw","version":"x"}', encoding="utf-8")
    if sentinel is not None:
        (vd / check.SENTINEL_NAME).write_text(sentinel + "\n", encoding="utf-8")
    return vd


class CacheStatusTest(unittest.TestCase):
    def test_in_sync_when_real_copy_sentinel_matches(self) -> None:
        home = _mk_home()
        _pinned_plugin(home)
        base = _cache_base(home)
        _real_copy(base, "0.6.0", sentinel=_PINNED_SHA)
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.IN_SYNC)
        self.assertTrue(rep.healthy)

    def test_stale_when_sentinel_mismatches(self) -> None:
        home = _mk_home()
        _pinned_plugin(home)
        base = _cache_base(home)
        _real_copy(base, "0.6.0", sentinel="b" * 40)  # != pinned HEAD
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.STALE)
        self.assertFalse(rep.healthy)

    def test_stale_when_sentinel_missing(self) -> None:
        home = _mk_home()
        _pinned_plugin(home)
        base = _cache_base(home)
        _real_copy(base, "0.6.0", sentinel=None)  # real dir + manifest, no sentinel
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.STALE)

    def test_needs_recopy_when_symlink(self) -> None:
        # The whole point: a symlink will be removed by claude on session start.
        # It must classify NEEDS_RECOPY (not healthy) so `check` FAILS on it.
        home = _mk_home()
        plugin = _pinned_plugin(home)
        base = _cache_base(home)
        os.symlink(plugin, base / "0.6.0")
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.NEEDS_RECOPY)
        self.assertFalse(rep.healthy)

    def test_broken_when_cache_base_missing(self) -> None:
        home = _mk_home()
        _pinned_plugin(home)
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.BROKEN)

    def test_broken_when_cache_base_empty(self) -> None:
        home = _mk_home()
        _pinned_plugin(home)
        _cache_base(home)  # exists but empty
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.BROKEN)

    def test_broken_when_real_dir_has_no_manifest(self) -> None:
        # An emptied/corrupt real dir (no plugin.json) is BROKEN, not merely stale.
        home = _mk_home()
        _pinned_plugin(home)
        base = _cache_base(home)
        _real_copy(base, "0.6.0", manifest=False, sentinel=_PINNED_SHA)
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.BROKEN)

    def test_worst_state_wins_symlink_beats_in_sync(self) -> None:
        # A healthy real copy + a stray symlink must NOT report IN_SYNC: the
        # symlink will be removed by claude, so check must fail.
        home = _mk_home()
        plugin = _pinned_plugin(home)
        base = _cache_base(home)
        _real_copy(base, "0.6.0", sentinel=_PINNED_SHA)
        os.symlink(plugin, base / "0.7.0")
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.NEEDS_RECOPY)
        self.assertNotEqual(rep.state, CacheState.IN_SYNC)


class RefreshCacheTest(unittest.TestCase):
    def test_refresh_produces_real_dir_with_sentinel(self) -> None:
        home = _mk_home()
        _pinned_plugin(home)
        base = _cache_base(home)  # empty -> label derived from plugin.json
        vd = check.refresh_cache(home, runner=_sha_runner(_PINNED_SHA))
        self.assertFalse(vd.is_symlink())
        self.assertTrue(vd.is_dir())
        self.assertTrue((vd / ".claude-plugin" / "plugin.json").exists())
        self.assertEqual((vd / check.SENTINEL_NAME).read_text().strip(), _PINNED_SHA)
        # and it now classifies IN_SYNC
        rep = check.cache_status(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual(rep.state, CacheState.IN_SYNC)

    def test_refresh_replaces_a_symlink_with_a_real_copy(self) -> None:
        # NEEDS_RECOPY -> refresh -> IN_SYNC: the symlink is gone, a real dir
        # with a sentinel takes its place.
        home = _mk_home()
        plugin = _pinned_plugin(home)
        base = _cache_base(home)
        os.symlink(plugin, base / "0.6.0")
        check.refresh_cache(home, runner=_sha_runner(_PINNED_SHA))
        vd = base / "0.6.0"
        self.assertFalse(vd.is_symlink())
        self.assertTrue(vd.is_dir())
        self.assertEqual((vd / check.SENTINEL_NAME).read_text().strip(), _PINNED_SHA)

    def test_refresh_updates_sentinel_on_stale_copy(self) -> None:
        home = _mk_home()
        _pinned_plugin(home)
        base = _cache_base(home)
        _real_copy(base, "0.6.0", sentinel="b" * 40)  # stale
        check.refresh_cache(home, runner=_sha_runner(_PINNED_SHA))
        self.assertEqual((base / "0.6.0" / check.SENTINEL_NAME).read_text().strip(),
                         _PINNED_SHA)

    def test_refresh_raises_when_pinned_source_missing(self) -> None:
        home = _mk_home()  # no pinned plugin at all
        _cache_base(home)
        with self.assertRaises(check.RepinError):
            check.refresh_cache(home, runner=_sha_runner(_PINNED_SHA))


class PinnedWorktreeStatusTest(unittest.TestCase):
    def _runner(self, branch: str = "main", left_right: str = "0\t0",
                fetch_rc: int = 0, rev_list_rc: int = 0):
        def run(cmd, cwd=None):
            joined = " ".join(cmd)
            if "rev-parse --abbrev-ref" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=branch + "\n", stderr="")
            if "fetch origin" in joined:
                return subprocess.CompletedProcess(cmd, fetch_rc, stdout="", stderr="")
            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, rev_list_rc,
                                                   stdout=left_right + "\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return run

    def test_missing_worktree(self) -> None:
        home = _mk_home()
        rep = check.pinned_worktree_status(self._runner(), home)
        self.assertFalse(rep.exists)
        self.assertFalse(rep.current)

    def test_current_when_on_main_and_synced(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(self._runner(branch="main", left_right="0\t0"), home)
        self.assertTrue(rep.exists)
        self.assertTrue(rep.on_main)
        self.assertTrue(rep.synced)
        self.assertTrue(rep.current)

    def test_behind_is_not_synced(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(self._runner(left_right="3\t0"), home)
        self.assertEqual(rep.behind, 3)
        self.assertEqual(rep.ahead, 0)
        self.assertFalse(rep.synced)
        self.assertFalse(rep.current)

    def test_ahead_is_not_synced(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(self._runner(left_right="0\t2"), home)
        self.assertEqual(rep.ahead, 2)
        self.assertFalse(rep.synced)

    def test_detached_at_origin_main_is_current(self) -> None:
        # MAJOR 1(a) regression: born-detached mirror at origin/main is current;
        # branch name must NOT gate `current`.
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(
            self._runner(branch="HEAD", left_right="0\t0"), home)
        self.assertFalse(rep.on_main)
        self.assertTrue(rep.synced)
        self.assertTrue(rep.current)

    def test_plugin_pin_branch_is_current(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(
            self._runner(branch="plugin-pin", left_right="0\t0"), home)
        self.assertFalse(rep.on_main)
        self.assertTrue(rep.current)

    def test_failed_fetch_is_not_current(self) -> None:
        # MAJOR 2 regression: stale ref ⇒ not current.
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(
            self._runner(branch="main", left_right="0\t0", fetch_rc=1), home)
        self.assertFalse(rep.fetched)
        self.assertFalse(rep.current)
        self.assertIn("fetch failed", rep.detail)

    def test_failed_rev_list_is_not_current(self) -> None:
        # MAJOR 3 regression: zero info ⇒ not current.
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(
            self._runner(branch="main", left_right="", rev_list_rc=1), home)
        self.assertFalse(rep.rev_list_ok)
        self.assertFalse(rep.current)
        self.assertIn("ahead/behind", rep.detail)

    def test_unparseable_rev_list_is_not_current(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        rep = check.pinned_worktree_status(
            self._runner(branch="main", left_right="garbage"), home)
        self.assertFalse(rep.rev_list_ok)
        self.assertFalse(rep.current)


class FastForwardPinnedTest(unittest.TestCase):
    def test_calls_fetch_then_ff_only_in_order(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)
        calls = []

        def run(cmd, cwd=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        self.assertTrue(check.fast_forward_pinned(run, home=home))
        joined = [" ".join(c) for c in calls]
        self.assertEqual(len(calls), 2)
        self.assertIn("fetch origin", joined[0])
        self.assertIn("merge --ff-only refs/remotes/origin/main", joined[1])
        self.assertLess(joined.index([j for j in joined if "fetch" in j][0]),
                        joined.index([j for j in joined if "merge" in j][0]))

    def test_missing_worktree_raises(self) -> None:
        home = _mk_home()
        with self.assertRaises(check.RepinError):
            check.fast_forward_pinned(lambda *a, **k: None, home=home)

    def test_ff_only_failure_surfaces_loudly(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)

        def run(cmd, cwd=None):
            if "merge" in " ".join(cmd):
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="fatal: Not possible to fast-forward")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with self.assertRaises(check.RepinError) as ctx:
            check.fast_forward_pinned(run, home=home)
        self.assertIn("ff-only", str(ctx.exception))

    def test_fetch_failure_raises(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)

        def run(cmd, cwd=None):
            if "fetch" in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no network")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with self.assertRaises(check.RepinError):
            check.fast_forward_pinned(run, home=home)

    def test_runner_oserror_raises_repinerror(self) -> None:
        home = _mk_home()
        (home / ".maw" / "plugin-main").mkdir(parents=True)

        def run(cmd, cwd=None):
            raise OSError("git: command not found")

        with self.assertRaises(check.RepinError):
            check.fast_forward_pinned(run, home=home)


class CliExitCodeTest(unittest.TestCase):
    def _run(self, argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = check.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_check_exits_0_when_in_sync_and_synced(self) -> None:
        orig_c, orig_p = check.cache_status, check.pinned_worktree_status
        check.cache_status = lambda *a, **k: check.CacheReport(
            CacheState.IN_SYNC, Path("/x"), _PINNED_SHA, "ok")
        check.pinned_worktree_status = lambda *a, **k: check.PinnedReport(
            exists=True, on_main=True, synced=True, ahead=0, behind=0, detail="ok")
        try:
            rc, out, _ = self._run(["check"])
        finally:
            check.cache_status, check.pinned_worktree_status = orig_c, orig_p
        self.assertEqual(rc, 0)
        self.assertIn("OK", out)

    def test_check_exits_2_when_cache_broken(self) -> None:
        orig_c, orig_p = check.cache_status, check.pinned_worktree_status
        check.cache_status = lambda *a, **k: check.CacheReport(
            CacheState.BROKEN, None, None, "missing")
        check.pinned_worktree_status = lambda *a, **k: check.PinnedReport(
            exists=True, on_main=True, synced=True, ahead=0, behind=0, detail="ok")
        try:
            rc, out, _ = self._run(["check"])
        finally:
            check.cache_status, check.pinned_worktree_status = orig_c, orig_p
        self.assertEqual(rc, 2)
        self.assertIn("ACTION NEEDED", out)
        self.assertIn("setup_pinned_plugin.sh", out)

    def test_check_exits_2_when_cache_is_symlink(self) -> None:
        # A symlink (NEEDS_RECOPY) must FAIL check — it is a latent empty cache.
        orig_c, orig_p = check.cache_status, check.pinned_worktree_status
        check.cache_status = lambda *a, **k: check.CacheReport(
            CacheState.NEEDS_RECOPY, Path("/x"), None, "symlink")
        check.pinned_worktree_status = lambda *a, **k: check.PinnedReport(
            exists=True, on_main=True, synced=True, ahead=0, behind=0, detail="ok")
        try:
            rc, out, _ = self._run(["check"])
        finally:
            check.cache_status, check.pinned_worktree_status = orig_c, orig_p
        self.assertEqual(rc, 2)
        self.assertIn("SYMLINK", out)
        self.assertIn("repin.check sync", out)

    def test_check_exits_2_when_cache_stale(self) -> None:
        orig_c, orig_p = check.cache_status, check.pinned_worktree_status
        check.cache_status = lambda *a, **k: check.CacheReport(
            CacheState.STALE, Path("/x"), "b" * 40, "mismatch")
        check.pinned_worktree_status = lambda *a, **k: check.PinnedReport(
            exists=True, on_main=True, synced=True, ahead=0, behind=0, detail="ok")
        try:
            rc, out, _ = self._run(["check"])
        finally:
            check.cache_status, check.pinned_worktree_status = orig_c, orig_p
        self.assertEqual(rc, 2)
        self.assertIn("STALE", out)
        self.assertIn("repin.check sync", out)

    def test_check_exits_2_when_pinned_behind(self) -> None:
        orig_c, orig_p = check.cache_status, check.pinned_worktree_status
        check.cache_status = lambda *a, **k: check.CacheReport(
            CacheState.IN_SYNC, Path("/x"), _PINNED_SHA, "ok")
        check.pinned_worktree_status = lambda *a, **k: check.PinnedReport(
            exists=True, on_main=True, synced=False, ahead=0, behind=4, detail="behind")
        try:
            rc, out, _ = self._run(["check"])
        finally:
            check.cache_status, check.pinned_worktree_status = orig_c, orig_p
        self.assertEqual(rc, 2)
        self.assertIn("sync", out)

    def test_sync_ffs_then_recopies(self) -> None:
        # sync = fast_forward_pinned THEN refresh_cache, in that order.
        order = []
        orig_ff, orig_rc = check.fast_forward_pinned, check.refresh_cache
        check.fast_forward_pinned = lambda *a, **k: order.append("ff") or True
        check.refresh_cache = lambda *a, **k: order.append("recopy") or Path("/v")
        try:
            rc, out, _ = self._run(["sync"])
        finally:
            check.fast_forward_pinned, check.refresh_cache = orig_ff, orig_rc
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["ff", "recopy"])
        self.assertIn("recopied", out)

    def test_sync_wording_says_whether_anything_moved(self) -> None:
        # A no-op sync used to print "fast-forwarded", overstating what happened.
        orig = check.fast_forward_pinned, check.refresh_cache, check.pinned_head
        check.fast_forward_pinned = lambda *a, **k: True
        check.refresh_cache = lambda *a, **k: Path("/v")
        try:
            for heads, want, not_want in (
                (["a" * 40, "a" * 40], "already at origin/main (aaaaaaaaaaaa)", "fast-forwarded"),
                (["a" * 40, "b" * 40], "fast-forwarded aaaaaaaaaaaa..bbbbbbbbbbbb", "already"),
            ):
                with self.subTest(heads=heads[0][:1] + heads[1][:1]):
                    it = iter(heads)
                    check.pinned_head = lambda *a, **k: next(it)
                    rc, out, _ = self._run(["sync"])
                    self.assertEqual(rc, 0)
                    self.assertIn(want, out)
                    self.assertNotIn(not_want, out)
                    self.assertIn("recopied", out)
        finally:
            check.fast_forward_pinned, check.refresh_cache, check.pinned_head = orig

    def test_sync_exits_1_on_failure(self) -> None:
        orig = check.fast_forward_pinned

        def boom(*a, **k):
            raise check.RepinError("ff refused")
        check.fast_forward_pinned = boom
        try:
            rc, _, err = self._run(["sync"])
        finally:
            check.fast_forward_pinned = orig
        self.assertEqual(rc, 1)
        self.assertIn("failed", err)

    def test_status_always_exits_0(self) -> None:
        orig_c, orig_p = check.cache_status, check.pinned_worktree_status
        check.cache_status = lambda *a, **k: check.CacheReport(
            CacheState.BROKEN, None, None, "missing")
        check.pinned_worktree_status = lambda *a, **k: check.PinnedReport(
            exists=False, on_main=False, synced=False, ahead=0, behind=0, detail="gone")
        try:
            rc, out, _ = self._run(["status"])
        finally:
            check.cache_status, check.pinned_worktree_status = orig_c, orig_p
        self.assertEqual(rc, 0)
        self.assertIn("NEEDS ATTENTION", out)

    def _with_runbook_roots(self, roots, argv) -> tuple[int, str, str]:
        orig = check._runbook_roots
        check._runbook_roots = lambda home=None: tuple(roots)
        try:
            return self._run(argv)
        finally:
            check._runbook_roots = orig

    def test_recipe_names_the_runbook_only_where_the_tree_has_one(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            has, lacks = Path(td) / "has", Path(td) / "lacks"
            (has / "docs").mkdir(parents=True)
            (lacks / "docs").mkdir(parents=True)
            (has / "docs" / "our-deploy-runbook.md").write_text("x", encoding="utf-8")
            rc, out, _ = self._with_runbook_roots([lacks, has], ["recipe"])
            self.assertEqual(rc, 0)
            self.assertIn("repin.check sync", out)
            self.assertIn("docs/our-deploy-runbook.md", out)
            # the obsolete force-refresh recipe must be gone
            self.assertNotIn("disable maw@maw", out)

            rc, out, _ = self._with_runbook_roots([lacks], ["recipe"])
            self.assertEqual(rc, 0)
            self.assertIn("repin.check sync", out)
            self.assertNotIn("runbook", out.lower())

    def test_two_runbooks_in_a_tree_name_neither(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            (root / "docs" / "a-deploy-runbook.md").write_text("x", encoding="utf-8")
            (root / "docs" / "b-deploy-runbook.md").write_text("x", encoding="utf-8")
            (root / "docs" / "c-deploy-runbook.md").mkdir()
            rc, out, _ = self._with_runbook_roots([root], ["recipe"])
            self.assertEqual(rc, 0)
            self.assertNotIn("runbook", out.lower())

    def test_failing_check_and_status_name_the_runbook_only_when_present(self) -> None:
        orig_c, orig_p = check.cache_status, check.pinned_worktree_status
        check.cache_status = lambda *a, **k: check.CacheReport(
            CacheState.BROKEN, None, None, "missing")
        check.pinned_worktree_status = lambda *a, **k: check.PinnedReport(
            exists=False, on_main=False, synced=False, ahead=0, behind=0, detail="gone")
        try:
            with tempfile.TemporaryDirectory() as td:
                has, lacks = Path(td) / "has", Path(td) / "lacks"
                (has / "docs").mkdir(parents=True)
                lacks.mkdir()
                (has / "docs" / "our-deploy-runbook.md").write_text("x", encoding="utf-8")
                for argv, want_rc in ((["check"], 2), (["status"], 0)):
                    rc, out, _ = self._with_runbook_roots([has], argv)
                    self.assertEqual(rc, want_rc, argv)
                    self.assertIn("see", out)
                    self.assertIn("docs/our-deploy-runbook.md", out, argv)
                    rc, out, _ = self._with_runbook_roots([lacks], argv)
                    self.assertEqual(rc, want_rc, argv)
                    self.assertNotIn("runbook", out.lower(), argv)
        finally:
            check.cache_status, check.pinned_worktree_status = orig_c, orig_p

    def test_no_shipped_repin_file_names_the_operators_runbook(self) -> None:
        # The public snapshot has no such file; a literal here would tell a
        # clone to read a file it lacks. The name is built so this test file
        # does not carry it either.
        name = "foundation-" + "deploy-runbook.md"
        maw = Path(check.__file__).resolve().parents[1]
        for rel in ("repin/check.py", "repin/setup_pinned_plugin.sh",
                    "init/templates/git-hooks/post-merge"):
            self.assertNotIn(name, (maw / rel).read_text(encoding="utf-8"), rel)


class ShellScriptSyntaxTest(unittest.TestCase):
    """`sh -n` syntax-checks for the operator shell scripts."""

    _REPIN = Path(__file__).resolve().parents[2] / "maw" / "repin"

    def _sh_n(self, script: Path) -> None:
        self.assertTrue(script.exists(), f"missing {script}")
        cp = subprocess.run(["sh", "-n", str(script)],
                            capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0,
                         f"sh -n failed for {script.name}: {cp.stderr}")

    def test_setup_pinned_plugin_sh_parses(self) -> None:
        self._sh_n(self._REPIN / "setup_pinned_plugin.sh")

    def test_validate_plugin_sh_parses(self) -> None:
        # renamed from validate_symlink.sh -> validate_plugin.sh (copy model)
        self._sh_n(self._REPIN / "validate_plugin.sh")

    def test_old_validate_symlink_name_is_gone(self) -> None:
        self.assertFalse((self._REPIN / "validate_symlink.sh").exists(),
                         "validate_symlink.sh must be renamed to validate_plugin.sh")

    def test_setup_pins_on_dedicated_branch_not_bare_main(self) -> None:
        # MAJOR 1(b) regression: worktree on a dedicated branch tracking
        # origin/main (never bare `worktree add <PINNED> main`).
        text = (self._REPIN / "setup_pinned_plugin.sh").read_text(encoding="utf-8")
        self.assertIn("plugin-pin", text)
        self.assertIn("--track -b plugin-pin", text)

    def test_setup_copies_not_symlinks(self) -> None:
        # The cache step must be a real copy + sentinel, never a symlink, never
        # `claude plugin install` on an already-registered plugin.
        text = (self._REPIN / "setup_pinned_plugin.sh").read_text(encoding="utf-8")
        self.assertIn("cp -R", text)
        self.assertIn(".maw-pinned-sha", text)
        self.assertNotIn("ln -s", text)


class SetupEndToEndTest(unittest.TestCase):
    """Run setup_pinned_plugin.sh against a temp HOME with a stub git, then
    assert cache_status(home) == IN_SYNC. Exercises the real script's
    copy-creation + sentinel leg without touching the operator's machine."""

    def test_setup_produces_in_sync_cache(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        setup = repo_root / "maw" / "repin" / "setup_pinned_plugin.sh"
        home = _mk_home()

        # Pre-build the pinned worktree dir with a manifest so the script's
        # worktree-creation leg is a no-op (the .git check passes) and we isolate
        # the copy leg.
        pinned = home / ".maw" / "plugin-main"
        _pinned_plugin(home)  # writes <pinned>/maw/.claude-plugin/plugin.json
        (pinned / ".git").write_text("gitdir: /dev/null\n", encoding="utf-8")
        _cache_base(home)  # empty cache base -> script derives a label + copies

        stub_sha = "c" * 40
        stub_dir = home / "stubbin"
        stub_dir.mkdir()
        # git stub: answer `rev-parse HEAD` with a fixed sha; everything else ok.
        git = stub_dir / "git"
        git.write_text(
            '#!/bin/sh\n'
            'for a in "$@"; do\n'
            '  if [ "$a" = "HEAD" ]; then echo ' + stub_sha + '; exit 0; fi\n'
            'done\n'
            'exit 0\n', encoding="utf-8")
        git.chmod(0o755)
        claude = stub_dir / "claude"
        claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        claude.chmod(0o755)

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = f"{stub_dir}:{env.get('PATH','')}"

        cp = subprocess.run(["sh", str(setup)], capture_output=True, text=True,
                            env=env, cwd=str(repo_root))
        self.assertEqual(cp.returncode, 0,
                         f"setup failed: {cp.stdout}\n{cp.stderr}")

        rep = check.cache_status(home, runner=_sha_runner(stub_sha))
        self.assertEqual(rep.state, CacheState.IN_SYNC,
                         f"expected IN_SYNC, got {rep.state}: {rep.detail}")
        # and it must be a REAL dir, not a symlink
        vd = rep.version_dir
        self.assertIsNotNone(vd)
        self.assertFalse(vd.is_symlink())


class PostMergeHookTest(unittest.TestCase):
    def test_hook_calls_sync_not_check_since(self) -> None:
        hook = (Path(__file__).resolve().parents[2] / "maw" / "init"
                / "templates" / "git-hooks" / "post-merge")
        text = hook.read_text(encoding="utf-8")
        self.assertIn("repin.check sync", text)
        self.assertNotIn("--since", text)
        self.assertNotIn("repin.check check", text)
        self.assertIn("exit 0", text)




class InheritedGitEnvTest(unittest.TestCase):
    """#206: a post-merge hook in a linked worktree exports GIT_DIR; the
    default runner must not let it redirect `git -C <pin>`."""

    def _repo(self, root: Path) -> Path:
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        return root

    def test_default_runner_ignores_inherited_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            pin = self._repo(Path(t) / "pin")
            caller = self._repo(Path(t) / "caller")
            leaked = {"GIT_DIR": str(caller / ".git"), "GIT_WORK_TREE": str(caller),
                      "GIT_INDEX_FILE": str(caller / ".git" / "index")}
            with unittest.mock.patch.dict(os.environ, leaked):
                cp = check._run(["git", "-C", str(pin), "rev-parse", "--absolute-git-dir"])
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(Path(cp.stdout.strip()).resolve(), (pin / ".git").resolve())

    def test_scrub_list_covers_this_git(self) -> None:
        cp = subprocess.run(["git", "rev-parse", "--local-env-vars"],
                            capture_output=True, text=True)
        if cp.returncode != 0:
            self.skipTest("git unavailable")
        missing = set(cp.stdout.split()) - set(check.GIT_LOCAL_ENV_VARS)
        self.assertEqual(missing, set())

if __name__ == "__main__":
    unittest.main()
