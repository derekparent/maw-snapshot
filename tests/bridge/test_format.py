"""Tests for bridge.format — round-trip, integrity, malformed inputs, mark_consumed."""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from bridge.format import (  # noqa: E402
    BridgeEmptyBodyError,
    BridgeIntegrityError,
    BridgeMalformedError,
    BridgeSizeExceededError,
    MAX_BODY_BYTES,
    Turn,
    append_turn,
    find_unconsumed_latest,
    find_unconsumed_oldest,
    make_turn,
    mark_consumed,
    parse_turns,
    render_turn,
)


class FormatTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.file = self.dir / "from-executor.md"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_turn(self, **overrides) -> Turn:
        defaults = dict(
            turn_id=1,
            role="executor",
            session_id="sess-abc-123",
            body="hello world",
        )
        defaults.update(overrides)
        return make_turn(**defaults)


class RoundTripTests(FormatTestBase):
    def test_simple_round_trip(self):
        turn = self._make_turn(body="quick check on the spec")
        append_turn(self.file, turn)
        parsed = list(parse_turns(self.file))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].body, "quick check on the spec")
        self.assertEqual(parsed[0].turn_id, 1)
        self.assertEqual(parsed[0].role, "executor")
        self.assertIsNone(parsed[0].consumed_at)

    def test_multiple_turns(self):
        for i in range(1, 6):
            append_turn(self.file, self._make_turn(turn_id=i, body=f"turn {i}"))
        parsed = list(parse_turns(self.file))
        self.assertEqual([t.turn_id for t in parsed], [1, 2, 3, 4, 5])
        self.assertEqual([t.body for t in parsed], [f"turn {i}" for i in range(1, 6)])

    def test_unicode_body_round_trips(self):
        body = "shipping ✓ — résumé 漢字 🚀"
        append_turn(self.file, self._make_turn(body=body))
        parsed = list(parse_turns(self.file))
        self.assertEqual(parsed[0].body, body)

    def test_body_containing_protocol_strings_does_not_break_parser(self):
        # Body contains literal text that COULD be confused for protocol delimiters
        # if the parser were doing stupid string searches over the file.
        body = (
            "<!--BRIDGE-TURN-BEGIN turn_id=999-->\n"
            "consumed_at: null\n"
            "body_sha256: deadbeef\n"
            "<!--BRIDGE-TURN-END turn_id=999-->\n"
        )
        append_turn(self.file, self._make_turn(turn_id=42, body=body))
        parsed = list(parse_turns(self.file))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].turn_id, 42)
        self.assertEqual(parsed[0].body, body)

    def test_parse_returns_empty_for_missing_file(self):
        self.assertEqual(list(parse_turns(self.file)), [])

    def test_appended_file_has_0600_perms(self):
        append_turn(self.file, self._make_turn())
        mode = self.file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class WriteValidationTests(FormatTestBase):
    def test_empty_body_rejected_at_make_turn(self):
        with self.assertRaises(BridgeEmptyBodyError):
            make_turn(turn_id=1, role="executor", session_id="s", body="")

    def test_oversized_body_rejected_at_make_turn(self):
        oversized = "x" * (MAX_BODY_BYTES + 1)
        with self.assertRaises(BridgeSizeExceededError):
            make_turn(turn_id=1, role="executor", session_id="s", body=oversized)

    def test_render_rejects_mismatched_sha256(self):
        good = self._make_turn(body="real")
        tampered = Turn(
            turn_id=good.turn_id,
            ts=good.ts,
            role=good.role,
            session_id=good.session_id,
            revision=good.revision,
            consumed_at=good.consumed_at,
            body_sha256="0" * 64,
            body=good.body,
        )
        with self.assertRaises(BridgeIntegrityError):
            render_turn(tampered)


