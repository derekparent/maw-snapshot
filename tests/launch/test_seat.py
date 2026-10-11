"""Tests for launch.seat — the in-repo pair launcher (L1 item 1)."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from launch import seat  # noqa: E402

WT = Path("/tmp/wt/maw-advisor")


class RoleTest(unittest.TestCase):
    def test_roles_and_generations(self) -> None:
        self.assertEqual(seat.role_of("advisor"), "advisor")
        self.assertEqual(seat.role_of("executor"), "executor")
        self.assertEqual(seat.role_of("advisor2"), "advisor")
        for bad in ("advisorx", "consultant", "", "pair"):
            with self.subTest(bad), self.assertRaises(ValueError):
                seat.role_of(bad)


class PlanTest(unittest.TestCase):
    def test_default_seat_env_and_first_prompt(self) -> None:
        p = seat.plan("advisor", WT, "maw")
        self.assertEqual(p.cwd, WT)
        self.assertEqual(p.argv, ["claude", "--model", "opus", "/maw:advisor"])
        self.assertEqual(p.env["MAW_BRIDGE_ROLE"], "advisor")
        self.assertEqual(p.env["MAW_SEAT_MODEL"], "opus")
        self.assertTrue(p.env["MAW_NET_DIR"].endswith("/.claude/bridge/sw-maw"))
        self.assertNotIn("MAW_NET_NAME", p.env)

    def test_the_human_marker_is_opt_in(self) -> None:
        self.assertNotIn("MAW_HUMAN", seat.plan("executor", WT, "maw").env)
        self.assertEqual(seat.plan("executor", WT, "maw", human=True).env["MAW_HUMAN"], "1")

    def test_a_generational_name_sets_the_net_name(self) -> None:
        p = seat.plan("executor2", WT, "maw")
        self.assertEqual(p.env["MAW_BRIDGE_ROLE"], "executor")
        self.assertEqual(p.env["MAW_NET_NAME"], "executor2")

    def test_claude_args_replace_the_first_prompt(self) -> None:
        p = seat.plan("advisor", WT, "maw", claude_args=["--permission-mode", "plan"])
        self.assertEqual(p.argv, ["claude", "--model", "opus", "--permission-mode", "plan"])

    def test_no_model_leaves_the_choice_to_claude(self) -> None:
        p = seat.plan("advisor", WT, "maw", model=None)
        self.assertEqual(p.argv, ["claude", "/maw:advisor"])
        self.assertNotIn("MAW_SEAT_MODEL", p.env)

    def test_the_shell_line_quotes_and_names_no_operator_paths(self) -> None:
        line = seat.shell_line(seat.plan("advisor", Path("/tmp/a b/wt"), "maw"))
        self.assertIn("cd '/tmp/a b/wt' && ", line)
        self.assertNotIn("plugin-main", line)


class InheritedIdentityTest(unittest.TestCase):
    def test_an_inherited_net_name_or_human_marker_is_scrubbed(self) -> None:
        base = {"MAW_NET_NAME": "advisor2", "MAW_HUMAN": "1", "PATH": "/bin"}
        env = seat.child_env(seat.plan("executor", WT, "maw"), base)
        self.assertNotIn("MAW_NET_NAME", env)
        self.assertNotIn("MAW_HUMAN", env)
        self.assertEqual(env["PATH"], "/bin")
        env2 = seat.child_env(seat.plan("executor2", WT, "maw", human=True), base)
        self.assertEqual(env2["MAW_NET_NAME"], "executor2")
        self.assertEqual(env2["MAW_HUMAN"], "1")

    def test_the_tmux_shell_line_unsets_what_it_does_not_set(self) -> None:
        line = seat.shell_line(seat.plan("executor", WT, "maw"))
        self.assertIn("env -u MAW_NET_NAME -u MAW_HUMAN ", line)


class PluginRootTest(unittest.TestCase):
    def test_the_plugin_root_is_derived_from_this_install(self) -> None:
        self.assertEqual(seat.PLUGIN_ROOT, Path(seat.__file__).resolve().parents[1])
        self.assertTrue((seat.PLUGIN_ROOT / "net" / "cli.py").is_file())

    def test_pair_commands_use_this_installs_launcher(self) -> None:
        cmds = seat.pair_commands("/r/maw", "maw", [])
        self.assertEqual(cmds[0][:5], ["tmux", "new-session", "-d", "-s", "maw-pair"])
        self.assertIn(str(seat.PLUGIN_ROOT / "bin" / "maw-seat"), cmds[0][5])
        self.assertIn("advisor", cmds[0][5])
        self.assertIn("executor", cmds[1][-1])
        self.assertNotIn("--human", " ".join(" ".join(c) for c in cmds))


class PairAttachTest(unittest.TestCase):
    def test_an_existing_pair_session_is_attached_not_recreated(self) -> None:
        self.assertEqual(seat.pair_commands("/r/maw", "maw", [], exists=True),
                         [["tmux", "attach-session", "-t", "maw-pair"]])


class PairArgsTest(unittest.TestCase):
    def test_pair_forwards_claude_args_to_both_seats(self) -> None:
        cmds = seat.pair_commands("/r/maw", "maw", [], claude_args=["--permission-mode", "plan"])
        self.assertIn("-- --permission-mode plan", cmds[0][-1])
        self.assertIn("-- --permission-mode plan", cmds[1][-1])


class ShellLineTest(unittest.TestCase):
    def test_the_shell_line_is_one_quoted_env_invocation(self) -> None:
        line = seat.shell_line(seat.plan("advisor2", WT, "maw", human=True))
        self.assertIn(" && env MAW_BRIDGE_ROLE=advisor ", line)
        self.assertNotIn("  ", line)
        self.assertNotIn("-u MAW_NET_NAME", line)


class MainDryRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(os.path.realpath(self.tmp.name)) / "myrepo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"],
                       check=True)
        self.home = Path(self.tmp.name) / "wthome"

    def _run(self, *argv: str) -> "tuple[int, str]":
        out = io.StringIO()
        with mock.patch.object(seat.worktree, "worktree_home", return_value=self.home), \
                redirect_stdout(out):
            rc = seat.main(list(argv))
        return rc, out.getvalue()

    def test_dry_run_prints_the_launch_and_creates_nothing(self) -> None:
        rc, out = self._run("executor", "--repo", str(self.repo), "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn(str(self.home / "myrepo-executor"), out)
        self.assertIn("MAW_BRIDGE_ROLE=executor", out)
        self.assertIn("/maw:executor", out)
        self.assertFalse(self.home.exists())

    def test_dry_run_passes_claude_args_after_the_separator(self) -> None:
        rc, out = self._run("advisor", "--repo", str(self.repo), "--dry-run",
                            "--", "--permission-mode", "plan")
        self.assertEqual(rc, 0)
        self.assertIn("--permission-mode plan", out)
        self.assertNotIn("/maw:advisor", out)

    def test_the_dry_run_path_is_the_path_ensure_creates(self) -> None:
        with mock.patch.object(seat.worktree, "worktree_home", return_value=self.home):
            planned = seat.worktree.seat_worktree_path("executor", str(self.repo))
            made = seat.worktree.ensure("executor", cwd=str(self.repo)).path
        self.assertEqual(planned, made)

    def test_pair_refuses_tmux_flag(self) -> None:
        rc, _ = self._run("pair", "--repo", str(self.repo), "--tmux", "--dry-run")
        self.assertEqual(rc, 2)

    def test_an_unknown_seat_is_refused(self) -> None:
        rc, _ = self._run("reviewer", "--repo", str(self.repo), "--dry-run")
        self.assertEqual(rc, 2)

    def test_a_non_repo_is_refused(self) -> None:
        rc, _ = self._run("advisor", "--repo", self.tmp.name, "--dry-run")
        self.assertEqual(rc, 2)

    def test_a_missing_or_file_repo_is_refused_cleanly(self) -> None:
        # It used to die with a raw FileNotFoundError traceback (the CoS's
        # user-test of #258): git ran with cwd= a path that is not there.
        a_file = Path(self.tmp.name) / "f"
        a_file.write_text("x")
        for bad in (str(Path(self.tmp.name) / "nonexistent" / "x"), str(a_file)):
            for seat_name in ("advisor", "pair"):
                with self.subTest(bad=bad, seat=seat_name):
                    err = io.StringIO()
                    with redirect_stderr(err):
                        rc = seat.main([seat_name, "--repo", bad, "--dry-run"])
                    self.assertEqual(rc, 2)
                    self.assertIn("maw-seat: --repo", err.getvalue())
                    self.assertIn("no such directory", err.getvalue())


class LaunchClaimTest(unittest.TestCase):
    def test_advisor_claims_grounding_at_launch(self) -> None:
        calls: list[dict] = []
        ok = seat.launch_claim("advisor", "/r", claimer=lambda **k: calls.append(k) or True,
                               reader=lambda r: {})
        self.assertTrue(ok)
        self.assertEqual(calls, [{"goal_plain": seat.LAUNCH_GOAL, "repo_root": "/r"}])
        self.assertIn("grounding", seat.LAUNCH_GOAL)

    def test_relaunch_keeps_a_live_fragment(self) -> None:
        live = {"status": "working", "goal_plain": "real goal", "shipped": [{"what": "x"}]}
        calls: list[dict] = []
        ok = seat.launch_claim("advisor", "/r", claimer=lambda **k: calls.append(k) or True,
                               reader=lambda r: live)
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertEqual(live["goal_plain"], "real goal")
        self.assertEqual(live["shipped"], [{"what": "x"}])

    def test_wrapped_or_idle_slot_is_reclaimed(self) -> None:
        for st in ("wrapped", "idle"):
            calls: list[dict] = []
            ok = seat.launch_claim("advisor", "/r", claimer=lambda **k: calls.append(k) or True,
                                   reader=lambda r, st=st: {"status": st})
            self.assertTrue(ok)
            self.assertEqual(len(calls), 1)

    def test_executor_never_claims(self) -> None:
        calls: list[dict] = []
        self.assertFalse(seat.launch_claim("executor", "/r", claimer=lambda **k: calls.append(k)))
        self.assertEqual(calls, [])

    def test_a_board_failure_never_blocks(self) -> None:
        def boom(**_k):
            raise RuntimeError("no board")
        self.assertFalse(seat.launch_claim("advisor", "/r", claimer=boom, reader=lambda r: {}))


class BootBannerTest(unittest.TestCase):
    class P:
        def __init__(self, role, pane):
            self.role, self.herdr_pane = role, pane

    def test_outside_herdr_is_silent(self) -> None:
        self.assertEqual(seat.boot_banner("advisor", "maw", {}, peers=[]), [])

    def test_names_own_and_peer_pane(self) -> None:
        env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "p_1"}
        lines = seat.boot_banner("advisor", "maw", env, peers=[self.P("executor", "p_2")])
        self.assertIn("pane p_1", lines[0])
        self.assertIn("peer executor pane: p_2", lines[1])
        self.assertIn("pane-peek", lines[1])

    def test_unregistered_peer_is_not_guessed(self) -> None:
        env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "p_1"}
        lines = seat.boot_banner("executor", "maw", env, peers=[self.P("executor", "p_9")])
        self.assertIn("not registered yet", lines[1])


class WrapperTest(unittest.TestCase):
    def test_the_wrapper_works_through_a_symlink_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "maw-seat"
            link.symlink_to(seat.PLUGIN_ROOT / "bin" / "maw-seat")
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            cp = subprocess.run([str(link), "--help"], capture_output=True, text=True,
                                cwd="/", env=env, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_the_bin_wrapper_runs_from_any_cwd(self) -> None:
        wrapper = seat.PLUGIN_ROOT / "bin" / "maw-seat"
        self.assertTrue(os.access(wrapper, os.X_OK))
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        cp = subprocess.run([str(wrapper), "--help"], capture_output=True, text=True,
                            cwd="/", env=env, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("MAW_HUMAN", cp.stdout)


class DeletedCwdTest(unittest.TestCase):
    def _run_from_deleted_cwd(self, *args: str) -> subprocess.CompletedProcess:
        # The child process, not the test runner, owns the deleted cwd.
        driver = (
            "import os, subprocess, sys, tempfile\n"
            "d = tempfile.mkdtemp()\n"
            "os.chdir(d)\n"
            "os.rmdir(d)\n"
            "cp = subprocess.run([sys.executable, '-m', 'launch.seat', *sys.argv[1:]],\n"
            "                    capture_output=True, text=True, timeout=30)\n"
            "sys.stdout.write(cp.stdout)\n"
            "sys.stderr.write(cp.stderr)\n"
            "sys.exit(cp.returncode)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(seat.PLUGIN_ROOT))
        return subprocess.run([sys.executable, "-c", driver, *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_no_repo_exits_with_a_message_not_a_traceback(self) -> None:
        cp = self._run_from_deleted_cwd("advisor", "--dry-run")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("Traceback", cp.stderr)
        self.assertIn("no longer exists", cp.stderr)
        self.assertIn("--repo", cp.stderr)

    def test_explicit_repo_does_not_need_the_cwd(self) -> None:
        cp = self._run_from_deleted_cwd("advisor", "--dry-run", "--repo", "/nonexistent-maw-repo")
        self.assertNotIn("Traceback", cp.stderr)
        self.assertIn("no such directory", cp.stderr)


if __name__ == "__main__":
    unittest.main()
