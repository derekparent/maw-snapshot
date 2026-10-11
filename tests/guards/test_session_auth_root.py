"""T5 — one canonical session_auth runtime root (the #195 class).

The CLI used Path.cwd() as the runtime root, so where a token, verdict or audit
line landed depended on where the shell stood. Live evidence: a stale
2026-09-23 token sits in ~/Projects/maw, armed there while `peer-status` reads
the advisor's registered worktree. These pin the resolution order: the seat's
own registry entry (lineage-verified), then the git toplevel, then the cwd.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import session_auth  # noqa: E402
from net import registry  # noqa: E402


def _entry(repo_root: Path) -> registry.Entry:
    return registry.Entry(
        name="advisor", role="advisor", repo_root=str(repo_root), cwd=str(repo_root),
        pid=os.getpid(), tty=None, tmux_pane=None, session_id="s1",
        started_at="2026-10-01T00:00:00Z", last_seen="2026-10-01T00:00:00Z",
        repo_common_dir=str(repo_root / ".git"),
    )


class DefaultRepoRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        # Repo A: the seat's own tree plus a sibling worktree of the SAME repo.
        self.seat_tree = root / "seat-tree"
        self.seat_tree.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(self.seat_tree)]
        subprocess.run(["git", "init", "-q", str(self.seat_tree)], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        self.sibling = root / "sibling-tree"
        subprocess.run([*git, "worktree", "add", "-q", str(self.sibling)], check=True)
        (self.sibling / "sub").mkdir()
        # Repo B: a different repository altogether.
        self.other = root / "other-tree"
        self.other.mkdir()
        subprocess.run(["git", "init", "-q", str(self.other)], check=True)
        (self.other / "sub" / "dir").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"MAW_NET_NAME": "advisor"})
        env.start()
        self.addCleanup(env.stop)

    def resolve(self, cwd: Path) -> Path:
        with contextlib.chdir(cwd):
            return session_auth.default_repo_root().resolve()

    def seat(self, lineage: bool = True):
        return (mock.patch.object(registry, "load_entry", return_value=_entry(self.seat_tree)),
                mock.patch.object(registry, "verify_caller_is", return_value=lineage))

    def test_sibling_worktree_of_the_seat_repo_resolves_to_the_seat_tree(self) -> None:
        # The T5 failure: the shell is in ANOTHER worktree of the same repo.
        a, b = self.seat()
        with a, b:
            self.assertEqual(self.resolve(self.sibling / "sub"), self.seat_tree)

    def plant_token(self, tree: Path) -> None:
        """A live-looking token (this pid, open window) — never a real arm."""
        now = time.time()
        path = session_auth._runtime_path(tree, session_auth.TOKEN_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": "s1", "pid": os.getpid(), "scope": "planted test token",
            "armed_at": session_auth._iso(now - 60),
            "expires_at": session_auth._iso(now + 3600), "nonce": "n"}), encoding="utf-8")

    def status_from_repo_b(self) -> "tuple[Path, bool, str]":
        a, b = self.seat()
        with a, b:
            root = self.resolve(self.other / "sub" / "dir")
        ok, msg = session_auth.status(root)
        return root, ok, msg

    def test_falsifier_a_different_repo_never_resolves_to_the_seat_repo(self) -> None:
        # Advisor review on #297: gh targeting derives from this root, so a seat
        # cd'd into repo B must land on B (no token there: refuse), never act on
        # its own repo A's PR of the same number.
        root, ok, msg = self.status_from_repo_b()
        self.assertEqual(root, self.other)
        self.assertFalse(ok, msg)

    def test_falsifier_is_blind_to_a_token_where_the_runner_stands(self) -> None:
        # The 2026-10-01 red main: the root was resolved from the RUNNER's cwd,
        # so an armed seat running the suite read its own live token here.
        self.plant_token(self.seat_tree)
        with contextlib.chdir(self.seat_tree):
            root, ok, msg = self.status_from_repo_b()
        self.assertEqual(root, self.other)
        self.assertFalse(ok, msg)

    def test_falsifier_status_sees_a_token_in_repo_b(self) -> None:
        # Positive control: the assertion above can fail. A token in B reads armed.
        self.plant_token(self.other)
        root, ok, msg = self.status_from_repo_b()
        self.assertEqual(root, self.other)
        self.assertTrue(ok, msg)

    def test_falsifier_unverified_lineage_does_not_borrow_the_entry(self) -> None:
        # Derive the actor: a process outside the seat's tree must not resolve
        # to the seat's root just because the env names the seat.
        a, b = self.seat(lineage=False)
        with a, b:
            self.assertEqual(self.resolve(self.sibling / "sub"), self.sibling)

    def test_unregistered_subdirectory_resolves_to_git_toplevel(self) -> None:
        with mock.patch.object(registry, "load_entry", return_value=None):
            self.assertEqual(self.resolve(self.other / "sub" / "dir"), self.other)

    def test_no_seat_name_uses_toplevel(self) -> None:
        with mock.patch.dict(os.environ, {"MAW_NET_NAME": "", "MAW_BRIDGE_ROLE": ""}):
            self.assertEqual(self.resolve(self.other / "sub"), self.other)

    def test_not_a_git_repo_falls_back_to_cwd(self) -> None:
        plain = Path(self._tmp.name).resolve() / "plain"
        plain.mkdir()
        with mock.patch.object(registry, "load_entry", return_value=None), \
                mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(plain.parent)}):
            self.assertEqual(self.resolve(plain), plain)

    def test_unreadable_registry_falls_through(self) -> None:
        with mock.patch.object(registry, "load_entry", side_effect=OSError("boom")):
            self.assertEqual(self.resolve(self.other / "sub"), self.other)

    def test_cli_uses_it_and_explicit_repo_root_still_wins(self) -> None:
        out = io.StringIO()
        with mock.patch.object(session_auth, "default_repo_root",
                               return_value=self.seat_tree) as d, \
                mock.patch.object(session_auth, "status", return_value=(False, "x")) as st, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            session_auth.main(["status"])
            self.assertEqual(st.call_args.args[0], self.seat_tree)
            d.reset_mock()
            session_auth.main(["--repo-root", str(self.other), "status"])
            self.assertEqual(st.call_args.args[0], self.other)
            d.assert_not_called()


if __name__ == "__main__":
    unittest.main()
