"""Substrate freshness — #201.

The gizmo-ios pair ran June-era doctrine for a week and nothing said so; the operator
escalated. These tests pin the warning, and — more importantly — pin the reason
the OLD stamp could not produce it: `substrate_version` is the plugin MANIFEST
label, identical on a fresh install and on a six-week-old one.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from init import substrate
from init.state import StateFile, write_state


class DriftTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._repo = tempfile.TemporaryDirectory()
        self.addCleanup(self._repo.cleanup)
        self.repo = Path(self._repo.name)

    def _install(self, *, sha: str = "abc1234", committed_at: str = "",
                 version: str = "0.6.0-pre-implementation") -> None:
        write_state(self.repo, StateFile(
            installed_at="2026-06-12T21:54:30Z", substrate_version=version,
            substrate_sha=sha, substrate_committed_at=committed_at,
            profile="generic", files={}))


class TestDriftWarning(DriftTestCase):
    def test_a_transplant_older_than_the_epoch_warns(self) -> None:
        self._install(committed_at="2026-06-12T21:00:00Z")
        warning = substrate.drift_warning(self.repo)
        self.assertIsNotNone(warning)
        self.assertIn("SUBSTRATE DRIFT", warning)
        self.assertIn("2026-06-12", warning)
        self.assertIn(substrate.DOCTRINE_EPOCH, warning)
        self.assertIn("--upgrade", warning)

    def test_a_current_transplant_is_silent(self) -> None:
        self._install(committed_at="2026-08-29T22:00:00-07:00")
        self.assertIsNone(substrate.drift_warning(self.repo))

    def test_the_epoch_day_itself_is_current(self) -> None:
        self._install(committed_at=f"{substrate.DOCTRINE_EPOCH}T00:00:01Z")
        self.assertIsNone(substrate.drift_warning(self.repo))

    def test_an_unstamped_substrate_warns_that_it_CANNOT_be_proven(self) -> None:
        """Every state.json written before this change has no stamp — and that is
        precisely the population at risk, so it must not read as current."""
        self._install(committed_at="")
        warning = substrate.drift_warning(self.repo)
        self.assertIsNotNone(warning)
        self.assertIn("no plugin-commit stamp", warning)

    def test_the_MANIFEST_version_cannot_distinguish_old_from_new(self) -> None:
        """The defect that made the old stamp useless: a six-week-old transplant
        and a fresh one carry the same manifest label, so any check against it is
        false by construction."""
        self._install(committed_at="2026-06-12T21:00:00Z")
        stale = json.loads((self.repo / ".maw" / "state.json").read_text())
        self._install(committed_at="2026-08-29T22:00:00-07:00")
        fresh = json.loads((self.repo / ".maw" / "state.json").read_text())
        self.assertEqual(stale["substrate_version"], fresh["substrate_version"])
        self.assertNotEqual(stale["substrate_committed_at"],
                            fresh["substrate_committed_at"])

    def test_no_substrate_at_all_is_not_a_drift_warning(self) -> None:
        self.assertIsNone(substrate.drift_warning(self.repo))

    def test_an_unreadable_state_file_never_raises(self) -> None:
        (self.repo / ".maw").mkdir()
        (self.repo / ".maw" / "state.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(substrate.drift_warning(self.repo))

    def test_a_garbage_timestamp_is_treated_as_unstamped(self) -> None:
        self._install(committed_at="not-a-date")
        warning = substrate.drift_warning(self.repo)
        self.assertIsNotNone(warning)
        self.assertIn("no plugin-commit stamp", warning)


class TestPluginCommit(unittest.TestCase):
    def test_reads_the_plugin_checkout_commit(self) -> None:
        sha, committed_at = substrate.plugin_commit(
            Path(__file__).resolve().parents[2])
        self.assertEqual(len(sha), 40, sha)
        self.assertRegex(committed_at, r"^\d{4}-\d{2}-\d{2}T")

    def test_a_non_git_root_stamps_unknown_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            # a bare cache COPY is not a git dir; "" is the honest answer, and an
            # empty stamp reads as un-provable rather than as current
            self.assertEqual(substrate.plugin_commit(Path(plain)), ("", ""))

    def test_the_cache_copy_falls_back_to_its_pinned_sha_sentinel(self) -> None:
        """A cache copy is not a git dir. If CLAUDE_PLUGIN_ROOT ever resolves
        there again (the #134 class), a git-only read stamps every new install as
        unstamped and the drift warning becomes permanent noise. The sentinel the
        repin sync already writes is the sha, and the pinned worktree can date
        it."""
        with tempfile.TemporaryDirectory() as cache:
            root = Path(cache)
            real_sha = subprocess.run(
                ["git", "-C", str(Path(__file__).resolve().parents[2]),
                 "rev-parse", "HEAD"],
                capture_output=True, text=True).stdout.strip()
            (root / substrate.SENTINEL_NAME).write_text(real_sha + "\n", encoding="utf-8")
            # Date it from THIS checkout, not the operator's ~/.maw/plugin-main:
            # the suite must pass on a machine that has never installed maw.
            with mock.patch.object(substrate, "PINNED_WORKTREE",
                                   Path(__file__).resolve().parents[2]):
                sha, committed_at = substrate.plugin_commit(root)
        self.assertEqual(sha, real_sha)
        self.assertRegex(committed_at, r"^\d{4}-\d{2}-\d{2}T")

    def test_a_sentinel_naming_an_unknown_commit_dates_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            root = Path(cache)
            (root / substrate.SENTINEL_NAME).write_text("0" * 40 + "\n", encoding="utf-8")
            sha, committed_at = substrate.plugin_commit(root)
        self.assertEqual(sha, "0" * 40)
        self.assertEqual(committed_at, "")

    def test_a_garbage_sentinel_is_no_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            root = Path(cache)
            (root / substrate.SENTINEL_NAME).write_text("not a sha\n", encoding="utf-8")
            self.assertEqual(substrate.plugin_commit(root), ("", ""))

    def _stamped_copy(self, cache: str, committed_at: str = "2026-10-08T06:12:00Z",
                      sha: str = "a" * 40) -> Path:
        root = Path(cache) / "maw"
        root.mkdir()
        (root / substrate.RELEASE_STAMP_NAME).write_text(
            json.dumps({"source_sha": sha, "committed_at": committed_at}), encoding="utf-8")
        return root

    def test_a_marketplace_copy_reads_the_release_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            self.assertEqual(substrate.plugin_commit(self._stamped_copy(cache)),
                             ("a" * 40, "2026-10-08T06:12:00Z"))

    def test_half_a_release_stamp_is_no_stamp(self) -> None:
        for sha, date in (("a" * 40, ""), ("a" * 40, "soon"), ("a" * 39, "2026-10-08T06:12:00Z"),
                          ("A" * 40, "2026-10-08T06:12:00Z"), (7, "2026-10-08T06:12:00Z")):
            with self.subTest(sha=sha, date=date), tempfile.TemporaryDirectory() as cache:
                root = Path(cache) / "maw"
                root.mkdir()
                (root / substrate.RELEASE_STAMP_NAME).write_text(
                    json.dumps({"source_sha": sha, "committed_at": date}), encoding="utf-8")
                self.assertEqual(substrate.plugin_commit(root), ("", ""))
        with tempfile.TemporaryDirectory() as cache:
            root = Path(cache) / "maw"
            root.mkdir()
            for junk in ("", "[]", "{not json", '"a string"'):
                (root / substrate.RELEASE_STAMP_NAME).write_text(junk, encoding="utf-8")
                self.assertEqual(substrate.plugin_commit(root), ("", ""), junk)

    def test_a_stamp_is_dated_only_in_the_exact_shape_the_export_writes(self) -> None:
        """A stale copy must never read as current. So a stamp that is not
        exactly what the export writes is no stamp, whatever it looks like."""
        good_sha, good_date = "c" * 40, "2026-10-01T12:00:00Z"
        bad = {
            "date with a tail": (good_sha, "2026-10-10 lol"),
            "date only, no time": (good_sha, "2026-10-10"),
            "not a real day": (good_sha, "9999-99-99T00:00:00Z"),
            "not a real time": (good_sha, "2026-10-10T25:61:61Z"),
            "an offset, not Z": (good_sha, "2026-10-10T14:41:51-07:00"),
            "no zone at all": (good_sha, "2026-10-10T14:41:51"),
            "date then newline": (good_sha, good_date + "\n"),
            "digits that are not ASCII": (good_sha, "\u0662\u0660\u0662\u0666-10-01T12:00:00Z"),
            "sha then newline": (good_sha + "\n", good_date),
            "sha with a space": (" " + good_sha, good_date),
            "upper-case sha": ("C" * 40, good_date),
        }
        for name, (sha, date) in bad.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as cache:
                self.assertEqual(
                    substrate.plugin_commit(self._stamped_copy(cache, date, sha)), ("", ""))
        with tempfile.TemporaryDirectory() as cache:
            self.assertEqual(substrate.plugin_commit(self._stamped_copy(cache, good_date, good_sha)),
                             (good_sha, good_date))

    def test_a_repin_sentinel_wins_over_a_release_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            root = self._stamped_copy(cache)
            (root / substrate.SENTINEL_NAME).write_text("0" * 40 + "\n", encoding="utf-8")
            self.assertEqual(substrate.plugin_commit(root), ("0" * 40, ""))

    def test_a_copy_inside_someone_elses_repo_is_not_dated_by_that_repo(self) -> None:
        """A home directory kept in git, with the plugin cache under it: the
        enclosing repo's HEAD is not the plugin's commit."""
        with tempfile.TemporaryDirectory() as home:
            git = ["git", "-C", home, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                   "-c", "commit.gpgsign=false"]
            subprocess.run([*git, "init", "-q"], check=True)
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "dotfiles"], check=True)
            deep = Path(home) / ".claude" / "plugins" / "cache"
            deep.mkdir(parents=True)
            self.assertEqual(substrate.plugin_commit(deep), ("", ""))
            root = self._stamped_copy(str(deep))
            self.assertEqual(substrate.plugin_commit(root)[0], "a" * 40)

    def test_a_git_failure_never_raises(self) -> None:
        with mock.patch("init.substrate.subprocess.run",
                        side_effect=OSError("no git")):
            self.assertEqual(substrate.plugin_commit(Path("/anywhere")), ("", ""))


class TestFreshInstallIsNotStale(DriftTestCase):
    """End to end on the two lines a stranger sees: /maw:status and the
    session-start drift hook."""

    def _state_from(self, committed_at: str) -> None:
        with tempfile.TemporaryDirectory() as cache:
            root = Path(cache) / "maw"
            root.mkdir()
            (root / substrate.RELEASE_STAMP_NAME).write_text(json.dumps(
                {"source_sha": "b" * 40, "committed_at": committed_at}), encoding="utf-8")
            sha, date = substrate.plugin_commit(root)
        self._install(sha=sha, committed_at=date)

    def test_a_substrate_installed_from_a_stamped_copy_is_current(self) -> None:
        from init import status
        self._state_from("2026-10-08T06:12:00Z")
        self.assertIsNone(substrate.drift_warning(self.repo))
        line = status._substrate_line(self.repo)
        self.assertIn("(2026-10-08)", line)
        self.assertNotIn("STALE", line)
        self.assertNotIn("unstamped", line)

    def test_a_really_stale_install_still_reads_stale(self) -> None:
        from init import status
        self._state_from("2026-06-12T00:00:00Z")
        self.assertIn("SUBSTRATE DRIFT", substrate.drift_warning(self.repo) or "")
        self.assertIn("STALE", status._substrate_line(self.repo))

    def test_a_copy_with_no_stamp_at_all_still_reads_unprovable(self) -> None:
        from init import status
        with tempfile.TemporaryDirectory() as plain:
            sha, date = substrate.plugin_commit(Path(plain))
        self._install(sha=sha, committed_at=date)
        self.assertIn("CANNOT be shown", substrate.drift_warning(self.repo) or "")
        self.assertIn("STALE", status._substrate_line(self.repo))


class TestCliAndHook(DriftTestCase):
    def test_cli_prints_the_warning_and_always_exits_zero(self) -> None:
        self._install(committed_at="2026-06-12T21:00:00Z")
        root = Path(__file__).resolve().parents[2] / "maw"
        cp = subprocess.run(
            ["python3", "-m", "init.substrate", "--repo-root", str(self.repo)],
            capture_output=True, text=True, env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"})
        self.assertEqual(cp.returncode, 0)
        self.assertIn("SUBSTRATE DRIFT", cp.stdout)

    def test_cli_is_silent_and_zero_for_a_current_repo(self) -> None:
        self._install(committed_at="2026-08-29T22:00:00-07:00")
        root = Path(__file__).resolve().parents[2] / "maw"
        cp = subprocess.run(
            ["python3", "-m", "init.substrate", "--repo-root", str(self.repo)],
            capture_output=True, text=True, env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"})
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "")

    def test_the_hook_is_registered_at_sessionstart(self) -> None:
        hooks = json.loads(
            (Path(__file__).resolve().parents[2] / "maw" / "hooks" / "hooks.json")
            .read_text(encoding="utf-8"))
        commands = [h["command"] for h in hooks["hooks"]["SessionStart"][0]["hooks"]]
        self.assertTrue(any("substrate-drift.sh" in c for c in commands), commands)

    def test_the_hook_exits_zero_on_a_repo_with_no_substrate(self) -> None:
        hook = Path(__file__).resolve().parents[2] / "maw" / "hooks" / "substrate-drift.sh"
        cp = subprocess.run(["bash", str(hook)], capture_output=True, text=True,
                            env={"MAW_BRIDGE_REPO_ROOT": str(self.repo),
                                 "CLAUDE_PLUGIN_ROOT": "/nonexistent",
                                 "PATH": "/usr/bin:/bin"})
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "")

    def test_the_hook_exits_zero_when_the_plugin_root_is_broken(self) -> None:
        """A session start must survive a hook that cannot run at all."""
        self._install(committed_at="2026-06-12T21:00:00Z")
        hook = Path(__file__).resolve().parents[2] / "maw" / "hooks" / "substrate-drift.sh"
        cp = subprocess.run(["bash", str(hook)], capture_output=True, text=True,
                            env={"MAW_BRIDGE_REPO_ROOT": str(self.repo),
                                 "CLAUDE_PLUGIN_ROOT": "/nonexistent",
                                 "PATH": "/usr/bin:/bin"})
        self.assertEqual(cp.returncode, 0)


if __name__ == "__main__":
    unittest.main()
