"""Subprocess-level integration test for the session-auth merge gate (#85/#87).

REQUIRED by #85. The 1052 in-process tests call arm() and check() in the SAME
python process, so they inject session_id+pid and never exercise the thing that
actually breaks in production: arm and check are SEPARATE `python3 -m`
invocations. os.getpid() gave each a different, transient pid that could never
match, so check() could never PASS (#85), and _repo_slug read the per-seat
worktree dir name so every seat fell through to echo-required (#87).

This test drives the REAL CLI across process boundaries, from a
per-seat-worktree-shaped path, with `gh` and `sudo` shimmed on PATH (the
module's own docstring names PATH as ground-truth-against-confusion, not against
a determined shim — a test is exactly the confused-but-cooperative caller it is
built for). It asserts:
  * arm (one process) -> check (another) PASSES with valid bindings — RED on
    pre-fix main (wrong-pid), GREEN after the registry-identity fix;
  * the classification slug comes from the origin remote, not the worktree dir;
  * the deployment-shape refusal codes fire (wrong-session, no-token,
    no-verdict, not-approved, sha-drift, echo-required).

(No-self-merge was RETIRED 2026-07-17 — the arm is the authorization; PR
seat-authorship is not a boundary the operator wants enforced. There is no self-merge
refusal to exercise here anymore.)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAW_PKG = REPO_ROOT / "maw"

SHA_A = "a" * 40
SHA_B = "b" * 40


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class IntegrationBase(unittest.TestCase):
    """A per-seat-worktree-shaped repo + shimmed gh/sudo + a seeded registry."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="maw-sessauth-int-"))
        self.addCleanup(self._rmtree, self.tmp)

        # A repo whose DIRECTORY NAME is a per-seat worktree ('maw-advisor') but
        # whose ORIGIN REMOTE is 'maw' — the exact shape that made #87 bite.
        self.repo = self.tmp / "maw-advisor"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("remote", "add", "origin", "git@github.com:example-org/maw.git")

        # Shim bin: a fake gh (canned author + headRefOid, both env-tunable) and
        # a fake sudo (the prompted call exits 0, so the Touch ID gate opens without a real
        # fingerprint). Real git stays reachable further down PATH.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        _write_exe(
            self.bin / "gh",
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "author = os.environ.get('GH_FAKE_AUTHOR', 'example-org')\n"
            "sha = os.environ.get('GH_FAKE_SHA', '%s')\n"
            "base = os.environ.get('GH_FAKE_BASE', 'main')\n"
            "argv = sys.argv[1:]\n"
            "if 'repo' in argv and 'view' in argv:\n"
            "    print(json.dumps({'defaultBranchRef': {'name': 'main'}}))\n"
            "elif 'list' in argv:\n"
            "    print('[]')\n"
            "else:\n"
            "    print(json.dumps({'author': {'login': author}, 'headRefOid': sha,\n"
            "                      'baseRefName': base, 'headRefName': 'fix/some-branch',\n"
            "                      'title': 'an integration PR'}))\n"
            % SHA_A,
        )
        # A real host needs authentication: `sudo -n` (no prompt) fails, the
        # prompted call succeeds (the human authenticated).
        _write_exe(self.bin / "sudo",
                   '#!/bin/sh\n[ "$1" = "-n" ] && exit 1\nexit 0\n')

        # A registry with a live advisor entry. pid is the ancestor find_claude_
        # ancestor() will resolve FROM A CHILD of this test process — the same
        # ancestor the arm/check subprocesses (also our children) will resolve,
        # so verify_caller_is passes deterministically whether or not we run
        # under a real claude session.
        self.net_dir = self.tmp / "net"
        (self.net_dir / "registry").mkdir(parents=True)
        self.session_id = "sess-integration-xyz"
        self.seat_pid = self._probe_ancestor_pid()
        self._write_registry_entry(self.session_id, self.seat_pid)

    def _rmtree(self, p: Path) -> None:
        import shutil

        shutil.rmtree(p, ignore_errors=True)

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True, capture_output=True, text=True,
        )

    def _base_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["PYTHONPATH"] = str(MAW_PKG)
        env["MAW_NET_DIR"] = str(self.net_dir)
        env["MAW_NET_NAME"] = "advisor"
        env["MAW_BRIDGE_ROLE"] = "advisor"
        # No MAW_SESSION_ID: the registry supplies identity now (that absence was
        # itself part of #85's live bite).
        env.pop("MAW_SESSION_ID", None)
        return env

    def _probe_ancestor_pid(self) -> int:
        cp = subprocess.run(
            [sys.executable, "-c",
             "from net import registry; print(registry.find_claude_ancestor()[0])"],
            env=self._base_env(), capture_output=True, text=True, check=True,
        )
        return int(cp.stdout.strip())

    def _write_registry_entry(self, session_id: str, pid: int) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry = {
            "name": "advisor", "role": "advisor",
            "repo_root": str(self.repo), "cwd": str(self.repo),
            "pid": pid, "tty": None, "tmux_pane": None,
            "session_id": session_id,
            "started_at": now, "last_seen": now,
            "transport": {"tmux_push": False, "herdr_push": False},
        }
        (self.net_dir / "registry" / "advisor.json").write_text(
            json.dumps(entry), encoding="utf-8"
        )

    def _run(self, *args: str, env: dict[str, str] | None = None
             ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "guards.session_auth",
             "--repo-root", str(self.repo), *args],
            env=env or self._base_env(),
            capture_output=True, text=True, cwd=str(self.repo),
        )

    def _arm(self, scope: str = "merge PRs 88 and 89 for the fix run",
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return self._run("arm", "--scope", scope, env=env)

    def _record_verdict(self, pr: int, verdict: str = "approved",
                        env: dict[str, str] | None = None
                        ) -> subprocess.CompletedProcess:
        return self._run("record-verdict", "--pr", str(pr), "--verdict", verdict,
                         env=env)

    def _check(self, pr: int, env: dict[str, str] | None = None,
               extra: list[str] | None = None) -> subprocess.CompletedProcess:
        return self._run("check", "--pr", str(pr), *(extra or []), env=env)

    def _merge(self, pr: int, *extra: str,
               env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return self._run("merge", "--pr", str(pr), *extra, env=env)


class TestArmCheckAcrossProcesses(IntegrationBase):
    def test_arm_then_check_passes_across_processes(self) -> None:
        """The headline #85 case: arm (proc 1) -> check (proc 2) PASSES.

        RED on pre-fix main — arm records its own pid, check compares its own
        different pid -> wrong-pid, forever. GREEN after the registry-identity
        fix. author 'example-org' != acting seat 'advisor', so this is not a
        self-merge; repo 'maw' is no-echo.
        """
        armed = self._arm()
        self.assertEqual(armed.returncode, 0,
                         f"arm failed: {armed.stdout}\n{armed.stderr}")
        rec = self._record_verdict(88, "approved")
        self.assertEqual(rec.returncode, 0, f"record-verdict failed: {rec.stderr}")
        checked = self._check(88)
        self.assertEqual(
            checked.returncode, 0,
            f"check should PASS across processes but refused:\n{checked.stderr}",
        )
        self.assertIn("merge auth OK", checked.stdout)

    def test_slug_from_remote_not_worktree_dirname(self) -> None:
        """#87: the repo dir is 'maw-advisor' but the slug must be 'maw'.

        Historical note: before the 2026-07-24 no-echo-default ruling this
        discriminated behaviorally (a dirname slug was unclassified → forced
        an echo-required refusal). With every slug no-echo now, the pass +
        no-echo-mention assertions remain as a regression floor; the slug
        resolution itself is covered by the _repo_slug unit tests.
        """
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._record_verdict(88).returncode, 0)
        checked = self._check(88)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertNotIn("echo-required", checked.stderr)


class TestDeploymentShapeRefusals(IntegrationBase):
    def test_no_token_refuses(self) -> None:
        # No arm at all.
        self.assertEqual(self._record_verdict(88).returncode, 0)
        checked = self._check(88)
        self.assertEqual(checked.returncode, 1)
        self.assertIn("no-token", checked.stderr)

    def test_wrong_session_refuses(self) -> None:
        """Arm, then the seat's registry session_id changes -> wrong-session.

        The token was bound to the arming session_id; a later check derives a
        DIFFERENT session_id from the (rewritten) registry entry, so the token
        is not this session's.
        """
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._record_verdict(88).returncode, 0)
        self._write_registry_entry("sess-a-different-session", self.seat_pid)
        checked = self._check(88)
        self.assertEqual(checked.returncode, 1)
        self.assertIn("wrong-session", checked.stderr)

    def test_unregistered_seat_refuses(self) -> None:
        """No registry entry -> no identity to bind -> refuse (fail-closed)."""
        (self.net_dir / "registry" / "advisor.json").unlink()
        armed = self._arm()
        self.assertEqual(armed.returncode, 1)
        self.assertIn("no switchboard entry", armed.stderr)

    def test_no_verdict_refuses(self) -> None:
        self.assertEqual(self._arm().returncode, 0)
        checked = self._check(88)  # armed, but no verdict recorded
        self.assertEqual(checked.returncode, 1)
        self.assertIn("no-verdict", checked.stderr)

    def test_changes_requested_refuses(self) -> None:
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._record_verdict(88, "changes-requested").returncode, 0)
        checked = self._check(88)
        self.assertEqual(checked.returncode, 1)
        self.assertIn("not-approved", checked.stderr)

    def test_sha_drift_refuses(self) -> None:
        """Verdict recorded at SHA_A; head moves to SHA_B -> sha-drift."""
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._record_verdict(88).returncode, 0)  # at SHA_A
        drift_env = self._base_env()
        drift_env["GH_FAKE_SHA"] = SHA_B
        checked = self._check(88, env=drift_env)
        self.assertEqual(checked.returncode, 1)
        self.assertIn("sha-drift", checked.stderr)

    def test_unclassified_repo_passes_without_echo(self) -> None:
        """An unlisted slug passes on pre-auth alone (2026-07-24 ruling).

        The typo guard is retired (every pair launched works the same), so a
        brand-new repo merges under the
        same armed-session posture as every classified one. The echo refusal
        path keeps unit coverage via the ECHO_REQUIRED_REPOS patch in
        test_session_auth.py.
        """
        self._git("remote", "set-url", "origin",
                  "git@github.com:example-org/an-unclassified-repo.git")
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._record_verdict(88).returncode, 0)
        checked = self._check(88)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertNotIn("echo-required", checked.stderr)


class TestMergeWrapperIntegration(IntegrationBase):
    """#88: the merge wrapper across process boundaries. --dry-run is the safe,
    end-to-end leg (real check, no real merge/repin), which is exactly the
    advisor's verify target."""

    def test_merge_dry_run_passes_and_prints_plan(self) -> None:
        self.assertEqual(self._arm().returncode, 0)
        self.assertEqual(self._record_verdict(88).returncode, 0)
        merged = self._merge(88, "--dry-run")
        self.assertEqual(merged.returncode, 0,
                         f"dry-run merge should pass auth:\n{merged.stderr}")
        self.assertIn("[auth] OK", merged.stdout)
        self.assertIn("DRY-RUN", merged.stdout)
        self.assertIn("repin.check sync", merged.stdout)  # names the deploy step

    def test_merge_dry_run_refuses_without_verdict(self) -> None:
        # No verdict recorded → check refuses → the wrapper aborts, no plan.
        self.assertEqual(self._arm().returncode, 0)
        merged = self._merge(88, "--dry-run")
        self.assertEqual(merged.returncode, 1)
        self.assertIn("no-verdict", merged.stderr)
        self.assertNotIn("DRY-RUN", merged.stdout)


if __name__ == "__main__":
    unittest.main()
