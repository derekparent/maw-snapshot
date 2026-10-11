"""Tests for net.worktree — per-seat git worktrees (backlog item 1).

Real-git integration: worktree creation is stateful and git-version-
sensitive, so these drive an actual `git` against throwaway repos rather
than faking the runner. A fake runner would test our string-munging, not
the behaviour we depend on (worktree isolation, branch contention, the
linked-worktree common-dir resolution).
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from net import worktree  # noqa: E402


def _git(args: list[str], cwd: str) -> str:
    cp = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return cp.stdout.strip()


def _init_repo(path: str, *, default_branch: str = "main") -> str:
    """Init a repo at path with one commit; return the initial SHA."""
    subprocess.run(
        ["git", "init", "-b", default_branch, path],
        capture_output=True, text=True, check=True,
    )
    _git(["config", "user.name", "t"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    (Path(path) / "README").write_text("seed\n", encoding="utf-8")
    _git(["add", "README"], path)
    _git(["commit", "-m", "seed"], path)
    return _git(["rev-parse", "HEAD"], path)


def _fake_origin(path: str, sha: str, branch: str = "main") -> None:
    """Simulate an `origin/<branch>` + origin/HEAD without a network remote."""
    _git(["update-ref", f"refs/remotes/origin/{branch}", sha], path)
    _git(["symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"], path)


class WorktreeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.tmp.name) / "repo")
        os.makedirs(self.root)
        self.sha = _init_repo(self.root)
        self.wt_home = str(Path(self.tmp.name) / "worktrees")
        self.env = mock.patch.dict(os.environ, {"MAW_WORKTREE_DIR": self.wt_home})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()


class CommonDirTest(WorktreeTestBase):
    def test_common_dir_in_main_worktree(self) -> None:
        common = worktree.repo_common_dir(cwd=self.root)
        self.assertEqual(common.name, ".git")
        self.assertEqual(common.parent.resolve(), Path(self.root).resolve())

    def test_main_worktree_root(self) -> None:
        root = worktree.main_worktree_root(cwd=self.root)
        self.assertEqual(root.resolve(), Path(self.root).resolve())

    def test_common_dir_identical_from_linked_worktree(self) -> None:
        # The keystone property: a seat sitting INSIDE its linked worktree
        # must resolve the SAME logical-repo identity as the main worktree.
        res = worktree.ensure("executor", cwd=self.root)
        from_main = worktree.repo_common_dir(cwd=self.root)
        from_linked = worktree.repo_common_dir(cwd=str(res.path))
        self.assertEqual(from_main, from_linked)

    def test_common_dir_canonical_through_symlinked_repo(self) -> None:
        # Note-1 regression: a repo reached via a symlinked path must yield the
        # SAME logical-repo key as via its real path (resolve() now applied to
        # the absolute branch too). Otherwise a main-worktree seat and a
        # linked-worktree seat behind a symlink would not group.
        link = Path(self.tmp.name) / "linkrepo"
        os.symlink(self.root, link)
        from_real = worktree.repo_common_dir(cwd=self.root)
        from_link = worktree.repo_common_dir(cwd=str(link))
        self.assertEqual(from_real, from_link)
        self.assertTrue(from_real.is_absolute())

    def test_common_dir_resolves_absolute_branch_symlink(self) -> None:
        # Strengthened per advisor: the real-git test above only hits the
        # RELATIVE branch (git emits '.git' from a symlinked repo root, which
        # always resolve()'d — it passes even under the OLD buggy code). Drive
        # the ABSOLUTE branch directly with an injected runner emulating a
        # linked worktree (git emits an absolute path); if that path traverses a
        # symlink, resolve() must canonicalize it. Has teeth: fails pre-fix.
        real = Path(self.tmp.name) / "real"
        (real / ".git").mkdir(parents=True)
        link = Path(self.tmp.name) / "lnk"
        os.symlink(real, link)

        def runner(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=str(link / ".git") + "\n", stderr=""
            )

        got = worktree.repo_common_dir(cwd=self.root, runner=runner)
        self.assertEqual(got, (real / ".git").resolve())
        self.assertNotEqual(got, link / ".git")  # proves the symlink was canonicalized


class DefaultBranchTest(WorktreeTestBase):
    def test_default_branch_origin_head(self) -> None:
        _fake_origin(self.root, self.sha)
        self.assertEqual(worktree.default_branch(cwd=self.root), "main")

    def test_default_branch_local_main(self) -> None:
        self.assertEqual(worktree.default_branch(cwd=self.root), "main")

    def test_default_branch_master(self) -> None:
        other = str(Path(self.tmp.name) / "master-repo")
        os.makedirs(other)
        _init_repo(other, default_branch="master")
        self.assertEqual(worktree.default_branch(cwd=other), "master")

    def test_base_ref_prefers_origin(self) -> None:
        _fake_origin(self.root, self.sha)
        self.assertEqual(worktree.base_ref(cwd=self.root), "origin/main")

    def test_base_ref_falls_back_to_local(self) -> None:
        # No origin configured → local default branch.
        self.assertEqual(worktree.base_ref(cwd=self.root), "main")


class EnsureTest(WorktreeTestBase):
    def test_creates_worktree(self) -> None:
        res = worktree.ensure("executor", cwd=self.root)
        self.assertTrue(res.created)
        self.assertEqual(res.branch, "seat/executor")
        self.assertEqual(res.path, (Path(self.wt_home) / "repo-executor").resolve())
        self.assertTrue(res.path.is_dir())
        self.assertTrue((res.path / "README").exists())
        # git tracks it as a real worktree
        listed = worktree.list_worktrees(cwd=self.root)
        self.assertIn(str(res.path), {str(Path(p).resolve()) for p in listed})

    def test_idempotent_attach(self) -> None:
        first = worktree.ensure("executor", cwd=self.root)
        second = worktree.ensure("executor", cwd=self.root)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.path, second.path)
        self.assertEqual(second.branch, "seat/executor")

    def test_idempotent_from_inside_linked_worktree(self) -> None:
        # Launcher may invoke ensure from within an existing seat worktree.
        first = worktree.ensure("executor", cwd=self.root)
        again = worktree.ensure("executor", cwd=str(first.path))
        self.assertFalse(again.created)
        self.assertEqual(again.path, first.path)

    def test_two_seats_isolated(self) -> None:
        adv = worktree.ensure("advisor", cwd=self.root)
        exe = worktree.ensure("executor", cwd=self.root)
        self.assertNotEqual(adv.path, exe.path)
        self.assertNotEqual(adv.branch, exe.branch)
        self.assertTrue(adv.path.is_dir())
        self.assertTrue(exe.path.is_dir())
        # Distinct branches → each can hold its own uncommitted work.
        (adv.path / "a.txt").write_text("advisor work\n", encoding="utf-8")
        (exe.path / "e.txt").write_text("executor work\n", encoding="utf-8")
        self.assertFalse((exe.path / "a.txt").exists())
        self.assertFalse((adv.path / "e.txt").exists())

    def test_rejects_unknown_seat(self) -> None:
        with self.assertRaises(worktree.WorktreeError):
            worktree.ensure("../../etc", cwd=self.root)
        with self.assertRaises(worktree.WorktreeError):
            worktree.ensure("planner", cwd=self.root)

    def test_refuses_to_clobber_foreign_dir(self) -> None:
        # A non-worktree directory already at the target path is NOT clobbered.
        target = Path(self.wt_home) / "repo-executor"
        target.mkdir(parents=True)
        (target / "precious.txt").write_text("do not delete\n", encoding="utf-8")
        with self.assertRaises(worktree.WorktreeError):
            worktree.ensure("executor", cwd=self.root)
        self.assertTrue((target / "precious.txt").exists())

    def test_reuses_existing_seat_branch(self) -> None:
        # seat/executor exists already (e.g. from a prior worktree that was
        # `git worktree remove`d, leaving the branch). ensure attaches to it.
        _git(["branch", "seat/executor", self.sha], self.root)
        res = worktree.ensure("executor", cwd=self.root)
        self.assertTrue(res.created)
        self.assertEqual(res.branch, "seat/executor")

    def test_refuses_when_seat_branch_checked_out_elsewhere(self) -> None:
        # seat/executor is checked out in some OTHER path; git would refuse a
        # second checkout and silently moving it would be a cross-seat hazard.
        other = str(Path(self.tmp.name) / "elsewhere")
        _git(["worktree", "add", "-b", "seat/executor", other, self.sha], self.root)
        with self.assertRaises(worktree.WorktreeError):
            worktree.ensure("executor", cwd=self.root)


class ListTest(WorktreeTestBase):
    def test_seat_worktrees(self) -> None:
        worktree.ensure("advisor", cwd=self.root)
        worktree.ensure("executor", cwd=self.root)
        seats = {s for s, _p, _b in worktree.seat_worktrees(cwd=self.root)}
        self.assertEqual(seats, {"advisor", "executor"})

    def test_seat_worktrees_excludes_non_seat(self) -> None:
        # A plain feature worktree is not a seat worktree.
        other = str(Path(self.tmp.name) / "feat")
        _git(["worktree", "add", "-b", "feature/x", other, self.sha], self.root)
        seats = [s for s, _p, _b in worktree.seat_worktrees(cwd=self.root)]
        self.assertNotIn("x", seats)
        self.assertEqual(seats, [])

    def test_list_worktrees_detached(self) -> None:
        other = str(Path(self.tmp.name) / "det")
        _git(["worktree", "add", "--detach", other, self.sha], self.root)
        wts = worktree.list_worktrees(cwd=self.root)
        match = {str(Path(p).resolve()): b for p, b in wts.items()}
        self.assertEqual(match[str(Path(other).resolve())], "")


class BridgeIdentityTest(unittest.TestCase):
    """Acceptance tests for the advisor's bridge-split finding (item 1).

    Per-seat worktrees must NOT split the bridge: both seats' bridge identity
    has to resolve to one shared dir even though their working dirs differ and
    the untracked .maw/ does not follow a `git worktree add`.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.root = str(base / "repo")
        os.makedirs(self.root)
        self.sha = _init_repo(self.root)
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "HOME", "MAW_WORKTREE_DIR", "MAW_BRIDGE_REPO_ROOT",
                "MAW_BRIDGE_ROLE", "MAW_SESSION_ID",
            )
        }
        os.environ["HOME"] = str(self.home)
        os.environ["MAW_WORKTREE_DIR"] = str(base / "worktrees")
        # The launcher does NOT set MAW_BRIDGE_REPO_ROOT (rejected option C —
        # an env that fails open). Identity must come from the worktree itself.
        os.environ.pop("MAW_BRIDGE_REPO_ROOT", None)
        os.environ.pop("MAW_BRIDGE_ROLE", None)
        os.environ["MAW_SESSION_ID"] = "test-session"

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_both_seats_same_bridge_dir(self) -> None:
        # Acceptance #1.
        from bridge.cli import _bridge_dir, _resolve_repo_slug

        adv = worktree.ensure("advisor", cwd=self.root)
        exe = worktree.ensure("executor", cwd=self.root)
        slug_adv = _resolve_repo_slug(adv.path)
        slug_exe = _resolve_repo_slug(exe.path)
        self.assertEqual(slug_adv, slug_exe)
        self.assertEqual(_bridge_dir(slug_adv), _bridge_dir(slug_exe))
        # And it matches the MAIN tree's logical slug (no migration discontinuity).
        self.assertEqual(slug_adv, "repo")

    def test_repo_root_resolves_to_worktree_not_home(self) -> None:
        # The subtle half of the finding: without a per-worktree .maw/, the
        # bridge's cwd-walk climbs to ~/.maw. Seeding .maw/ stops the climb.
        from bridge.cli import _resolve_repo_root

        adv = worktree.ensure("advisor", cwd=self.root)
        cwd = os.getcwd()
        os.chdir(str(adv.path))
        try:
            resolved = _resolve_repo_root()
        finally:
            os.chdir(cwd)
        self.assertEqual(resolved.resolve(), adv.path.resolve())

    def test_channel_continuity_slug_stays_repo_name(self) -> None:
        # Acceptance #5: the shared slug must equal the MAIN repo's basename so
        # seats keep reading ~/.claude/bridge/<name> — existing turns survive
        # the migration. For a repo named 'maw' the slug must be 'maw'.
        from bridge.cli import _bridge_dir, _resolve_repo_slug

        maw_root = str(Path(self.tmp.name) / "maw")
        os.makedirs(maw_root)
        _init_repo(maw_root)
        res = worktree.ensure("advisor", cwd=maw_root)
        self.assertEqual(res.slug, "maw")
        slug = _resolve_repo_slug(res.path)
        self.assertEqual(slug, "maw")
        self.assertEqual(_bridge_dir(slug), self.home / ".claude" / "bridge" / "maw")

    def test_repo_root_correct_even_without_seed(self) -> None:
        # Acceptance #7 (structural half): worktrees live outside any .maw
        # ancestor, so an UNSEEDED worktree still resolves repo_root to itself,
        # not HOME. Removes the failure class instead of masking it.
        from bridge.cli import _resolve_repo_root

        adv = worktree.ensure("advisor", cwd=self.root)
        shutil.rmtree(adv.path / ".maw")  # simulate the pre-seed window
        cwd = os.getcwd()
        os.chdir(str(adv.path))
        try:
            resolved = _resolve_repo_root()
        finally:
            os.chdir(cwd)
        self.assertEqual(resolved.resolve(), adv.path.resolve())

    def test_bridgelink_and_bridge_cli_resolve_same_slug(self) -> None:
        # MAJOR 3 acceptance: net recv (bridgelink._shared_slug) and
        # /maw:bridge-in (bridge.cli._resolve_repo_slug) must resolve the SAME
        # slug for a worktree pair. Before the unify, bridge.cli returned the
        # WORKTREE basename ('repo-executor') while bridgelink returned the
        # logical 'repo' — so a net-send body was unreachable via bridge-in.
        from bridge.cli import _resolve_repo_slug
        from net import bridgelink, registry

        adv = worktree.ensure("advisor", cwd=self.root)
        exe = worktree.ensure("executor", cwd=self.root)
        # Remove the seeded config so this exercises the worktree-aware basename
        # path (the failure mode the finding names), not the config shortcut.
        shutil.rmtree(adv.path / ".maw")
        shutil.rmtree(exe.path / ".maw")
        common = str(worktree.repo_common_dir(cwd=self.root))

        def _entry(name: str, role: str, wt: Path) -> registry.Entry:
            now = "2026-06-12T00:00:00Z"
            return registry.Entry(
                name=name, role=role, repo_root=str(wt), cwd=str(wt),
                pid=os.getpid(), tty=None, tmux_pane="%1",
                session_id=f"{name}-1", started_at=now, last_seen=now,
                transport={"tmux_push": True}, repo_common_dir=common,
            )

        adv_entry = _entry("advisor", "advisor", adv.path)
        exe_entry = _entry("executor", "executor", exe.path)

        # bridge.cli resolves the worktree-basename ('repo-executor') WITHOUT the
        # unify; with it, it climbs to the shared .git's parent -> 'repo'.
        cli_slug_exe = _resolve_repo_slug(exe.path)
        cli_slug_adv = _resolve_repo_slug(adv.path)
        link_slug = bridgelink._shared_slug(exe_entry)

        self.assertEqual(cli_slug_exe, "repo")
        self.assertEqual(cli_slug_adv, "repo")
        self.assertEqual(link_slug, "repo")
        self.assertEqual(link_slug, cli_slug_exe)
        self.assertEqual(bridgelink._shared_slug(adv_entry), cli_slug_adv)

    def test_turn_round_trips_across_worktrees(self) -> None:
        # Acceptance #2: a from-advisor turn written in the advisor worktree is
        # readable by `bridge in` run in the executor worktree.
        from bridge.cli import main as bridge_main

        adv = worktree.ensure("advisor", cwd=self.root)
        exe = worktree.ensure("executor", cwd=self.root)

        cwd = os.getcwd()
        out = io.StringIO()
        sink = io.StringIO()
        try:
            os.chdir(str(adv.path))
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                rc_out = bridge_main(["out", "--role", "advisor", "--message", "ping-across"])
            os.chdir(str(exe.path))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(sink):
                rc_in = bridge_main(["in", "--role", "executor"])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc_out, 0)
        self.assertEqual(rc_in, 0)
        self.assertIn("ping-across", out.getvalue())


