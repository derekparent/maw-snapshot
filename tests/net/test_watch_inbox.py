"""maw-watch-inbox: the one documented inbound watcher (#347).

Runs the real script against a stub `bin/maw-run` that replays a scripted
sequence of `recv --check` results (stdout + exit code), so the edge rules are
pinned: announce on change, no latch on a back-to-back turn, re-announce while
waiting, quiet when idle, loud when degraded, one-shot exits on first event.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "maw" / "bin" / "maw-watch-inbox"

STUB = """#!/bin/sh
# usage: maw-run net.cli recv <name> --check ; replays $0.seq line by line
seq="$(dirname "$0")/seq"; idx="$(dirname "$0")/idx"
i=$(cat "$idx" 2>/dev/null || echo 0)
n=$(wc -l < "$seq" | tr -d ' ')
[ "$i" -ge "$n" ] && i=$((n - 1))
line=$(sed -n "$((i + 1))p" "$seq")
echo $((i + 1)) > "$idx"
rc="${line%%|*}"; out="${line#*|}"
[ -n "$out" ] && echo "$out"
exit "$rc"
"""


class WatchInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "bin").mkdir()
        shutil.copy(SCRIPT, self.root / "bin" / "maw-watch-inbox")
        stub = self.root / "bin" / "maw-run"
        stub.write_text(STUB)
        stub.chmod(0o755)
        (self.root / "bin" / "maw-watch-inbox").chmod(0o755)

    def _run(self, seq: list[str], *args: str, wait: float = 3.0) -> tuple[list[str], int | None]:
        # On a loaded machine one poll (a stub process plus a sleep) can take
        # far longer than the 50 ms interval, and a fixed wait then cuts the
        # replay short. Time one stub call and allow twice that per poll
        # (at most 20 s in all).
        (self.root / "bin" / "seq").write_text("3|\n")
        start = time.time()
        subprocess.run([str(self.root / "bin" / "maw-run")], capture_output=True)
        (self.root / "bin" / "idx").unlink(missing_ok=True)
        wait = max(wait, min(20.0, (len(seq) + 3) * (0.05 + 2 * (time.time() - start))))
        (self.root / "bin" / "seq").write_text("\n".join(seq) + "\n")
        p = subprocess.Popen(
            [str(self.root / "bin" / "maw-watch-inbox"), "seat", "--interval", "0.05", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + wait
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        rc = p.poll()
        if rc is None:
            p.terminate()
        out, _ = p.communicate(timeout=10)
        return [l for l in out.splitlines() if l], rc

    def test_announces_once_per_change_and_not_while_idle(self) -> None:
        lines, _ = self._run([
            "3|IDLE nothing waiting for seat",
            "0|PENDING 1 turn(s) waiting for seat (wire:1)",
            "0|PENDING 1 turn(s) waiting for seat (wire:1)",   # same set: silent
            "0|PENDING 2 turn(s) waiting for seat (wire:2)",   # back-to-back: announces
            "3|IDLE nothing waiting for seat",
        ], "--reannounce", "1000", wait=1.5)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("PENDING 1", lines[0])
        self.assertIn("PENDING 2", lines[1])

    def test_own_consume_does_not_refire(self) -> None:
        # Two turns wait, the seat drains one: the count text changes (2 -> 1)
        # but no new turn arrived.
        lines, _ = self._run([
            "0|PENDING 2 turn(s) waiting for seat (bridge:2) ids=bridge-4,bridge-5",
            "0|PENDING 1 turn(s) waiting for seat (bridge:1) ids=bridge-5",
            "0|PENDING 1 turn(s) waiting for seat (bridge:1) ids=bridge-5",
        ], "--reannounce", "1000", wait=1.5)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("PENDING 2", lines[0])
        self.assertNotIn("ids=", lines[0])

    def test_turn_landing_in_the_poll_another_was_consumed_in_announces(self) -> None:
        # One consumed, one arrived between two polls: the count text is the
        # same, the newest turn id is not.
        lines, _ = self._run([
            "0|PENDING 1 turn(s) waiting for seat (bridge:1) ids=bridge-4",
            "0|PENDING 1 turn(s) waiting for seat (bridge:1) ids=bridge-5",
            "0|PENDING 1 turn(s) waiting for seat (bridge:1) ids=bridge-5",
        ], "--reannounce", "1000", wait=1.5)
        self.assertEqual(len(lines), 2, lines)

    def test_bad_or_missing_interval_is_a_usage_error_not_a_hot_loop(self) -> None:
        script = str(self.root / "bin" / "maw-watch-inbox")
        (self.root / "bin" / "seq").write_text("3|IDLE nothing waiting for seat\n")
        for args in (["seat", "--interval"], ["seat", "--interval", "abc"],
                     ["seat", "--interval", "0"], ["seat", "--interval", ""],
                     ["seat", "--interval", "1.2.3"], ["--interval", "--once", "seat"],
                     ["seat", "--reannounce", "0"], ["seat", "--reannounce"]):
            r = subprocess.run([script, *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("usage:", r.stderr, args)
        self.assertFalse((self.root / "bin" / "idx").exists(), "it polled before refusing")

    def test_rearms_after_drain(self) -> None:
        lines, _ = self._run([
            "0|PENDING 1 turn(s) waiting for seat (wire:1)",
            "3|IDLE nothing waiting for seat",
            "0|PENDING 1 turn(s) waiting for seat (wire:1)",
            "3|IDLE nothing waiting for seat",
        ], "--reannounce", "1000", wait=1.5)
        self.assertEqual(len(lines), 2, lines)

    def test_reannounces_while_still_waiting(self) -> None:
        lines, _ = self._run(["0|PENDING 1 turn(s) waiting for seat (wire:1)"],
                             "--reannounce", "3", wait=1.5)
        self.assertGreaterEqual(len(lines), 2, lines)

    def test_degraded_is_loud_once(self) -> None:
        lines, _ = self._run(["1|", "1|", "1|"], "--reannounce", "1000", wait=1.0)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("degraded", lines[0])

    def test_one_failed_poll_is_not_announced(self) -> None:
        # run 1006 lane 2: a single failed lineage walk flipped the watch to
        # "degraded" and back on every hiccup.
        lines, _ = self._run(["3|IDLE nothing waiting for seat", "1|",
                              "3|IDLE nothing waiting for seat", "1|",
                              "3|IDLE nothing waiting for seat", "1|",
                              "3|IDLE nothing waiting for seat"],
                             "--reannounce", "1000", wait=1.0)
        self.assertEqual(lines, [])

    def test_two_failed_polls_in_a_row_are_announced_once_per_run_of_failures(self) -> None:
        lines, _ = self._run(["1|", "1|", "1|", "3|IDLE nothing waiting for seat",
                              "1|", "3|IDLE nothing waiting for seat",
                              "1|", "1|", "3|IDLE nothing waiting for seat"],
                             "--reannounce", "1000", wait=1.5)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all("degraded" in ln and "twice in a row" in ln for ln in lines), lines)

    def test_a_failed_poll_between_two_pending_polls_does_not_refire(self) -> None:
        lines, _ = self._run([
            "0|PENDING 1 turn(s) waiting for seat (bridge:1) ids=bridge-5", "1|",
            "0|PENDING 1 turn(s) waiting for seat (bridge:1) ids=bridge-5",
        ], "--reannounce", "1000", wait=1.0)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("PENDING 1", lines[0])

    def test_an_id_that_looks_like_a_glob_is_compared_as_text(self) -> None:
        # An older net.cli prints no ids; the whole line is then one id. A
        # star in it must not expand to the file names in the watcher's cwd.
        (self.root / "bin" / "zz-file").write_text("x")
        lines, _ = self._run(["0|PENDING * waiting", "0|PENDING * waiting",
                              "0|PENDING * waiting"], "--reannounce", "1000", wait=1.0)
        self.assertEqual(len(lines), 1, lines)

    def test_once_exits_on_first_event(self) -> None:
        lines, rc = self._run(["3|IDLE nothing waiting for seat",
                               "0|PENDING 1 turn(s) waiting for seat (bridge:1)"], "--once")
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("bridge:1", lines[0])

    def test_missing_name_is_usage_error(self) -> None:
        r = subprocess.run([str(self.root / "bin" / "maw-watch-inbox")],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