class MalformedFileTests(FormatTestBase):
    def test_begin_end_mismatch(self):
        self.file.write_text(
            "<!--BRIDGE-TURN-BEGIN turn_id=1-->\n"
            "---\n"
            "turn_id: 1\n"
            "ts: 2026-05-08T03:15:22Z\n"
            "role: executor\n"
            "session_id: s\n"
            "revision: 1\n"
            "consumed_at: null\n"
            "body_encoding: base64\n"
            "body_sha256: " + hashlib.sha256(b"x").hexdigest() + "\n"
            "---\n"
            + base64.b64encode(b"x").decode("ascii") + "\n"
            "<!--BRIDGE-TURN-END turn_id=2-->\n"
        )
        with self.assertRaises(BridgeMalformedError):
            list(parse_turns(self.file))

    def test_unterminated_block(self):
        self.file.write_text(
            "<!--BRIDGE-TURN-BEGIN turn_id=1-->\n"
            "---\n"
            "turn_id: 1\n"
        )
        with self.assertRaises(BridgeMalformedError):
            list(parse_turns(self.file))

    def test_unparseable_frontmatter(self):
        body = "x"
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.file.write_text(
            "<!--BRIDGE-TURN-BEGIN turn_id=1-->\n"
            "---\n"
            "turn_id: 1\n"
            "this is not a key:value line\n"
            "ts: 2026-05-08T03:15:22Z\n"
            "role: executor\n"
            "session_id: s\n"
            "revision: 1\n"
            "consumed_at: null\n"
            "body_encoding: base64\n"
            f"body_sha256: {sha}\n"
            "---\n"
            f"{b64}\n"
            "<!--BRIDGE-TURN-END turn_id=1-->\n"
        )
        with self.assertRaises(BridgeMalformedError):
            list(parse_turns(self.file))

    def test_missing_required_field(self):
        body = "x"
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        # Missing 'role'
        self.file.write_text(
            "<!--BRIDGE-TURN-BEGIN turn_id=1-->\n"
            "---\n"
            "turn_id: 1\n"
            "ts: 2026-05-08T03:15:22Z\n"
            "session_id: s\n"
            "revision: 1\n"
            "consumed_at: null\n"
            "body_encoding: base64\n"
            f"body_sha256: {sha}\n"
            "---\n"
            f"{b64}\n"
            "<!--BRIDGE-TURN-END turn_id=1-->\n"
        )
        with self.assertRaises(BridgeMalformedError):
            list(parse_turns(self.file))


class IntegrityTests(FormatTestBase):
    def test_tampered_body_detected(self):
        append_turn(self.file, self._make_turn(body="original content"))
        # Tamper: change body bytes (different length to ensure sha mismatch)
        text = self.file.read_text()
        tampered_body = "TAMPERED!"
        b64 = base64.b64encode(tampered_body.encode("utf-8")).decode("ascii")
        # Replace the base64 line (which is the line right before the END comment)
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.startswith("<!--BRIDGE-TURN-END"):
                lines[i - 1] = b64 + "\n"
                break
        self.file.write_text("".join(lines))
        with self.assertRaises(BridgeIntegrityError):
            list(parse_turns(self.file))

    def test_tampered_hash_detected(self):
        append_turn(self.file, self._make_turn(body="content"))
        text = self.file.read_text()
        # Replace body_sha256 with all zeroes
        text = self._replace_first_line_starting_with(text, "body_sha256:", "body_sha256: " + "0" * 64)
        self.file.write_text(text)
        with self.assertRaises(BridgeIntegrityError):
            list(parse_turns(self.file))

    def test_whitespace_in_base64_tolerated(self):
        # Build a turn manually, then split the base64 line across multiple lines
        # and add internal whitespace; the parser should still validate.
        body = "whitespace tolerance check"
        body_bytes = body.encode("utf-8")
        sha = hashlib.sha256(body_bytes).hexdigest()
        b64 = base64.b64encode(body_bytes).decode("ascii")
        # Inject extra whitespace and a soft newline mid-base64
        midpoint = len(b64) // 2
        wrapped = b64[:midpoint] + "\n  " + b64[midpoint:]
        self.file.write_text(
            "<!--BRIDGE-TURN-BEGIN turn_id=1-->\n"
            "---\n"
            "turn_id: 1\n"
            "ts: 2026-05-08T03:15:22Z\n"
            "role: executor\n"
            "session_id: s\n"
            "revision: 1\n"
            "consumed_at: null\n"
            "body_encoding: base64\n"
            f"body_sha256: {sha}\n"
            "---\n"
            f"{wrapped}\n"
            "<!--BRIDGE-TURN-END turn_id=1-->\n"
        )
        parsed = list(parse_turns(self.file))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].body, body)

    @staticmethod
    def _replace_first_line_starting_with(text: str, prefix: str, replacement: str) -> str:
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = replacement + "\n"
                break
        return "".join(lines)