class SeedTest(WorktreeTestBase):
    def test_seed_creates_config(self) -> None:
        res = worktree.ensure("executor", cwd=self.root)
        self.assertEqual(res.seed_action, "created")
        cfg = res.path / ".maw" / "config.yaml"
        self.assertIn("repo_slug: repo", cfg.read_text(encoding="utf-8"))

    def test_seed_idempotent_ok(self) -> None:
        worktree.ensure("executor", cwd=self.root)
        again = worktree.ensure("executor", cwd=self.root)
        self.assertEqual(again.seed_action, "ok")

    def test_seed_preserves_other_keys_and_replaces_stale(self) -> None:
        res = worktree.ensure("executor", cwd=self.root)
        cfg = res.path / ".maw" / "config.yaml"
        cfg.write_text("repo_slug: WRONG\nother_key: keepme\n", encoding="utf-8")
        again = worktree.ensure("executor", cwd=self.root)
        self.assertEqual(again.seed_action, "updated")
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("repo_slug: repo", text)
        self.assertIn("other_key: keepme", text)
        self.assertNotIn("WRONG", text)

    def test_seed_appends_when_no_slug_line(self) -> None:
        res = worktree.ensure("executor", cwd=self.root)
        cfg = res.path / ".maw" / "config.yaml"
        cfg.write_text("other_key: v\n", encoding="utf-8")
        again = worktree.ensure("executor", cwd=self.root)
        self.assertEqual(again.seed_action, "set")
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("other_key: v", text)
        self.assertIn("repo_slug: repo", text)

    def test_attach_reseeds_deleted_config(self) -> None:
        # Acceptance #6: seed-if-absent runs on EVERY bring-up, so a worktree
        # created before this feature (or a deleted .maw/) self-heals.
        res = worktree.ensure("executor", cwd=self.root)
        shutil.rmtree(res.path / ".maw")
        again = worktree.ensure("executor", cwd=self.root)
        self.assertFalse(again.created)  # attach, not create
        self.assertEqual(again.seed_action, "created")  # config re-seeded
        self.assertTrue((again.path / ".maw" / "config.yaml").is_file())

    def test_seed_idempotent_single_line_no_dup(self) -> None:
        # Acceptance #8: ensure twice → exactly one repo_slug line, no dup.
        worktree.ensure("executor", cwd=self.root)
        res = worktree.ensure("executor", cwd=self.root)
        self.assertEqual(res.seed_action, "ok")
        text = (res.path / ".maw" / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(text.count("repo_slug:"), 1)

    def test_seed_ignores_commented_slug(self) -> None:
        # Acceptance #8 adversarial: a commented repo_slug is not a slug line.
        res = worktree.ensure("executor", cwd=self.root)
        cfg = res.path / ".maw" / "config.yaml"
        cfg.write_text("# repo_slug: commented-out\nother: v\n", encoding="utf-8")
        again = worktree.ensure("executor", cwd=self.root)
        self.assertEqual(again.seed_action, "set")
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("# repo_slug: commented-out", text)  # comment untouched
        self.assertTrue(any(ln == "repo_slug: repo" for ln in text.splitlines()))

    def test_seed_does_not_restructure_nested_slug(self) -> None:
        # Acceptance #8 adversarial: an indented (nested) repo_slug is left
        # verbatim — never restructure a user's YAML. Resolving nested keys is
        # the bridge's KD25 concern, not this seeder's.
        res = worktree.ensure("executor", cwd=self.root)
        cfg = res.path / ".maw" / "config.yaml"
        cfg.write_text("maw:\n  repo_slug: nested\n", encoding="utf-8")
        again = worktree.ensure("executor", cwd=self.root)
        self.assertEqual(again.seed_action, "set")
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("  repo_slug: nested", text)  # nested preserved verbatim
        self.assertTrue(any(ln == "repo_slug: repo" for ln in text.splitlines()))


class CliTest(WorktreeTestBase):
    def test_ensure_cli_prints_path_to_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = worktree.main(["ensure", "--seat", "executor"])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0)
        # stdout is ONLY the path (launcher cd's into it); status on stderr.
        printed = out.getvalue().strip()
        self.assertEqual(printed, str((Path(self.wt_home) / "repo-executor").resolve()))
        self.assertIn("created", err.getvalue())

    def test_ensure_cli_rejects_bad_seat(self) -> None:
        # argparse choices reject it before our code runs.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                worktree.main(["ensure", "--seat", "planner"])


