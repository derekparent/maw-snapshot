"""Tests for net.wire — delivery audit reuses the bridge turn format."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from bridge.format import parse_turns  # noqa: E402
from net import wire  # noqa: E402


class WireTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_wire_file_layout_is_directed(self) -> None:
        f = wire.wire_file("advisor", "executor")
        self.assertTrue(
            str(f).endswith("wire/advisor/from-executor.md"),
            f"unexpected layout: {f}",
        )

    def test_record_delivery_roundtrips_via_bridge_parser(self) -> None:
        turn = wire.record_delivery(
            sender_name="executor",
            sender_session_id="exec-123",
            to_name="advisor",
            body="hello peer",
            pushed=True,
            detail="pasted + Enter sent",
        )
        self.assertEqual(turn.turn_id, 1)
        turns = list(parse_turns(wire.wire_file("advisor", "executor")))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].role, "executor")
        self.assertEqual(turns[0].session_id, "exec-123")
        self.assertIn("push: pasted+enter", turns[0].body)
        self.assertIn("hello peer", turns[0].body)

    def test_sent_collects_outbound_across_recipients(self) -> None:
        # sender-side receipt: everything executor sent, to any peer (item 5)
        wire.record_delivery(
            sender_name="executor", sender_session_id="e-1", to_name="advisor",
            body="to advisor", pushed=True, detail="pasted + Enter sent",
        )
        wire.record_delivery(
            sender_name="executor", sender_session_id="e-1", to_name="planner",
            body="to planner", pushed=False, detail="parked",
        )
        # a turn from a DIFFERENT sender must not show up
        wire.record_delivery(
            sender_name="advisor", sender_session_id="a-1", to_name="executor",
            body="from advisor", pushed=True, detail="pasted + Enter sent",
        )
        rows = wire.sent("executor")
        tos = sorted(to for to, _t in rows)
        self.assertEqual(tos, ["advisor", "planner"])
        # the parked one is unconsumed; the pushed one is born-consumed
        parked = [(to, t) for to, t in rows if t.consumed_at is None]
        self.assertEqual([to for to, _t in parked], ["planner"])

    def test_sent_empty_when_none(self) -> None:
        self.assertEqual(wire.sent("nobody"), [])

    def test_failed_delivery_recorded_with_same_fidelity(self) -> None:
        wire.record_delivery(
            sender_name="executor",
            sender_session_id="exec-123",
            to_name="advisor",
            body="lost msg",
            pushed=False,
            detail="pane gone",
        )
        turns = list(parse_turns(wire.wire_file("advisor", "executor")))
        self.assertIn("push: none (pane gone)", turns[0].body)
        self.assertIn("lost msg", turns[0].body)

    def test_unconsumed_and_consume_roundtrip(self) -> None:
        for i in range(2):
            wire.record_delivery(
                sender_name="advisor",
                sender_session_id="adv-9",
                to_name="executor",
                body=f"msg {i}",
                pushed=False,
                detail="executor is push-deaf",
            )
        pending = wire.unconsumed("executor")
        self.assertEqual([t.turn_id for _, t in pending], [1, 2])

    def test_pushed_turn_is_born_consumed(self) -> None:
        # Dedup (f): the wake line already carried a pushed turn into the
        # peer's conversation; recv must not replay it (turns 6-10 replayed
        # live, 2026-06-11). consumed_at = wire bookkeeping, not a delivery
        # claim — the push: header keeps the honest record.
        wire.record_delivery(
            sender_name="advisor",
            sender_session_id="adv-9",
            to_name="executor",
            body="already in your conversation",
            pushed=True,
            detail="pasted + Enter sent",
        )
        self.assertEqual(wire.unconsumed("executor"), [])
        turns = list(parse_turns(wire.wire_file("executor", "advisor")))
        self.assertEqual(len(turns), 1, "audit copy must still exist")
        self.assertIsNotNone(turns[0].consumed_at)
        self.assertIn("push: pasted+enter", turns[0].body)

    def test_parked_turn_stays_recoverable(self) -> None:
        # The recovery path is sacred: a refused/parked push (pushed=False)
        # must remain unconsumed for recv.
        wire.record_delivery(
            sender_name="advisor",
            sender_session_id="adv-9",
            to_name="executor",
            body="parked behind a draft",
            pushed=False,
            detail="input box not empty",
        )
        pending = wire.unconsumed("executor")
        self.assertEqual(len(pending), 1)
        self.assertIn("push: none", pending[0][1].body)

    def test_consume_roundtrip(self) -> None:
        for i in range(2):
            wire.record_delivery(
                sender_name="advisor",
                sender_session_id="adv-9",
                to_name="executor",
                body=f"msg {i}",
                pushed=False,
                detail="parked",
            )
        pending = wire.unconsumed("executor")
        file, first = pending[0]
        self.assertTrue(wire.consume(file, first.turn_id))
        remaining = wire.unconsumed("executor")
        self.assertEqual([t.turn_id for _, t in remaining], [2])

    def test_unconsumed_empty_for_unknown_recipient(self) -> None:
        self.assertEqual(wire.unconsumed("nobody"), [])

    def test_unconsumed_merges_multiple_senders_oldest_first(self) -> None:
        # pushed=False: pushed turns are born consumed (dedup), so only
        # parked turns appear on the pull side at all.
        wire.record_delivery(
            sender_name="alpha", sender_session_id="a", to_name="executor",
            body="from alpha", pushed=False, detail="parked",
        )
        wire.record_delivery(
            sender_name="beta", sender_session_id="b", to_name="executor",
            body="from beta", pushed=False, detail="parked",
        )
        senders = [t.role for _, t in wire.unconsumed("executor")]
        self.assertEqual(sorted(senders), ["alpha", "beta"])

    def test_bridge_backed_record_links_turn_and_is_audit_only(self) -> None:
        # Item 5: when a body is durable on the bridge, the wire record is pure
        # AUDIT — it links the bridge turn id and is born consumed so the legacy
        # recv never re-delivers it (the bridge is the recovery path now).
        turn = wire.record_delivery(
            sender_name="executor", sender_session_id="e-1", to_name="advisor",
            body="durable body", pushed=False, detail="input box not empty",
            bridge_turn_id=42,
        )
        # Even though pushed=False, a bridge-backed record is born consumed:
        # the recovery path is the bridge, not this audit copy.
        self.assertIsNotNone(turn.consumed_at)
        self.assertEqual(wire.unconsumed("advisor"), [])
        turns = list(parse_turns(wire.wire_file("advisor", "executor")))
        self.assertIn("bridge_turn: 42", turns[0].body)

    def test_legacy_record_without_bridge_turn_unchanged(self) -> None:
        # No bridge_turn_id -> legacy semantics: parked stays unconsumed.
        wire.record_delivery(
            sender_name="executor", sender_session_id="e-1", to_name="advisor",
            body="parked legacy", pushed=False, detail="parked",
        )
        self.assertEqual(len(wire.unconsumed("advisor")), 1)
        turns = list(parse_turns(wire.wire_file("advisor", "executor")))
        self.assertNotIn("bridge_turn:", turns[0].body)

    def test_record_refusal_appends_jsonl(self) -> None:
        import json as json_mod

        wire.record_refusal(
            kind="lineage",
            claimed_sender="executor",
            caller_pid=999,
            to_name="advisor",
            reason="caller lineage pid 999 != registered pid 42",
        )
        wire.record_refusal(
            kind="precheck",
            claimed_sender="advisor",
            caller_pid=43,
            to_name="executor",
            reason="forbidden pattern [prompt_jailbreak]",
        )
        lines = wire.refusals_file().read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        first = json_mod.loads(lines[0])
        self.assertEqual(first["kind"], "lineage")
        self.assertEqual(first["claimed_sender"], "executor")
        self.assertEqual(first["caller_pid"], 999)
        self.assertIn("ts", first)

    def test_turn_ids_increment_per_directed_pair(self) -> None:
        for i in range(3):
            turn = wire.record_delivery(
                sender_name="executor",
                sender_session_id="exec-123",
                to_name="advisor",
                body=f"msg {i}",
                pushed=True,
                detail="ok",
            )
        self.assertEqual(turn.turn_id, 3)
        # reverse direction has its own file and numbering
        rev = wire.record_delivery(
            sender_name="advisor",
            sender_session_id="adv-9",
            to_name="executor",
            body="reply",
            pushed=True,
            detail="ok",
        )
        self.assertEqual(rev.turn_id, 1)


if __name__ == "__main__":
    unittest.main()
