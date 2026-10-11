"""The drill's cross-process (live) mode — #191.

`seed_stuck_pair` stamped `pid=os.getpid()`, which is right for a unit test and
wrong for a live drill: seeding from `python3 -c ...` leaves a DEAD pid behind, so
`who(prune=True)` reaps the seeded seat and the finding never fires. The 2026-07-22
drill worked around it by running seed + sweep + escalate in ONE process — which is
not the CLI path the seat commands document.

These tests pin the explicit-pid seam and the seed-only CLI mode, including the two
refusals that keep a live drill off real state.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from net.drills import stuck_seat_drill as drill
from net.registry import registry_dir


class DrillLiveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._net = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._net.cleanup)
        self.addCleanup(self._home.cleanup)
        p = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._net.name,
                                         "HOME": self._home.name})
        p.start()
        self.addCleanup(p.stop)
        self.now_s = drill.FIXED_NOW_S

    def _entry(self, name: str) -> dict:
        return json.loads((registry_dir() / f"{name}.json").read_text(encoding="utf-8"))


class TestExplicitPid(DrillLiveTestCase):
    def test_pid_defaults_to_this_process(self) -> None:
        drill.seed_stuck_pair(self.now_s)
        self.assertEqual(self._entry("executor")["pid"], os.getpid())

    def test_explicit_pid_is_stamped_on_both_seats(self) -> None:
        """The seam #191 asks for: the seeded seat outlives the seeding process."""
        drill.seed_stuck_pair(self.now_s, pid=4242)
        self.assertEqual(self._entry("executor")["pid"], 4242)
        self.assertEqual(self._entry("advisor")["pid"], 4242)