class NewBranchTest(WorktreeTestBase):
    def test_bases_on_origin_main_not_the_current_branch(self) -> None:
        # #102: from a feature branch with its own commit, new_branch must base
        # the new lane on origin/main, NOT inherit the feature branch's commits.
        _fake_origin(self.root, self.sha)
        _git(["checkout", "-q", "-b", "feature/x"], self.root)
        (Path(self.root) / "f.txt").write_text("x", encoding="utf-8")
        _git(["add", "f.txt"], self.root)
        _git(["commit", "-q", "-m", "feat commit"], self.root)
        feat_sha = _git(["rev-parse", "HEAD"], self.root)
        self.assertNotEqual(feat_sha, self.sha)

        base = worktree.new_branch("lane/y", cwd=self.root)
        self.assertIn("origin/main", base)
        head = _git(["rev-parse", "HEAD"], self.root)
        self.assertEqual(head, self.sha, "new lane must sit at origin/main")
        self.assertNotEqual(head, feat_sha, "new lane must NOT inherit feature commits")
        self.assertEqual(_git(["rev-parse", "--abbrev-ref", "HEAD"], self.root), "lane/y")

    def test_invalid_name_refused(self) -> None:
        _fake_origin(self.root, self.sha)
        for bad in ("../evil", "has space", "a..b"):
            with self.assertRaises(worktree.WorktreeError):
                worktree.new_branch(bad, cwd=self.root)

    def test_refuses_in_main_worktree_when_seats_exist(self) -> None:
        # #144: run from the live/main tree of a seat-based repo, new_branch would
        # put the operator's editing tree on the lane branch — refuse instead.
        _fake_origin(self.root, self.sha)
        worktree.ensure("executor", cwd=self.root)  # a per-seat worktree now exists
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.new_branch("lane/z", cwd=self.root)
        self.assertIn("MAIN worktree", str(cm.exception))
        # the main tree stayed on main — NOT switched to the lane branch.
        self.assertEqual(_git(["rev-parse", "--abbrev-ref", "HEAD"], self.root), "main")

    def test_allowed_from_seat_worktree(self) -> None:
        # From the seat worktree (the correct place) branching proceeds normally.
        _fake_origin(self.root, self.sha)
        res = worktree.ensure("executor", cwd=self.root)
        base = worktree.new_branch("lane/w", cwd=str(res.path))
        self.assertIn("origin/main", base)
        self.assertEqual(
            _git(["rev-parse", "--abbrev-ref", "HEAD"], str(res.path)), "lane/w"
        )
        # main tree untouched.
        self.assertEqual(_git(["rev-parse", "--abbrev-ref", "HEAD"], self.root), "main")

    def test_allowed_in_single_worktree_repo_no_seats(self) -> None:
        # A plain repo with no seat worktrees has nowhere else to branch — the
        # guard must NOT over-refuse there.
        _fake_origin(self.root, self.sha)
        base = worktree.new_branch("lane/solo", cwd=self.root)
        self.assertIn("origin/main", base)
        self.assertEqual(
            _git(["rev-parse", "--abbrev-ref", "HEAD"], self.root), "lane/solo"
        )

    def _local_only_commit(self) -> str:
        # origin/main stays at the seed; HEAD gains a commit that is NOT on origin.
        _fake_origin(self.root, self.sha)
        (Path(self.root) / "base.txt").write_text("baseline", encoding="utf-8")
        _git(["add", "base.txt"], self.root)
        _git(["commit", "-q", "-m", "local baseline"], self.root)
        return _git(["rev-parse", "HEAD"], self.root)

    def test_onto_bases_on_local_only_commit(self) -> None:
        local = self._local_only_commit()
        self.assertNotEqual(local, self.sha)
        base = worktree.new_branch("lane/onto", cwd=self.root, onto=local)
        self.assertEqual(base, local)
        self.assertEqual(_git(["rev-parse", "HEAD"], self.root), local)
        self.assertEqual(_git(["rev-parse", "origin/main"], self.root), self.sha)
        self.assertEqual(_git(["rev-parse", "--abbrev-ref", "HEAD"], self.root), "lane/onto")

    def test_onto_unresolvable_refuses_and_creates_nothing(self) -> None:
        _fake_origin(self.root, self.sha)
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.new_branch("lane/nope", cwd=self.root, onto="no-such-ref")
        self.assertIn("no-such-ref", str(cm.exception))
        self.assertEqual(_git(["branch", "--list", "lane/nope"], self.root), "")
        self.assertEqual(_git(["rev-parse", "--abbrev-ref", "HEAD"], self.root), "main")

    def test_onto_option_shaped_ref_refuses(self) -> None:
        # A ref that git would read as an option never reaches checkout.
        _fake_origin(self.root, self.sha)
        for ref in ("--orphan", "-f"):
            with self.subTest(ref=ref), self.assertRaises(worktree.WorktreeError):
                worktree.new_branch("lane/opt", cwd=self.root, onto=ref)
        self.assertEqual(_git(["branch", "--list", "lane/opt"], self.root), "")
        self.assertEqual(_git(["rev-parse", "--abbrev-ref", "HEAD"], self.root), "main")

    def test_no_onto_unchanged_still_fresh_origin(self) -> None:
        local = self._local_only_commit()
        base = worktree.new_branch("lane/plain", cwd=self.root)
        self.assertIn("origin/main", base)
        head = _git(["rev-parse", "HEAD"], self.root)
        self.assertEqual(head, self.sha)
        self.assertNotEqual(head, local)

    def test_onto_still_refused_in_live_main(self) -> None:
        local = self._local_only_commit()
        worktree.ensure("executor", cwd=self.root)
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.new_branch("lane/live", cwd=self.root, onto=local)
        self.assertIn("MAIN worktree", str(cm.exception))
        self.assertEqual(_git(["branch", "--list", "lane/live"], self.root), "")

    def test_cli_onto_success_line_names_base(self) -> None:
        local = self._local_only_commit()
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = worktree.main(["branch", "lane/cli", "--onto", local])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn(f"based on {local} ({local[:7]})", out)
        self.assertIn("--onto, not origin/<default>", out)


