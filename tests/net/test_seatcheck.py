"""Tests for net.seatcheck — succession item (d) seat-env validation."""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from net import seatcheck  # noqa: E402


class CheckTests(unittest.TestCase):
    def test_matching_role_ok(self) -> None:
        rc, msgs = seatcheck.check(
            "executor",
            {"MAW_BRIDGE_ROLE": "executor", "MAW_NET_NAME": "executor"},
        )
        self.assertEqual(rc, 0)
        self.assertIn("seat-env OK", msgs[0])

    def test_generational_name_ok(self) -> None:
        rc, msgs = seatcheck.check(
            "advisor",
            {"MAW_BRIDGE_ROLE": "advisor", "MAW_NET_NAME": "advisor2"},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(msgs), 1, msgs)  # no warnings

    def test_role_contradiction_refuses(self) -> None:
        # The %5 incident shape: launched as advisor, seat command claims
        # executor — the contradiction is the only detectable spoof signal.
        rc, msgs = seatcheck.check(
            "executor",
            {"MAW_BRIDGE_ROLE": "advisor", "MAW_NET_NAME": "advisor2"},
        )
        self.assertEqual(rc, 2)
        joined = "\n".join(msgs)
        self.assertIn("CONTRADICTION", joined)
        self.assertIn("REFUSE bring-up", joined)
        self.assertIn("Layer-1", joined)
        self.assertIn("%5 vector", joined)

    def test_foreign_name_warns_but_passes(self) -> None:
        rc, msgs = seatcheck.check(
            "executor",
            {"MAW_BRIDGE_ROLE": "executor", "MAW_NET_NAME": "advisor2"},
        )
        self.assertEqual(rc, 0)
        self.assertIn("not a executor-generation name", "\n".join(msgs))

    def test_unset_role_warns_unverified(self) -> None:
        rc, msgs = seatcheck.check("executor", {})
        self.assertEqual(rc, 0)
        joined = "\n".join(msgs)
        self.assertIn("UNVERIFIED", joined)
        self.assertIn("rests on the prompt alone", joined)

    def test_unknown_seat_refused(self) -> None:
        rc, msgs = seatcheck.check("captain", {})
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", msgs[0])

    def test_default_env_reads_process_environ(self) -> None:
        with mock.patch.dict(
            "os.environ", {"MAW_BRIDGE_ROLE": "advisor"}, clear=False
        ):
            rc, _ = seatcheck.check("executor")
        self.assertEqual(rc, 2)


class MainTests(unittest.TestCase):
    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = seatcheck.main(argv, drift=lambda: None)
        return rc, out.getvalue(), err.getvalue()

    def test_usage_on_wrong_argc(self) -> None:
        rc, _, err = self._main([])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_contradiction_to_stderr_rc2(self) -> None:
        with mock.patch.dict(
            "os.environ", {"MAW_BRIDGE_ROLE": "advisor"}, clear=False
        ):
            rc, out, err = self._main(["executor"])
        self.assertEqual(rc, 2)
        self.assertIn("CONTRADICTION", err)
        self.assertEqual(out, "")

    def test_ok_to_stdout_rc0(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"MAW_BRIDGE_ROLE": "executor", "MAW_NET_NAME": "executor"},
            clear=False,
        ):
            rc, out, err = self._main(["executor"])
        self.assertEqual(rc, 0)
        self.assertIn("seat-env OK", out)
        self.assertEqual(err, "")


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd),
                         *args], capture_output=True, text=True, check=True)
    return cp.stdout.strip()


class LiveTreeDriftTests(unittest.TestCase):
    """#206 item 5, against real temp repos (no operator tree is touched)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.live = root / "live"
        self.live.mkdir()
        subprocess.run(["git", "init", "-q", str(self.live)], check=True)
        (self.live / "skill.md").write_text("v1\n")
        _git(self.live, "add", "skill.md")
        _git(self.live, "commit", "-q", "-m", "init")
        self.seat = root / "seat"
        _git(self.live, "worktree", "add", "-q", "-b", "seat", str(self.seat))

    def test_clean_live_tree_is_silent(self) -> None:
        self.assertIsNone(seatcheck.live_tree_drift(str(self.seat)))

    def test_hand_edit_and_untracked_file_warn_from_the_seat(self) -> None:
        (self.live / "skill.md").write_text("v2 hand edit\n")
        (self.live / "new-skill.md").write_text("untracked\n")
        msg = seatcheck.live_tree_drift(str(self.seat))
        self.assertIsNotNone(msg)
        self.assertIn(str(self.live), msg)
        self.assertIn("2 uncommitted path(s) (1 untracked)", msg)

    def test_in_the_live_tree_itself_is_silent(self) -> None:
        (self.live / "skill.md").write_text("v2\n")
        self.assertIsNone(seatcheck.live_tree_drift(str(self.live)))

    def test_seat_own_dirt_is_not_reported_as_live_drift(self) -> None:
        (self.seat / "skill.md").write_text("seat edit\n")
        self.assertIsNone(seatcheck.live_tree_drift(str(self.seat)))

    def test_not_a_repo_or_git_failure_is_silent(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        with mock.patch.dict("os.environ", {"GIT_CEILING_DIRECTORIES": str(plain.parent)}):
            self.assertIsNone(seatcheck.live_tree_drift(str(plain)))

        def boom(*_a, **_k):
            raise OSError("git missing")
        self.assertIsNone(seatcheck.live_tree_drift(str(self.seat), runner=boom))

    def test_any_probe_failure_is_silent(self) -> None:
        # Fail-open by contract: an unexpected error class (not OSError) in
        # either git call must not traceback bring-up.
        def boom(*_a, **_k):
            raise ValueError("unexpected")
        self.assertIsNone(seatcheck.live_tree_drift(str(self.seat), runner=boom))

        real = subprocess.run

        def status_breaks(cmd, cwd=None):
            if "status" in cmd:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
            return real(cmd, capture_output=True, text=True, cwd=cwd)
        self.assertIsNone(seatcheck.live_tree_drift(str(self.seat), runner=status_breaks))

    def test_main_prints_it_on_a_clean_seat_only(self) -> None:
        def run(argv, env):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict("os.environ", env, clear=False), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = seatcheck.main(argv, drift=lambda: "warning: live drift")
            return rc, out.getvalue(), err.getvalue()
        rc, out, _ = run(["executor"], {"MAW_BRIDGE_ROLE": "executor"})
        self.assertEqual(rc, 0)
        self.assertIn("warning: live drift", out)
        rc, out, err = run(["executor"], {"MAW_BRIDGE_ROLE": "advisor"})
        self.assertEqual(rc, 2)
        self.assertNotIn("live drift", out + err)


if __name__ == "__main__":
    unittest.main()