class TestSeedOnlyCli(DrillLiveTestCase):
    def test_seed_only_writes_the_registry_and_does_not_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with mock.patch.object(drill, "baseline_findings") as swept:
                rc = drill.main(["--seed-only", "--net-dir", scratch,
                                 "--pid", str(os.getpid())])
            self.assertEqual(rc, 0)
            swept.assert_not_called()
            seeded = Path(scratch) / "registry" / "executor.json"
            self.assertTrue(seeded.is_file())
            self.assertEqual(json.loads(seeded.read_text())["pid"], os.getpid())

    def test_seed_only_refuses_the_default_switchboard(self) -> None:
        """A drill that can seed fake stalls into a live registry is a hazard, not
        a harness — real seats sweep it every 45s and the dead-man pages a human."""
        real = Path.home() / ".claude" / "bridge" / "switchboard"
        rc = drill.main(["--seed-only", "--net-dir", str(real), "--pid", str(os.getpid())])
        self.assertEqual(rc, 2)
        self.assertFalse((real / "registry" / "executor.json").exists())

    def test_seed_only_refuses_the_AMBIENT_net_dir(self) -> None:
        """The live switchboard is per-repo (MAW_NET_DIR=~/.claude/bridge/sw-<repo>),
        so guarding only the default path guards the one dir nobody uses — the
        #211 review blocker."""
        ambient = Path(self._net.name) / "sw-maw"
        (ambient / "registry").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"MAW_NET_DIR": str(ambient)}):
            rc = drill.main(["--seed-only", "--net-dir", str(ambient),
                             "--pid", str(os.getpid())])
        self.assertEqual(rc, 2)
        self.assertFalse((ambient / "registry" / "executor.json").exists())

    def test_seed_only_refuses_any_bridge_dir_that_holds_a_registry(self) -> None:
        """Every per-repo switchboard under ~/.claude/bridge is live for some pair,
        not just this session's. Discovered structurally, never by name pattern."""
        other = Path(self._home.name) / ".claude" / "bridge" / "sw-otherrepo"
        (other / "registry").mkdir(parents=True)
        rc = drill.main(["--seed-only", "--net-dir", str(other), "--pid", str(os.getpid())])
        self.assertEqual(rc, 2)

    def test_seed_only_refuses_a_dir_holding_a_LIVE_non_drill_entry(self) -> None:
        """The structural rule, independent of knowing where the real ones are: a
        scratch dir is empty; a live switchboard is not."""
        with tempfile.TemporaryDirectory() as scratch:
            reg = Path(scratch) / "registry"
            reg.mkdir()
            (reg / "somebody.json").write_text(json.dumps({
                "name": "somebody", "role": "advisor", "repo_root": "/r", "cwd": "/r",
                "pid": os.getpid(), "tty": None, "tmux_pane": None,
                "session_id": "somebody-20260830T000000Z",
                "started_at": "2026-08-30T00:00:00Z", "last_seen": "2026-08-30T00:00:00Z",
            }), encoding="utf-8")
            rc = drill.main(["--seed-only", "--net-dir", scratch, "--pid", str(os.getpid())])
            self.assertEqual(rc, 2)
            self.assertFalse((reg / "executor.json").exists())

    def test_reseeding_a_scratch_dir_the_drill_owns_is_allowed(self) -> None:
        """Its own seeded entries are pid-alive too; refusing them would make the
        harness single-use."""
        with tempfile.TemporaryDirectory() as scratch:
            self.assertEqual(drill.main(["--seed-only", "--net-dir", scratch,
                                         "--pid", str(os.getpid())]), 0)
            self.assertEqual(drill.main(["--seed-only", "--net-dir", scratch,
                                         "--pid", str(os.getpid())]), 0)

    def test_a_dead_entry_does_not_make_a_scratch_dir_look_live(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            reg = Path(scratch) / "registry"
            reg.mkdir()
            dead = subprocess.run(["python3", "-c", "import os; print(os.getpid())"],
                                  capture_output=True, text=True).stdout.strip()
            (reg / "ghost.json").write_text(json.dumps({
                "name": "ghost", "role": "advisor", "repo_root": "/r", "cwd": "/r",
                "pid": int(dead), "tty": None, "tmux_pane": None, "session_id": "ghost-x",
                "started_at": "2026-08-30T00:00:00Z", "last_seen": "2026-08-30T00:00:00Z",
            }), encoding="utf-8")
            self.assertEqual(drill.main(["--seed-only", "--net-dir", scratch,
                                         "--pid", str(os.getpid())]), 0)

    def test_hold_seconds_below_the_floor_is_refused(self) -> None:
        """A holder that dies mid-drill leaves the seeded seat reaped and the sweep
        silent — the same false-green, just later."""
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SystemExit):
                drill.main(["--seed-only", "--net-dir", scratch, "--hold-seconds", "30"])

    def test_status_is_refused_in_seed_only_mode(self) -> None:
        """Cross-process the herdr status is read live from a pane that does not
        exist, so --status silently means something else. Refuse before asking."""
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SystemExit):
                drill.main(["--seed-only", "--net-dir", scratch, "--status", "blocked",
                            "--pid", str(os.getpid())])

    def test_seed_only_refuses_a_dead_pid(self) -> None:
        """The exact #191 footgun: a dead pid seeds a seat who(prune=True) reaps,
        so the drill reports 'no finding' and looks like a passing system."""
        with tempfile.TemporaryDirectory() as scratch:
            dead = subprocess.run(["python3", "-c", "import os; print(os.getpid())"],
                                  capture_output=True, text=True).stdout.strip()
            rc = drill.main(["--seed-only", "--net-dir", scratch, "--pid", dead])
        self.assertEqual(rc, 2)

    def test_net_dir_is_required_for_seed_only(self) -> None:
        with self.assertRaises(SystemExit):
            drill.main(["--seed-only", "--pid", str(os.getpid())])

    def test_hold_seconds_spawns_a_live_holder_and_stamps_its_pid(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            rc = drill.main(["--seed-only", "--net-dir", scratch, "--hold-seconds", "120"])
            self.assertEqual(rc, 0)
            pid = json.loads((Path(scratch) / "registry" / "executor.json").read_text())["pid"]
            self.assertNotEqual(pid, os.getpid())
            os.kill(pid, 0)  # alive: raises OSError if not
            os.kill(pid, 15)

    def test_pid_and_hold_seconds_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SystemExit):
                drill.main(["--seed-only", "--net-dir", scratch,
                            "--pid", str(os.getpid()), "--hold-seconds", "120"])


if __name__ == "__main__":
    unittest.main()