class DoctorTest(WorktreeTestBase):
    """#147: bring-up health checks — stale base, stranded artifacts, missing venv."""

    def _find(self, findings, check):
        return next(f for f in findings if f.check == check)

    def _commit(self, name: str) -> str:
        (Path(self.root) / name).write_text("x", encoding="utf-8")
        _git(["add", name], self.root)
        _git(["commit", "-q", "-m", name], self.root)
        return _git(["rev-parse", "HEAD"], self.root)

    def test_stale_base_detected(self) -> None:
        sha2 = self._commit("a.txt")               # main advances beyond self.sha
        _git(["update-ref", "refs/remotes/origin/main", sha2], self.root)
        _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], self.root)
        _git(["checkout", "-q", "-b", "lane", self.sha], self.root)  # HEAD behind origin/main
        f = self._find(worktree.doctor(cwd=self.root), "stale-base")
        self.assertFalse(f.ok)
        self.assertIn("behind", f.detail)

    def test_current_base_ok(self) -> None:
        _fake_origin(self.root, self.sha)          # origin/main == HEAD
        f = self._find(worktree.doctor(cwd=self.root), "stale-base")
        self.assertTrue(f.ok)

    def test_stranded_artifact_flagged_noise_ignored(self) -> None:
        _fake_origin(self.root, self.sha)
        (Path(self.root) / ".gitignore").write_text("*.pdf\nvenv/\n", encoding="utf-8")
        _git(["add", ".gitignore"], self.root)
        _git(["commit", "-q", "-m", "ignore"], self.root)
        (Path(self.root) / "data.pdf").write_bytes(b"%PDF-stranded")
        (Path(self.root) / "venv").mkdir()
        (Path(self.root) / "venv" / "x").write_text("x", encoding="utf-8")
        f = self._find(worktree.doctor(cwd=self.root), "stranded-artifacts")
        self.assertFalse(f.ok)
        self.assertIn("data.pdf", f.detail)   # the real stranded data
        self.assertNotIn("venv", f.detail)    # venv is expected noise, not flagged

    def test_no_stranded_when_only_noise(self) -> None:
        _fake_origin(self.root, self.sha)
        (Path(self.root) / ".gitignore").write_text("venv/\n", encoding="utf-8")
        _git(["add", ".gitignore"], self.root)
        _git(["commit", "-q", "-m", "ignore"], self.root)
        (Path(self.root) / "venv").mkdir()
        (Path(self.root) / "venv" / "x").write_text("x", encoding="utf-8")
        f = self._find(worktree.doctor(cwd=self.root), "stranded-artifacts")
        self.assertTrue(f.ok)

    def test_missing_venv_flagged(self) -> None:
        _fake_origin(self.root, self.sha)
        f = self._find(worktree.doctor(cwd=self.root), "missing-venv")
        self.assertFalse(f.ok)

    def test_venv_present_ok(self) -> None:
        _fake_origin(self.root, self.sha)
        (Path(self.root) / "venv").mkdir()
        f = self._find(worktree.doctor(cwd=self.root), "missing-venv")
        self.assertTrue(f.ok)

    def test_doctor_survives_a_check_subprocess_error(self) -> None:
        # A git failure inside ONE check (e.g. a 30s fetch TimeoutExpired) must
        # not abort the whole doctor — it becomes a WARN, the others still run.
        _fake_origin(self.root, self.sha)

        def boom_runner(cmd, cwd=None):
            if "fetch" in cmd:
                raise subprocess.TimeoutExpired(cmd, 30)
            return worktree._run(cmd, cwd)

        findings = worktree.doctor(cwd=self.root, runner=boom_runner)
        checks = {f.check: f for f in findings}
        self.assertEqual(len(findings), 3)                      # all three ran
        self.assertFalse(checks["_check_stale_base"].ok)        # errored cleanly
        self.assertIn("errored", checks["_check_stale_base"].detail)
        self.assertIn("missing-venv", checks)                   # later checks not sunk


