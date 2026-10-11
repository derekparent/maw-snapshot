"""Tests for succession.checkpoint — crash-safe WIP snapshot (item 7a)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from succession import checkpoint  # noqa: E402


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class CheckpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-b", "main", self.repo], capture_output=True, check=True)
        _git(["config", "user.name", "t"], self.repo)
        _git(["config", "user.email", "t@t"], self.repo)
        _git(["config", "commit.gpgsign", "false"], self.repo)
        (Path(self.repo) / "f.txt").write_text("v1\n", encoding="utf-8")
        _git(["add", "f.txt"], self.repo)
        _git(["commit", "-m", "seed"], self.repo)
        self.addCleanup(self.tmp.cleanup)

    def test_clean_tree_no_snapshot(self) -> None:
        res = checkpoint.checkpoint("executor", cwd=self.repo)
        self.assertFalse(res.created)
        self.assertIn("clean", res.detail)

    def test_dirty_tracked_change_snapshotted_without_touching_tree(self) -> None:
        f = Path(self.repo) / "f.txt"
        f.write_text("v2 uncommitted\n", encoding="utf-8")
        res = checkpoint.checkpoint("executor", cwd=self.repo)
        self.assertTrue(res.created)
        self.assertEqual(res.ref, "refs/maw-wip/executor")
        # working tree is UNCHANGED by the snapshot
        self.assertEqual(f.read_text(encoding="utf-8"), "v2 uncommitted\n")
        # the ref resolves to a commit capturing the change
        snap = _git(["show", f"{res.ref}:f.txt"], self.repo)
        self.assertEqual(snap, "v2 uncommitted")

    def test_untracked_is_captured(self) -> None:
        # advisor Note 1: new (untracked) work MUST be snapshotted — it's the
        # crash-loss case (a fresh module). Temp-index captures it.
        (Path(self.repo) / "new.txt").write_text("brand new\n", encoding="utf-8")
        res = checkpoint.checkpoint("executor", cwd=self.repo)
        self.assertTrue(res.created)
        self.assertEqual(_git(["show", f"{res.ref}:new.txt"], self.repo), "brand new")

    def test_tracked_and_untracked_together(self) -> None:
        (Path(self.repo) / "f.txt").write_text("edited\n", encoding="utf-8")
        (Path(self.repo) / "new.txt").write_text("new file\n", encoding="utf-8")
        res = checkpoint.checkpoint("executor", cwd=self.repo)
        self.assertTrue(res.created)
        self.assertEqual(_git(["show", f"{res.ref}:f.txt"], self.repo), "edited")
        self.assertEqual(_git(["show", f"{res.ref}:new.txt"], self.repo), "new file")
        # working tree untouched
        self.assertEqual((Path(self.repo) / "new.txt").read_text(encoding="utf-8"), "new file\n")

    def test_gitignored_file_excluded(self) -> None:
        (Path(self.repo) / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        _git(["add", ".gitignore"], self.repo)
        _git(["commit", "-m", "ignore"], self.repo)
        (Path(self.repo) / "ignored.txt").write_text("secret\n", encoding="utf-8")
        (Path(self.repo) / "real.txt").write_text("kept\n", encoding="utf-8")
        res = checkpoint.checkpoint("executor", cwd=self.repo)
        self.assertTrue(res.created)
        tree = _git(["ls-tree", "-r", "--name-only", res.ref], self.repo).split()
        self.assertNotIn("ignored.txt", tree)
        self.assertIn("real.txt", tree)

    def test_not_a_git_repo_is_noop(self) -> None:
        nonrepo = str(Path(self.tmp.name) / "plain")
        os.makedirs(nonrepo)
        res = checkpoint.checkpoint("executor", cwd=nonrepo)
        self.assertFalse(res.created)  # no raise

    def test_seat_label_sanitized(self) -> None:
        f = Path(self.repo) / "f.txt"
        f.write_text("v3\n", encoding="utf-8")
        res = checkpoint.checkpoint("../../etc/passwd", cwd=self.repo)
        self.assertEqual(res.ref, "refs/maw-wip/session")  # unsafe seat → 'session'


if __name__ == "__main__":
    unittest.main()
