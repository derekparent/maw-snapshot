"""The canonical runner drops the seat it runs in before any test loads.

Launched from an advisor or executor pane the suite inherits the seat's role,
name, model, the LIVE switchboard directory and the multiplexer's socket and
pane ids. The falsifier for this file's subject: the suite gives the same
count and OK with the seat variables set, unset, and with no claude ancestor.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maw"))

import run_all  # noqa: E402

SEAT = {
    "MAW_BRIDGE_ROLE": "executor", "MAW_NET_NAME": "executor2", "MAW_NET_DIR": "/live/switchboard",
    "MAW_SEAT_MODEL": "opus", "MAW_HUMAN": "1", "MAW_SESSION_ID": "s", "MAW_ADVISOR_MODEL": "fable",
    "MAW_SOMETHING_ADDED_NEXT_YEAR": "x",
    "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p2", "HERDR_SOCKET_PATH": "/live/herdr.sock",
    "TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%5",
    "CLAUDE_PLUGIN_ROOT": "/live/plugin", "CLAUDE_CONFIG_DIR": "/live/claude",
    "CLAUDE_SESSION_ID": "abc",
}
KEPT = {"PATH": "/usr/bin", "HOME": "/home/x", "CLAUDECODE": "1", "MAWKISH": "not ours",
        "LANG": "C"}


class TestIsolateSeatEnv(unittest.TestCase):
    def _isolated(self) -> "tuple[dict[str, str], list[str]]":
        env = {**SEAT, **KEPT}
        dropped = run_all.isolate_seat_env(env)
        return env, dropped

    def test_every_seat_variable_is_dropped(self) -> None:
        env, dropped = self._isolated()
        self.assertEqual(dropped, sorted(SEAT))
        for name in SEAT:
            if name != "MAW_NET_DIR":
                self.assertNotIn(name, env)

    def test_unrelated_variables_are_kept(self) -> None:
        env, _ = self._isolated()
        for name, value in KEPT.items():
            self.assertEqual(env[name], value)

    def test_net_dir_is_a_fresh_scratch_directory_not_the_live_one(self) -> None:
        env, _ = self._isolated()
        scratch = Path(env["MAW_NET_DIR"])
        self.assertNotEqual(str(scratch), SEAT["MAW_NET_DIR"])
        self.assertTrue(scratch.is_dir())
        self.assertEqual(list(scratch.iterdir()), [])
        self.assertTrue(scratch.name.startswith("maw-suite-net-"))

    def test_a_clean_environment_still_gets_the_scratch_net_dir(self) -> None:
        env = dict(KEPT)
        self.assertEqual(run_all.isolate_seat_env(env), [])
        self.assertIn("MAW_NET_DIR", env)

    def test_main_isolates_the_seat_first_and_before_any_test_loads(self) -> None:
        order: list[str] = []

        class Stop(Exception):
            pass

        def load(_names):
            order.append("load")
            raise Stop

        with mock.patch.object(run_all, "isolate_seat_env",
                               side_effect=lambda: order.append("seat")), \
                mock.patch.object(run_all, "isolate_operator_config",
                                  side_effect=lambda: order.append("operator")), \
                mock.patch.object(run_all, "install_offline_guard",
                                  side_effect=lambda: order.append("offline")), \
                mock.patch.object(run_all.unittest.defaultTestLoader, "loadTestsFromNames",
                                  side_effect=load), \
                mock.patch.object(sys, "path", list(sys.path)):
            with self.assertRaises(Stop):
                run_all.main([])
        self.assertEqual(order, ["seat", "operator", "offline", "load"])


class TestTheRunningSuiteIsIsolated(unittest.TestCase):
    """What a test actually sees when the canonical runner started it."""

    def setUp(self) -> None:
        net = os.environ.get("MAW_NET_DIR", "")
        if not Path(net).name.startswith("maw-suite-net-"):
            self.skipTest("not started by tests/run_all.py")

    def test_no_seat_identity_reaches_a_test(self) -> None:
        for name in ("MAW_BRIDGE_ROLE", "MAW_NET_NAME", "MAW_SEAT_MODEL", "MAW_HUMAN",
                     "MAW_SESSION_ID", "TMUX", "TMUX_PANE"):
            self.assertNotIn(name, os.environ)
        self.assertEqual([k for k in os.environ if k.startswith("HERDR_")], [])

    def test_the_default_switchboard_is_the_scratch_directory(self) -> None:
        from net import registry
        scratch = Path(os.environ["MAW_NET_DIR"]).resolve()
        self.assertTrue(registry.registry_dir().resolve().is_relative_to(scratch))


if __name__ == "__main__":
    unittest.main()