class RestackTest(WorktreeTestBase):
    """net.worktree restack (#117) — rebase onto fresh origin/<default>."""

    def _commit(self, fname: str, content: str, msg: str) -> str:
        (Path(self.root) / fname).write_text(content, encoding="utf-8")
        _git(["add", fname], self.root)
        _git(["commit", "-q", "-m", msg], self.root)
        return _git(["rev-parse", "HEAD"], self.root)

    def _advance_origin_main(self, fname: str, content: str, msg: str) -> str:
        """Simulate a sibling PR merging into main: a commit on main (INDEPENDENT
        of the feature branch, off the shared seed) that origin/main points at."""
        _git(["checkout", "-q", "main"], self.root)
        sha = self._commit(fname, content, msg)
        _git(["update-ref", "refs/remotes/origin/main", sha], self.root)
        return sha

    def _make_feature(self, name: str, fname: str, content: str) -> str:
        """A feature branch off the seed, one commit; leaves HEAD on `name`."""
        _git(["checkout", "-q", "-b", name, self.sha], self.root)
        return self._commit(fname, content, f"{name} work")

    # --- refusal paths (red-first: the whole surface is new) ---
    def test_refuse_detached_head(self) -> None:
        _fake_origin(self.root, self.sha)
        _git(["checkout", "-q", "--detach", self.sha], self.root)
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack(cwd=self.root)

    def test_refuse_default_branch(self) -> None:
        _fake_origin(self.root, self.sha)
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack("main", cwd=self.root)

    def test_refuse_nonexistent_branch(self) -> None:
        _fake_origin(self.root, self.sha)
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack("no/such/branch", cwd=self.root)

    def test_refuse_invalid_name(self) -> None:
        _fake_origin(self.root, self.sha)
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack("../evil", cwd=self.root)

    def test_untracked_files_do_not_refuse(self) -> None:
        # The .maw/ reality: every real seat worktree carries untracked files.
        # git rebase ignores them, so restack must too — a bare `status
        # --porcelain` check would refuse ALWAYS in production (advisor catch).
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/x", "a.txt", "feature\n")
        self._advance_origin_main("b.txt", "sibling\n", "sibling merge")
        _git(["checkout", "-q", "feat/x"], self.root)
        # a stand-in for the untracked .maw/ every seat carries
        (Path(self.root) / ".maw").mkdir()
        (Path(self.root) / ".maw" / "config.yaml").write_text("repo_slug: t\n", encoding="utf-8")
        res = worktree.restack("feat/x", cwd=self.root, push=False)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(res.action, "restacked")

    def test_refuse_tracked_modification(self) -> None:
        # A modification to a TRACKED file DOES block (rebase would refuse too).
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/x", "a.txt", "one\n")
        (Path(self.root) / "a.txt").write_text("uncommitted edit\n", encoding="utf-8")
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack("feat/x", cwd=self.root)

    def test_refuse_branch_checked_out_elsewhere(self) -> None:
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/x", "a.txt", "one\n")
        # Return to main HERE first, then park feat/x in a linked worktree
        # (git refuses to add a worktree for the currently-checked-out branch).
        _git(["checkout", "-q", "main"], self.root)
        other = str(Path(self.tmp.name) / "other-wt")
        _git(["worktree", "add", "-q", other, "feat/x"], self.root)
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack("feat/x", cwd=self.root)

    # --- the clean rebase-after-siblings-merge (the recurring bite) ---
    def test_clean_restack_after_sibling_merge(self) -> None:
        _fake_origin(self.root, self.sha)
        feat = self._make_feature("feat/x", "a.txt", "feature\n")
        # a sibling PR merges into main, touching a DIFFERENT file
        newmain = self._advance_origin_main("b.txt", "sibling\n", "sibling merge")
        _git(["checkout", "-q", "feat/x"], self.root)

        res = worktree.restack("feat/x", cwd=self.root, push=False)
        self.assertTrue(res.ok)
        self.assertEqual(res.action, "restacked")
        # feat/x now sits ON TOP of the advanced origin/main
        self.assertTrue(
            worktree._git_ok(
                ["merge-base", "--is-ancestor", newmain, "feat/x"],
                self.root, worktree._run,
            ),
            "advanced origin/main must be an ancestor of the restacked branch",
        )
        # the feature's own change survived the replay
        self.assertEqual((Path(self.root) / "a.txt").read_text(), "feature\n")
        self.assertEqual((Path(self.root) / "b.txt").read_text(), "sibling\n")
        self.assertNotEqual(feat, _git(["rev-parse", "HEAD"], self.root))

    def test_noop_restack_when_already_current(self) -> None:
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/x", "a.txt", "feature\n")  # already off origin/main tip
        res = worktree.restack("feat/x", cwd=self.root, push=False)
        self.assertTrue(res.ok)
        self.assertEqual(res.action, "restacked")

    def test_stacked_onto_replays_only_child_commits(self) -> None:
        # #138: a parent PR merged + its branch was auto-deleted; the child must
        # restack replaying ONLY its own commits onto fresh main, not the parent's.
        _fake_origin(self.root, self.sha)
        self._make_feature("parent", "p.txt", "parent\n")     # HEAD on parent
        parent_tip = _git(["rev-parse", "HEAD"], self.root)
        _git(["checkout", "-q", "-b", "child"], self.root)    # child off parent
        self._commit("c.txt", "child\n", "child commit")
        newmain = self._advance_origin_main("m.txt", "main\n", "parent merged elsewhere")

        res = worktree.restack("child", cwd=self.root, push=False, onto=parent_tip)
        self.assertTrue(res.ok, res.detail)
        _git(["checkout", "-q", "child"], self.root)
        self.assertTrue((Path(self.root) / "c.txt").exists())   # child's own commit kept
        self.assertTrue((Path(self.root) / "m.txt").exists())   # sits on advanced main
        self.assertFalse((Path(self.root) / "p.txt").exists())  # parent's commit NOT replayed
        self.assertTrue(worktree._git_ok(
            ["merge-base", "--is-ancestor", newmain, "child"], self.root, worktree._run))

    def test_onto_invalid_ref_refused(self) -> None:
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/x", "a.txt", "x\n")
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack("feat/x", cwd=self.root, push=False, onto="../evil")

    def test_onto_nonexistent_commit_refused(self) -> None:
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/x", "a.txt", "x\n")
        with self.assertRaises(worktree.WorktreeError):
            worktree.restack("feat/x", cwd=self.root, push=False, onto="deadbeefdeadbeef")

    # --- the conflict path: surface, leave in progress, never auto-resolve ---
    def test_conflict_surfaced_and_left_in_progress(self) -> None:
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/x", "shared.txt", "feature edit\n")
        # sibling merge edits the SAME file differently -> conflict on rebase
        self._advance_origin_main("shared.txt", "main edit\n", "sibling merge")
        _git(["checkout", "-q", "feat/x"], self.root)

        res = worktree.restack("feat/x", cwd=self.root, push=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.action, "conflicts")
        self.assertIn("shared.txt", res.conflicts)
        # rebase is IN PROGRESS (not auto-aborted, not auto-resolved)
        self.assertTrue((Path(self.root) / ".git" / "rebase-merge").exists()
                        or (Path(self.root) / ".git" / "rebase-apply").exists())
        _git(["rebase", "--abort"], self.root)  # cleanup

    # --- the push leg against a REAL remote ---
    def test_failed_fetch_is_named_in_the_result(self) -> None:
        # _fake_origin has no real remote, so the fetch fails: the base used is
        # the last-fetched one and the result must say it may be stale.
        _fake_origin(self.root, self.sha)
        self._make_feature("feat/f", "a.txt", "feature\n")
        res = worktree.restack("feat/f", cwd=self.root, push=False)
        self.assertTrue(res.ok, res.detail)
        self.assertIn("git fetch failed", res.detail)

    def test_clean_restack_pushes_force_with_lease(self) -> None:
        remote = str(Path(self.tmp.name) / "remote.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", remote],
                       capture_output=True, text=True, check=True)
        _git(["remote", "add", "origin", remote], self.root)
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["fetch", "-q", "origin"], self.root)
        _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], self.root)

        self._make_feature("feat/x", "a.txt", "feature\n")
        _git(["push", "-q", "-u", "origin", "feat/x"], self.root)
        # sibling merge lands on the real remote's main
        _git(["checkout", "-q", "main"], self.root)
        self._commit("b.txt", "sibling\n", "sibling merge")
        _git(["push", "-q", "origin", "main"], self.root)
        _git(["checkout", "-q", "feat/x"], self.root)

        res = worktree.restack("feat/x", cwd=self.root, push=True)
        self.assertTrue(res.ok, res.detail)
        self.assertIn("force-with-lease", res.detail)
        self.assertNotIn("fetch failed", res.detail)
        # the remote's feat/x now matches our restacked local HEAD
        local = _git(["rev-parse", "HEAD"], self.root)
        remote_sha = _git(["rev-parse", "origin/feat/x"], self.root)
        self.assertEqual(local, remote_sha)