class MarkConsumedTests(FormatTestBase):
    def test_basic_mark_consumed(self):
        append_turn(self.file, self._make_turn(turn_id=7, body="please consume me"))
        ok = mark_consumed(self.file, turn_id=7, consumed_at="2026-05-08T04:00:00Z")
        self.assertTrue(ok)
        parsed = list(parse_turns(self.file))
        self.assertEqual(parsed[0].consumed_at, "2026-05-08T04:00:00Z")

    def test_mark_consumed_preserves_integrity(self):
        # After mark_consumed, body bytes must still validate against body_sha256
        append_turn(self.file, self._make_turn(turn_id=7, body="integrity matters"))
        mark_consumed(self.file, turn_id=7, consumed_at="2026-05-08T04:00:00Z")
        # parse_turns runs the integrity check; if it passes, body_sha256 still matches
        parsed = list(parse_turns(self.file))
        self.assertEqual(parsed[0].body, "integrity matters")

    def test_mark_consumed_returns_false_for_unknown_turn(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="x"))
        ok = mark_consumed(self.file, turn_id=999)
        self.assertFalse(ok)

    def test_mark_consumed_returns_false_when_already_consumed(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="x"))
        self.assertTrue(mark_consumed(self.file, turn_id=1))
        self.assertFalse(mark_consumed(self.file, turn_id=1))

    def test_mark_consumed_picks_highest_revision_when_unspecified(self):
        append_turn(self.file, self._make_turn(turn_id=3, revision=1, body="draft v1"))
        append_turn(self.file, self._make_turn(turn_id=3, revision=2, body="draft v2"))
        ok = mark_consumed(self.file, turn_id=3, consumed_at="2026-05-08T04:00:00Z")
        self.assertTrue(ok)
        parsed = list(parse_turns(self.file))
        # rev 1 untouched, rev 2 marked consumed
        rev1 = next(t for t in parsed if t.revision == 1)
        rev2 = next(t for t in parsed if t.revision == 2)
        self.assertIsNone(rev1.consumed_at)
        self.assertEqual(rev2.consumed_at, "2026-05-08T04:00:00Z")

    def test_mark_consumed_specific_revision(self):
        append_turn(self.file, self._make_turn(turn_id=3, revision=1, body="draft v1"))
        append_turn(self.file, self._make_turn(turn_id=3, revision=2, body="draft v2"))
        ok = mark_consumed(self.file, turn_id=3, revision=1, consumed_at="2026-05-08T04:00:00Z")
        self.assertTrue(ok)
        parsed = list(parse_turns(self.file))
        rev1 = next(t for t in parsed if t.revision == 1)
        rev2 = next(t for t in parsed if t.revision == 2)
        self.assertEqual(rev1.consumed_at, "2026-05-08T04:00:00Z")
        self.assertIsNone(rev2.consumed_at)


class FindUnconsumedTests(FormatTestBase):
    def test_returns_none_for_missing_file(self):
        self.assertIsNone(find_unconsumed_latest(self.file))

    def test_returns_none_when_all_consumed(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="a"))
        mark_consumed(self.file, turn_id=1)
        self.assertIsNone(find_unconsumed_latest(self.file))

    def test_returns_highest_turn_id(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="a"))
        append_turn(self.file, self._make_turn(turn_id=2, body="b"))
        append_turn(self.file, self._make_turn(turn_id=3, body="c"))
        latest = find_unconsumed_latest(self.file)
        assert latest is not None
        self.assertEqual(latest.turn_id, 3)
        self.assertEqual(latest.body, "c")

    def test_skips_consumed_within_returned_id(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="a"))
        append_turn(self.file, self._make_turn(turn_id=2, body="b"))
        mark_consumed(self.file, turn_id=2)
        latest = find_unconsumed_latest(self.file)
        assert latest is not None
        self.assertEqual(latest.turn_id, 1)

    def test_picks_highest_revision_for_a_given_turn_id(self):
        append_turn(self.file, self._make_turn(turn_id=5, revision=1, body="v1"))
        append_turn(self.file, self._make_turn(turn_id=5, revision=2, body="v2"))
        latest = find_unconsumed_latest(self.file)
        assert latest is not None
        self.assertEqual(latest.revision, 2)
        self.assertEqual(latest.body, "v2")


