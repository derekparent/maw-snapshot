"""The sweep's false-positive family — #188.

Three noise shapes, all observed live, none of them a page (model-flip is
excluded from ESCALATABLE_KINDS and stays that way). Each fire still costs a
wake plus a pane read, which is what trains a seat to ignore the watch output.

  1. A single transient fallback record stayed "the last" until a fresh turn
     overwrote it, so the flip fired on a seat whose pane said 4.8 (maw
     2026-07-22).
  2. The finding CLEARED and RE-FIRED ~6x, each re-appearance a fresh edge for
     the id-set dedup (gizmo 2026-07-23) — and a DELIBERATE mismatch, which
     can never clear, re-fired on nearly every wake all night (skills 07-25).
  3. A duplicate registry entry for one session (executor + executor-<slug>,
     same pane) doubled every finding in the sweep (skills 07-25).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from net import watch
from net.registry import Entry


def _entry(name: str, model: str = "claude-opus-4-8", pid: int = 4242,
           pane: str = "w1:p1", last_seen: str = "2026-08-30T00:00:00Z") -> Entry:
    return Entry(name=name, role="executor", repo_root="/repo", cwd="/repo",
                 pid=pid, tty=None, tmux_pane=None, session_id=f"{name}-s",
                 started_at="2026-08-30T00:00:00Z", last_seen=last_seen,
                 mux="herdr", herdr_pane=pane, model=model)


class NoiseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._net = tempfile.TemporaryDirectory()
        self.addCleanup(self._net.cleanup)
        p = mock.patch.dict(os.environ, {"MAW_NET_DIR": self._net.name})
        p.start()
        self.addCleanup(p.stop)
        self.now = 1_800_000_000.0


class TestModelFlipPersistence(NoiseTestCase):
    def _flips(self, models, entries=None, **kw):
        with mock.patch("net.watch.live_seats",
                        return_value=entries or [_entry("executor")]):
            return watch.model_flip_sessions(
                now_s=self.now, live_models_fn=lambda e: models, **kw)

    def test_one_stale_fallback_record_is_not_a_flip(self) -> None:
        """The maw 2026-07-22 false positive: pinned 4.8, one old 4.7 record,
        every newer turn back on 4.8."""
        found = self._flips(["claude-opus-4-8", "claude-opus-4-8",
                             "claude-opus-4-8", "claude-opus-4-7"])
        self.assertEqual(found, [])

    def test_a_sustained_flip_still_fires(self) -> None:
        found = self._flips(["claude-opus-4-7"] * 4)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "model-flip")
        self.assertIn("claude-opus-4-7", found[0].detail)

    def test_a_flip_still_settling_does_not_fire_yet(self) -> None:
        """Two fallback records with a pinned-model turn behind them is a
        transition, not a state — it fires on the next sweep if it persists."""
        found = self._flips(["claude-opus-4-7", "claude-opus-4-7",
                             "claude-opus-4-8", "claude-opus-4-8"])
        self.assertEqual(found, [])

    def test_too_few_records_is_fail_open(self) -> None:
        self.assertEqual(self._flips(["claude-opus-4-7"]), [])
        self.assertEqual(self._flips([]), [])

    def test_empty_records_do_not_pad_the_persistence_gate(self) -> None:
        """Review note on #213: the length gate counted empty models while the
        agreement check skipped them, so ONE real fallback record plus two blanks
        fired — the single-record FP this gate exists to remove."""
        self.assertEqual(self._flips(["claude-opus-4-7", "", ""]), [])

    def test_empties_are_skipped_over_not_counted(self) -> None:
        """With MODEL_FLIP_TAIL=5 there is slack: three real fallback records
        still fire even when blanks sit between them."""
        found = self._flips(["claude-opus-4-7", "", "claude-opus-4-7",
                             "claude-opus-4-7", ""])
        self.assertEqual(len(found), 1)

    def test_a_decorated_suffix_is_never_a_flip(self) -> None:
        self.assertEqual(self._flips(["claude-opus-4-8[1m]"] * 4), [])

    def test_unpinned_seat_is_skipped(self) -> None:
        found = self._flips(["claude-opus-4-7"] * 4,
                            entries=[_entry("executor", model="")])
        self.assertEqual(found, [])


class TestFlapCooldown(NoiseTestCase):
    def _flip_once(self, now):
        with mock.patch("net.watch.live_seats", return_value=[_entry("executor")]):
            return watch.model_flip_sessions(
                now_s=now, live_models_fn=lambda e: ["claude-opus-4-7"] * 4)

    def test_a_repeat_within_the_window_is_suppressed(self) -> None:
        """The all-night case: a deliberate mismatch can never clear, so without a
        cooldown it re-fires on every wake."""
        self.assertEqual(len(self._flip_once(self.now)), 1)
        self.assertEqual(self._flip_once(self.now + 60), [])
        self.assertEqual(self._flip_once(self.now + 3600), [])

    def test_it_fires_again_after_the_window(self) -> None:
        self.assertEqual(len(self._flip_once(self.now)), 1)
        later = self.now + (watch.DEFAULT_MODEL_FLIP_COOLDOWN_MIN + 1) * 60
        self.assertEqual(len(self._flip_once(later)), 1)

    def test_a_clear_and_re_fire_cycle_is_ONE_wake_not_six(self) -> None:
        """The gizmo shape: the condition cycles, so the id set goes
        empty->present repeatedly and every re-appearance is a fresh edge."""
        fires = 0
        for i in range(6):
            at = self.now + i * 300  # a 5-minute cycle, 30 minutes total
            if self._flip_once(at):
                fires += 1
            with mock.patch("net.watch.live_seats", return_value=[_entry("executor")]):
                watch.model_flip_sessions(  # the "cleared" half of the cycle
                    now_s=at + 60, live_models_fn=lambda e: ["claude-opus-4-8"] * 4)
        self.assertEqual(fires, 1)

    def test_the_cooldown_is_per_finding_id(self) -> None:
        with mock.patch("net.watch.live_seats",
                        return_value=[_entry("executor"), _entry("advisor", pane="w1:p2")]):
            first = watch.model_flip_sessions(
                now_s=self.now, live_models_fn=lambda e: ["claude-opus-4-7"] * 4)
        self.assertEqual({f.subject for f in first}, {"executor", "advisor"})

    def test_a_torn_cooldown_ledger_does_not_crash_the_sweep(self) -> None:
        (watch.net_dir() / watch.FLAP_LEDGER).write_text("{not json\n", encoding="utf-8")
        self.assertEqual(len(self._flip_once(self.now)), 1)


class TestLiveSeatsDedup(NoiseTestCase):
    def test_a_self_qualified_duplicate_collapses_to_the_freshest(self) -> None:
        """The skills 07-25 shape: the collision handler re-registered the seat
        under a qualified name and the old file stayed, so one session appeared
        twice — doubling every finding about it."""
        stale = _entry("executor", last_seen="2026-08-30T00:00:00Z")
        fresh = _entry("executor-skills-executor", last_seen="2026-08-30T00:10:00Z")
        with mock.patch("net.watch.who", return_value=[stale, fresh]):
            seats = watch.live_seats()
        self.assertEqual([s.name for s in seats], ["executor-skills-executor"])

    def test_two_real_seats_are_not_collapsed(self) -> None:
        a = _entry("advisor", pid=1, pane="w1:p1")
        e = _entry("executor", pid=2, pane="w1:p2")
        with mock.patch("net.watch.who", return_value=[a, e]):
            seats = watch.live_seats()
        self.assertEqual(sorted(s.name for s in seats), ["advisor", "executor"])

    def test_dedup_halves_the_findings_for_one_doubled_session(self) -> None:
        stale = _entry("executor", last_seen="2026-08-30T00:00:00Z")
        fresh = _entry("executor-skills-executor", last_seen="2026-08-30T00:10:00Z")
        with mock.patch("net.watch.who", return_value=[stale, fresh]):
            found = watch.model_flip_sessions(
                now_s=self.now, live_models_fn=lambda e: ["claude-opus-4-7"] * 4)
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