class RestackOntoAutoTest(WorktreeTestBase):
    """`restack --onto auto` (#197): discover the old-parent SHA. Real git; gh
    is stubbed through the explicitly-passed runner (never the real CLI)."""

    def _commit(self, fname: str, content: str, msg: str) -> str:
        (Path(self.root) / fname).write_text(content, encoding="utf-8")
        _git(["add", fname], self.root)
        _git(["commit", "-q", "-m", msg], self.root)
        return _git(["rev-parse", "HEAD"], self.root)

    def setUp(self) -> None:
        super().setUp()
        _fake_origin(self.root, self.sha)
        _git(["checkout", "-q", "-b", "parent", self.sha], self.root)
        self.parent_tip = self._commit("p.txt", "parent\n", "parent work")
        _git(["checkout", "-q", "-b", "child"], self.root)
        self.child_tip = self._commit("c.txt", "child\n", "child work")
        self.gh_calls: list[list[str]] = []

    def _land_parent_on_main(self) -> str:
        """The parent PR merges (squash of one commit = same patch) and main moves."""
        _git(["checkout", "-q", "main"], self.root)
        # --no-commit + a new message: the squash commit is a DIFFERENT commit
        # (a same-second identical cherry-pick would reproduce the parent's SHA).
        _git(["cherry-pick", "--no-commit", self.parent_tip], self.root)
        _git(["commit", "-q", "-m", "parent work (#5)"], self.root)
        sha = _git(["rev-parse", "HEAD"], self.root)
        _git(["update-ref", "refs/remotes/origin/main", sha], self.root)
        _git(["checkout", "-q", "child"], self.root)
        return sha

    def _move_main_unrelated(self) -> str:
        _git(["checkout", "-q", "main"], self.root)
        sha = self._commit("m.txt", "main\n", "unrelated main work")
        _git(["update-ref", "refs/remotes/origin/main", sha], self.root)
        _git(["checkout", "-q", "child"], self.root)
        return sha

    def _runner(self, view: tuple[int, str] = (1, ""), merged: tuple[int, str] = (1, "")):
        def run(cmd, cwd=None):
            if cmd[0] == "gh":
                self.gh_calls.append(list(cmd))
                rc, out = view if cmd[1:3] == ["pr", "view"] else merged
                return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
            return worktree._run(cmd, cwd)
        return run

    def test_gh_path_found_and_ancestor_checked(self) -> None:
        newmain = self._move_main_unrelated()  # gh path must not need a cherry match
        run = self._runner(view=(0, "parent\n"),
                           merged=(0, f'[{{"headRefOid": "{self.parent_tip}", "number": 5}}]'))
        res = worktree.restack("child", cwd=self.root, runner=run, push=False, onto="auto")
        self.assertTrue(res.ok, res.detail)
        self.assertEqual((res.onto, res.onto_method), (self.parent_tip, "gh"))
        self.assertIn(["gh", "pr", "view", "child", "--json", "baseRefName", "-q", ".baseRefName"],
                      self.gh_calls)
        self.assertFalse((Path(self.root) / "p.txt").exists())  # parent NOT replayed
        self.assertTrue((Path(self.root) / "c.txt").exists())
        self.assertTrue(worktree._git_ok(["merge-base", "--is-ancestor", newmain, "child"],
                                         self.root, worktree._run))

    def test_gh_sha_not_an_ancestor_falls_to_cherry(self) -> None:
        newmain = self._land_parent_on_main()
        # gh names a real local commit that is NOT in the child's history
        run = self._runner(view=(0, "parent\n"),
                           merged=(0, f'[{{"headRefOid": "{newmain}", "number": 5}}]'))
        res = worktree.restack("child", cwd=self.root, runner=run, push=False, onto="auto")
        self.assertTrue(res.ok, res.detail)
        self.assertEqual((res.onto, res.onto_method), (self.parent_tip, "cherry"))

    def test_cherry_fallback_without_gh(self) -> None:
        self._land_parent_on_main()
        res = worktree.restack("child", cwd=self.root, runner=self._runner(), push=False, onto="auto")
        self.assertTrue(res.ok, res.detail)
        self.assertEqual((res.onto, res.onto_method), (self.parent_tip, "cherry"))
        self.assertTrue((Path(self.root) / "c.txt").exists())

    def test_child_already_based_on_default_skips_gh_list(self) -> None:
        self._land_parent_on_main()
        res = worktree.restack("child", cwd=self.root, runner=self._runner(view=(0, "main\n")),
                               push=False, onto="auto")
        self.assertEqual(res.onto_method, "cherry")
        self.assertFalse(any(c[1:3] == ["pr", "list"] for c in self.gh_calls))

    def test_nothing_found_is_refused_before_touching_the_branch(self) -> None:
        self._move_main_unrelated()  # parent never landed: cherry run starts with `+`
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.restack("child", cwd=self.root, runner=self._runner(), push=False, onto="auto")
        self.assertIn("--onto <sha>", str(cm.exception))
        self.assertEqual(_git(["rev-parse", "child"], self.root), self.child_tip)

    def test_explicit_sha_unchanged_and_never_asks_gh(self) -> None:
        self._move_main_unrelated()
        res = worktree.restack("child", cwd=self.root, runner=self._runner(), push=False,
                               onto=self.parent_tip)
        self.assertTrue(res.ok, res.detail)
        self.assertEqual((res.onto, res.onto_method), (self.parent_tip, "explicit"))
        self.assertEqual(self.gh_calls, [])

    def test_cli_prints_the_method_and_sha(self) -> None:
        self._land_parent_on_main()
        args = worktree.build_parser().parse_args(["restack", "child", "--onto", "auto", "--no-push"])
        out = io.StringIO()
        with mock.patch.object(worktree, "restack",
                               lambda b, push, onto: worktree.RestackResult(
                                   True, b, "origin/main", "restacked", "rebased",
                                   onto=self.parent_tip, onto_method="cherry")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(worktree.cmd_restack(args), 0)
        self.assertIn(f"--onto auto found the old parent {self.parent_tip} via cherry", out.getvalue())


class DuplicatePairGuardTest(WorktreeTestBase):
    """T1: `ensure` refuses a worktree a LIVE registry entry already occupies."""

    def setUp(self) -> None:
        super().setUp()
        self.bridge = Path(self.tmp.name) / "bridge"
        self.net = self.bridge / "sw-repo"
        (self.net / "registry").mkdir(parents=True)
        (self.bridge / "switchboard" / "registry").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"MAW_NET_DIR": str(self.net)})
        env.start()
        self.addCleanup(env.stop)
        from net import registry
        real = registry.live_entries_all_nets
        patch = mock.patch.object(registry, "live_entries_all_nets",
                                  side_effect=lambda base=None: real(self.bridge))
        patch.start()
        self.addCleanup(patch.stop)

    def seat_entry(self, name: str, where: Path, pid: int) -> None:
        import json
        (self.net / "registry" / f"{name}.json").write_text(json.dumps({
            "name": name, "role": "executor", "repo_root": str(where), "cwd": str(where),
            "pid": pid, "tty": None, "tmux_pane": None, "herdr_pane": "w50:p2",
            "started_at": "2026-09-23T00:00:00Z", "last_seen": "2026-09-23T00:00:00Z", "mux": "herdr",
            "session_id": "s", "model": "opus"}), encoding="utf-8")

    def test_second_ensure_into_a_live_seats_worktree_is_refused(self) -> None:
        first = worktree.ensure("executor", cwd=self.root)
        self.seat_entry("executor", first.path, os.getpid())
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.ensure("executor", cwd=self.root)
        self.assertIn("occupied by live session(s): executor (role executor", cm.exception.args[0])
        self.assertIn("pane w50:p2, net repo", cm.exception.args[0])

    def test_a_seat_in_a_subdirectory_still_occupies_it(self) -> None:
        first = worktree.ensure("executor", cwd=self.root)
        self.seat_entry("executor", first.path / "maw", os.getpid())
        with self.assertRaises(worktree.WorktreeError):
            worktree.ensure("executor", cwd=self.root)

    def test_dead_or_elsewhere_entries_do_not_block(self) -> None:
        first = worktree.ensure("executor", cwd=self.root)
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.seat_entry("executor", first.path, dead.pid)
        self.seat_entry("other", Path(self.tmp.name) / "somewhere-else", os.getpid())
        self.assertFalse(worktree.ensure("executor", cwd=self.root).created)

    def test_allow_occupied_attaches_for_a_live_handoff(self) -> None:
        first = worktree.ensure("executor", cwd=self.root)
        self.seat_entry("executor", first.path, os.getpid())
        self.assertEqual(worktree.ensure("executor", cwd=self.root, allow_occupied=True).path,
                         first.path)

    def test_env_passthrough_allows_a_live_handoff_via_the_cli(self) -> None:
        first = worktree.ensure("executor", cwd=self.root)
        self.seat_entry("executor", first.path, os.getpid())
        for val, rc_want in (("0", 1), ("1", 0)):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"MAW_ALLOW_OCCUPIED": val}), contextlib.chdir(self.root), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = worktree.main(["ensure", "--seat", "executor"])
            with self.subTest(val):
                self.assertEqual(rc, rc_want)

    def test_cli_refuses_with_exit_1_and_prints_no_path(self) -> None:
        first = worktree.ensure("executor", cwd=self.root)
        self.seat_entry("executor", first.path, os.getpid())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = worktree.main(["ensure", "--seat", "executor"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")  # the launcher's `[ -n "$_wt" ]` guard then aborts
        self.assertIn("occupied", err.getvalue())


if __name__ == "__main__":
    unittest.main()
