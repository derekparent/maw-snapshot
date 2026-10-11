"""Tests for /maw:status (init.status renderer).

Status never refuses: every repo state — bare, installed, drifted — renders
as a four-line report with exit 0. These tests pin each line's source of
truth: state.json for [1], the probe snapshot for [2], config.yaml slug for
[3], the Jev client for [4].
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

import init.status as status_mod  # noqa: E402
from init.capabilities import ALL_CAPABILITIES  # noqa: E402
from init.probe import cmd_write  # noqa: E402
from init.state import FileEntry, StateFile, write_state  # noqa: E402
from init.status import render_status  # noqa: E402

ALL_PASS = json.dumps({name: {"probe_passed": True} for name in ALL_CAPABILITIES})


class StatusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".maw").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install_state(self, files: dict[str, FileEntry] | None = None) -> None:
        write_state(
            self.repo,
            StateFile(
                installed_at="2026-06-10T00:00:00Z",
                substrate_version="0.6.0",
                profile="generic",
                files=files or {},
            ),
        )


class TestBareRepo(StatusTestCase):
    def test_renders_all_four_lines_with_not_installed(self) -> None:
        out = render_status(self.repo)
        self.assertIn("[1] Substrate: not installed (run /maw:init)", out)
        self.assertIn("[2] Capabilities:", out)
        self.assertIn("deferred-probe", out)
        self.assertIn("[3] Bridge: no .maw/config.yaml (run /maw:init)", out)
        self.assertIn("[4] Sensors:", out)

    @unittest.skipUnless(importlib.util.find_spec("jev") is not None,
                         "the jev package is not installed (left out of the release export)")
    def test_jev_line_reports_off_and_on_without_the_key(self) -> None:
        from unittest import mock
        with mock.patch.dict("os.environ", {"MAW_JEV": "0"}, clear=False):
            out = render_status(self.repo)
        self.assertIn("[4] Sensors: off", out)
        self.assertIn("no signal", out)
        with mock.patch.dict("os.environ", {"MAW_JEV": "1", "TYPESAFE_API_KEY": "apikey_x"}):
            out = render_status(self.repo)
        self.assertIn("[4] Sensors: on (jev-1.13.0)", out)
        self.assertNotIn("apikey_x", out)


class TestSubstrateLine(StatusTestCase):
    def test_installed_state_renders_version_profile_count(self) -> None:
        self.install_state(
            {
                ".claude/hooks/budget-guard.py": FileEntry(
                    template="hooks/budget-guard.py",
                    installed_sha256="0" * 64,
                    installed_at="2026-06-10T00:00:00Z",
                )
            }
        )
        out = render_status(self.repo)
        self.assertIn("installed v0.6.0", out)
        self.assertIn("profile=generic", out)
        self.assertIn("(1 files tracked)", out)
        # #201: the line now dates the transplant by PLUGIN COMMIT, because the
        # manifest version never moves. This fixture has no stamp, which is what
        # every pre-#201 install looks like — it must read as stale, not current.
        self.assertIn("(unstamped)", out)
        self.assertIn("STALE, run /maw:init --upgrade", out)

    def test_drift_siblings_counted(self) -> None:
        target_rel = ".claude/hooks/budget-guard.py"
        self.install_state(
            {
                target_rel: FileEntry(
                    template="hooks/budget-guard.py",
                    installed_sha256="0" * 64,
                    installed_at="2026-06-10T00:00:00Z",
                )
            }
        )
        sibling = self.repo / f"{target_rel}.maw-recommended"
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text("# recommended\n", encoding="utf-8")
        out = render_status(self.repo)
        self.assertIn("1 drift sibling(s) pending review", out)

    def test_corrupt_state_reported_not_raised(self) -> None:
        (self.repo / ".maw" / "state.json").write_text("{broken", encoding="utf-8")
        out = render_status(self.repo)
        self.assertIn("state.json unreadable", out)

    def test_io_layer_failure_reported_not_raised(self) -> None:
        # PermissionError escapes read_state's StateError wrapping (it
        # happens at read_text, before parsing) — "status never refuses"
        # must survive it (Codex P2 #3).
        with mock.patch.object(
            status_mod, "read_state", side_effect=PermissionError("denied")
        ):
            out = render_status(self.repo)
        self.assertIn("state.json unreadable (denied)", out)


class TestCapabilitiesLine(StatusTestCase):
    def test_fresh_snapshot_renders_probed_state(self) -> None:
        (self.repo / ".maw" / "config.yaml").write_text(
            "bridge:\n  repo_slug: \"status-test\"\n", encoding="utf-8"
        )
        import contextlib, io

        with contextlib.redirect_stdout(io.StringIO()):
            cmd_write(self.repo, ALL_PASS)
        out = render_status(self.repo)
        self.assertIn("cron_create=available", out)


class TestBridgeLine(StatusTestCase):
    def test_slug_rendered_with_dir_presence(self) -> None:
        (self.repo / ".maw" / "config.yaml").write_text(
            "bridge:\n  repo_slug: \"status-test\"\n", encoding="utf-8"
        )
        fake_bridge_home = self.repo / "bridge-home"
        (fake_bridge_home / "status-test").mkdir(parents=True)
        with mock.patch.object(status_mod, "BRIDGE_HOME", fake_bridge_home):
            out = render_status(self.repo)
        self.assertIn("slug=status-test", out)
        self.assertIn("(exists)", out)
        self.assertIn("/maw:bridge-status for turns", out)

    def test_missing_bridge_dir_reported(self) -> None:
        (self.repo / ".maw" / "config.yaml").write_text(
            "bridge:\n  repo_slug: \"status-test\"\n", encoding="utf-8"
        )
        with mock.patch.object(
            status_mod, "BRIDGE_HOME", self.repo / "bridge-home"
        ):
            out = render_status(self.repo)
        self.assertIn("(not created yet)", out)


class TestMergePostureLine(StatusTestCase):
    """[5] reads session_auth's own classifier and token reader, read-only."""

    def setUp(self) -> None:
        super().setUp()
        import subprocess
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "git@github.com:example-org/sample.git"], check=True)
        # Isolate from the real switchboard registry: no registered advisors.
        from net import registry
        for name, val in (("who", []), ("git_common_dir", "")):
            p = mock.patch.object(registry, name, lambda *a, _v=val, **k: _v)
            p.start()
            self.addCleanup(p.stop)

    def arm(self) -> None:
        import os
        from guards import session_auth as sa
        ok, msg = sa.arm(
            "merge the status posture test PRs", self.repo,
            session_id="sess-x", pid=os.getpid(), seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"},
            gate_fn=lambda *_a, **_k: True,
        )
        self.assertTrue(ok, msg)

    def token_path(self) -> Path:
        return self.repo / ".maw" / ".runtime" / "session-auth.json"

    def test_unarmed_repo_reports_no_echo_and_not_armed(self) -> None:
        out = render_status(self.repo)
        self.assertIn("[5] Merge posture: no-echo (repo=sample)", out)
        self.assertIn("NOT ARMED", out)

    def test_echo_required_repo_is_labelled_echo_required(self) -> None:
        from guards import session_auth as sa
        with mock.patch.object(sa, "ECHO_REQUIRED_REPOS", ("sample",)):
            out = render_status(self.repo)
        self.assertIn("echo-required (repo=sample)", out)

    def test_armed_token_shows_scope_and_expiry_and_writes_nothing(self) -> None:
        self.arm()
        tok = self.token_path()
        before = (tok.read_bytes(), sorted(p.name for p in tok.parent.iterdir()))
        out = render_status(self.repo)
        self.assertIn("ARMED", out)
        self.assertNotIn("NOT ARMED", out)
        self.assertIn("status posture test PRs", out)
        self.assertIn("token expires", out)
        self.assertEqual(before, (tok.read_bytes(),
                                  sorted(p.name for p in tok.parent.iterdir())))

    def test_corrupt_token_is_an_honest_line_not_a_crash(self) -> None:
        self.arm()
        self.token_path().write_text("{nope")
        out = render_status(self.repo)
        self.assertIn("[5] Merge posture:", out)
        self.assertIn("NOT ARMED [corrupt-token]", out)

    def test_broken_session_auth_reports_unknown_and_status_still_renders(self) -> None:
        from guards import session_auth as sa
        with mock.patch.object(sa, "requires_echo", side_effect=RuntimeError("boom")):
            out = render_status(self.repo)
        self.assertIn("[5] Merge posture: unknown (posture unreadable: boom)", out)
        self.assertIn("[4] Sensors:", out)


class TestRemovedLines(StatusTestCase):
    def test_no_ool_or_budget_line_is_rendered(self) -> None:
        # The OOL window and the budget ledger were never produced by anything;
        # status reports only state that exists.
        out = render_status(self.repo)
        self.assertNotIn("OOL", out)
        self.assertNotIn("Budget", out)


class TestCliEntrypoint(StatusTestCase):
    def test_main_exits_zero_and_prints_report(self) -> None:
        import contextlib, io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = status_mod.main(["--repo-root", str(self.repo)])
        self.assertEqual(rc, 0)
        self.assertIn("maw status —", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