class FindUnconsumedOldestTests(FormatTestBase):
    """FIFO selector — counterpart to find_unconsumed_latest (Q1 2026-05-13)."""

    def test_returns_none_for_missing_file(self):
        self.assertIsNone(find_unconsumed_oldest(self.file))

    def test_returns_none_when_all_consumed(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="a"))
        append_turn(self.file, self._make_turn(turn_id=2, body="b"))
        mark_consumed(self.file, turn_id=1)
        mark_consumed(self.file, turn_id=2)
        self.assertIsNone(find_unconsumed_oldest(self.file))

    def test_single_unconsumed_turn_returned(self):
        append_turn(self.file, self._make_turn(turn_id=7, body="only"))
        oldest = find_unconsumed_oldest(self.file)
        assert oldest is not None
        self.assertEqual(oldest.turn_id, 7)
        self.assertEqual(oldest.body, "only")

    def test_returns_smallest_turn_id(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="a"))
        append_turn(self.file, self._make_turn(turn_id=2, body="b"))
        append_turn(self.file, self._make_turn(turn_id=3, body="c"))
        oldest = find_unconsumed_oldest(self.file)
        assert oldest is not None
        self.assertEqual(oldest.turn_id, 1)
        self.assertEqual(oldest.body, "a")

    def test_skips_consumed_to_next_smallest(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="a"))
        append_turn(self.file, self._make_turn(turn_id=2, body="b"))
        append_turn(self.file, self._make_turn(turn_id=3, body="c"))
        mark_consumed(self.file, turn_id=1)
        oldest = find_unconsumed_oldest(self.file)
        assert oldest is not None
        self.assertEqual(oldest.turn_id, 2)

    def test_picks_highest_revision_for_a_given_turn_id(self):
        append_turn(self.file, self._make_turn(turn_id=5, revision=1, body="v1"))
        append_turn(self.file, self._make_turn(turn_id=5, revision=2, body="v2"))
        oldest = find_unconsumed_oldest(self.file)
        assert oldest is not None
        self.assertEqual(oldest.revision, 2)
        self.assertEqual(oldest.body, "v2")

    def test_skip_turn_ids_filters_quarantined(self):
        append_turn(self.file, self._make_turn(turn_id=1, body="quarantine-me"))
        append_turn(self.file, self._make_turn(turn_id=2, body="b"))
        append_turn(self.file, self._make_turn(turn_id=3, body="c"))
        oldest = find_unconsumed_oldest(self.file, skip_turn_ids={1})
        assert oldest is not None
        self.assertEqual(oldest.turn_id, 2)


class LockTests(FormatTestBase):
    def test_lock_file_perms_0600(self):
        append_turn(self.file, self._make_turn())
        lock = self.file.parent / ".lock"
        self.assertTrue(lock.exists())
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_dir_perms_0700_when_created(self):
        deep = self.dir / "subdir" / "deeper"
        target = deep / "from-advisor.md"
        append_turn(target, self._make_turn(body="x"))
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)


class ModelProvenanceTests(FormatTestBase):
    """B2 — the model that produced a turn travels in the frontmatter."""

    def test_model_round_trips(self):
        append_turn(self.file, self._make_turn(body="done", model="claude-fable-5"))
        parsed = list(parse_turns(self.file))
        self.assertEqual(parsed[0].model, "claude-fable-5")

    def test_blank_model_renders_like_a_pre_b2_turn(self):
        # No model -> no model line -> byte-identical to the old format.
        rendered = render_turn(self._make_turn(body="x"))
        self.assertNotIn("model:", rendered)

    def test_set_model_emits_a_line(self):
        rendered = render_turn(self._make_turn(body="x", model="claude-opus-4-8"))
        self.assertIn("model: claude-opus-4-8", rendered)

    def test_old_turn_without_model_parses_to_blank(self):
        # A durable file written before B2 has no model key; parse must tolerate
        # it (no migration) and yield "".
        turn = self._make_turn(body="legacy turn", model="")
        rendered = render_turn(turn)
        self.assertNotIn("model:", rendered)
        self.file.write_text(rendered, encoding="utf-8")
        parsed = list(parse_turns(self.file))
        self.assertEqual(parsed[0].model, "")

    def test_model_not_in_body_hash(self):
        # model lives in frontmatter, so two turns with identical bodies but
        # different models share the same body_sha256 (integrity is body-only).
        a = self._make_turn(body="same body", model="claude-fable-5")
        b = self._make_turn(body="same body", model="claude-opus-4-8")
        self.assertEqual(a.body_sha256, b.body_sha256)


if __name__ == "__main__":
    unittest.main()
