"""Thesis tests #1/#2 for the push-gate (PRD v0.7 §7).

Two layers, two test classes:

1. TestPrePushHookVerdicts — the local `pre-push` git hook (rendered from
   maw/init/templates/git-hooks/pre-push) is exercised as a real process
   with the exact stdin contract git gives it, across the full env matrix.
   This pins the hook's verdict table — including the verified M1
   limitation: a subagent that *inherits* MAW_HUMAN=1 is ALLOWED at this
   layer (fail-open, re-confirmed on Claude Code 2.1.170, 2026-06-10).
   The local hook is fast feedback, not the guarantee.

2. TestRemoteGateSurvivesBypasses — the guarantee layer (Path 1: remote
   branch protection, live on github.com:example-org/maw since
   2026-06-10) is modeled hermetically: a bare repo whose pre-receive
   rejects refs/heads/main stands in for GitHub's GH006. The §7 thesis
   bypass vectors — `git push --no-verify` and
   `git -c core.hooksPath=/dev/null push` — are client-side; the tests
   assert they cannot defeat a server-side reject. (The third §7 vector,
   an MCP github-push tool, is also server-side-covered: any API write to
   a protected branch gets the same rejection. Not simulatable here.)

Stdlib-only, hermetic (tmpdir repos, scrubbed env, no network).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from init.substitution import substitute_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_TEMPLATE = REPO_ROOT / "maw" / "init" / "templates" / "git-hooks" / "pre-push"

SHA_A = "1" * 40
SHA_B = "2" * 40


def _scrubbed_env(**overrides: str) -> dict[str, str]:
    """Env with every MAW_* marker removed, then explicit overrides applied.

    The suite itself runs inside an mw-launched session (MAW_HUMAN=1
    inherited), so starting from os.environ would make the default-deny
    cases pass or fail depending on who ran the tests.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAW_")}
    env.update(overrides)
    return env


class TestPrePushHookVerdicts(unittest.TestCase):
    """Thesis test #1 surface: the git-process-layer hook's verdict table."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.hook = Path(cls.tmpdir.name) / "pre-push"
        rendered = substitute_file(
            HOOK_TEMPLATE,
            {"deploy_branch": "main", "alias_suggestion": "mw"},
        )
        cls.hook.write_text(rendered, encoding="utf-8")
        cls.hook.chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()

    def _run(self, ref: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        line = f"{ref} {SHA_A} {ref} {SHA_B}\n"
        return subprocess.run(
            ["bash", str(self.hook), "origin", "git@example.invalid:x.git"],
            input=line,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_agent_marker_denied_even_with_human_marker(self) -> None:
        # Agent precedence: MAW_AGENT=1 blocks even if MAW_HUMAN=1 bled in.
        result = self._run(
            "refs/heads/main", _scrubbed_env(MAW_AGENT="1", MAW_HUMAN="1")
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("MAW_AGENT=1", result.stderr)

    def test_agent_marker_alone_denied(self) -> None:
        result = self._run("refs/heads/main", _scrubbed_env(MAW_AGENT="1"))
        self.assertEqual(result.returncode, 1)

    def test_no_markers_default_denied(self) -> None:
        # Cron-spawned sessions, sub-shells, anything not operator-launched.
        result = self._run("refs/heads/main", _scrubbed_env())
        self.assertEqual(result.returncode, 1)
        self.assertIn("default-deny", result.stderr)

    def test_human_marker_allowed(self) -> None:
        result = self._run("refs/heads/main", _scrubbed_env(MAW_HUMAN="1"))
        self.assertEqual(result.returncode, 0)

    def test_master_also_protected(self) -> None:
        result = self._run("refs/heads/master", _scrubbed_env())
        self.assertEqual(result.returncode, 1)

    def test_feature_branch_unaffected_without_markers(self) -> None:
        result = self._run("refs/heads/feature/x", _scrubbed_env())
        self.assertEqual(result.returncode, 0)

    def test_dev_branch_unaffected_for_agents(self) -> None:
        result = self._run("refs/heads/dev", _scrubbed_env(MAW_AGENT="1"))
        self.assertEqual(result.returncode, 0)

    def test_inherited_human_marker_fails_open_at_this_layer(self) -> None:
        # M1, re-verified 2026-06-10 on 2.1.170: subagents inherit
        # MAW_HUMAN=1 and MAW_AGENT is never set, so the env layer ALLOWS
        # a subagent push to main. This test pins the limitation so any
        # future fix (harness stripping the marker, sentinel redesign)
        # shows up as an intentional test change, not a silent drift.
        # The guarantee is the remote layer (TestRemoteGateSurvivesBypasses).
        result = self._run("refs/heads/main", _scrubbed_env(MAW_HUMAN="1"))
        self.assertEqual(result.returncode, 0, "env layer is known to fail open")

    def test_multi_ref_push_denies_if_any_ref_protected(self) -> None:
        lines = (
            f"refs/heads/feature/x {SHA_A} refs/heads/feature/x {SHA_B}\n"
            f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n"
        )
        result = subprocess.run(
            ["bash", str(self.hook), "origin", "git@example.invalid:x.git"],
            input=lines,
            env=_scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)


class TestRemoteGateSurvivesBypasses(unittest.TestCase):
    """Thesis test #2 surface: client-side bypasses vs a server-side reject.

    A bare repo's pre-receive hook stands in for GitHub branch protection
    (live GH006 verification of the real remote performed 2026-06-10; see
    findings/2026-06-10-pushgate-green.md).
    """

    PRE_RECEIVE = (
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        '  if [ "$ref" = "refs/heads/main" ]; then\n'
        '    echo "GH006-sim: refs/heads/main is PR-only" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.origin = root / "origin.git"
        self.work = root / "work"

        self.env = _scrubbed_env(
            HOME=str(root),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
        )

        self._git(["init", "-q", "--bare", str(self.origin)])
        pre_receive = self.origin / "hooks" / "pre-receive"
        pre_receive.write_text(self.PRE_RECEIVE, encoding="utf-8")
        pre_receive.chmod(0o755)

        self._git(["init", "-q", "-b", "main", str(self.work)])
        self._git(
            ["-C", str(self.work), "commit", "--allow-empty", "-q", "-m", "seed"]
        )
        self._git(
            ["-C", str(self.work), "remote", "add", "origin", str(self.origin)]
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_plain_push_to_main_rejected(self) -> None:
        result = self._git(["-C", str(self.work), "push", "origin", "main"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GH006-sim", result.stderr)

    def test_no_verify_bypass_still_rejected(self) -> None:
        result = self._git(
            ["-C", str(self.work), "push", "--no-verify", "origin", "main"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GH006-sim", result.stderr)

    def test_hookspath_null_bypass_still_rejected(self) -> None:
        result = self._git(
            [
                "-C",
                str(self.work),
                "-c",
                "core.hooksPath=/dev/null",
                "push",
                "origin",
                "main",
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GH006-sim", result.stderr)

    def test_inherited_human_marker_cannot_defeat_server_reject(self) -> None:
        # The composed thesis: the exact env that fails open at the local
        # layer (inherited MAW_HUMAN=1) still cannot land a commit on main.
        env = dict(self.env)
        env["MAW_HUMAN"] = "1"
        result = subprocess.run(
            ["git", "-C", str(self.work), "push", "origin", "main"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GH006-sim", result.stderr)

    def test_feature_branch_push_succeeds(self) -> None:
        result = self._git(
            ["-C", str(self.work), "push", "origin", "main:feature/x"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
