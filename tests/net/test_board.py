"""net.board — the fleet-board slot protocol (#156).

board.sh + homebase are absent in CI, so the subprocess seam (runner) and the
board.sh path are injected; every assemble/merge/fire-and-forget property is
verified without homebase present. The one presence-of-effect check (src flips to
'seat' on the LIVE board) is a manual post-merge step, not CI (gate 10).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from net import board  # noqa: E402


class BoardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sh = Path(self.tmp.name) / "board.sh"
        self.sh.write_text("#!/bin/bash\n", encoding="utf-8")  # exists() is True
        self.bdir = Path(self.tmp.name) / "board"
        self.bdir.mkdir()
        self.sent: list[tuple[list[str], dict]] = []

    def _runner(self, rc: int = 0):
        def r(cmd, stdin_text):
            self.sent.append((cmd, json.loads(stdin_text)))
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")
        return r

    def _seed(self, frag: dict) -> None:
        (self.bdir / f"{frag['name']}.json").write_text(json.dumps(frag), encoding="utf-8")

    # --- build_fragment ------------------------------------------------------

    def test_build_fragment_required_and_src(self) -> None:
        f = board.build_fragment(name="maw", status="working", headline="h")
        for k in ("name", "kind", "status", "headline"):
            self.assertIn(k, f)
        self.assertEqual(f["src"], "seat")   # the provenance flip — always seat here
        self.assertEqual(f["kind"], "pair")

    def test_build_fragment_omits_empty_optionals(self) -> None:
        f = board.build_fragment(name="maw", status="working", headline="h")
        for k in ("detail", "goal_plain", "goal_armed_at", "armed_until", "shipped"):
            self.assertNotIn(k, f)

    # --- write (fire-and-forget) --------------------------------------------

    def test_write_missing_required_never_calls_runner(self) -> None:
        called = []

        def r(cmd, s):
            called.append(1)
            return subprocess.CompletedProcess(cmd, 0)

        ok = board.write({"name": "maw", "kind": "pair", "status": "working"},  # no headline
                         runner=r, board_sh=self.sh)
        self.assertFalse(ok)
        self.assertEqual(called, [])   # never even attempted the silently-dropped write

    def test_write_absent_board_sh_is_noop(self) -> None:
        ok = board.write(
            board.build_fragment(name="maw", status="working", headline="h"),
            runner=self._runner(), board_sh=Path("/nonexistent/board.sh"))
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])

    def test_write_nonzero_is_false(self) -> None:
        ok = board.write(
            board.build_fragment(name="maw", status="working", headline="h"),
            runner=self._runner(rc=1), board_sh=self.sh)
        self.assertFalse(ok)

    def test_write_runner_raises_is_false(self) -> None:
        def r(cmd, s):
            raise RuntimeError("board blew up")
        ok = board.write(
            board.build_fragment(name="maw", status="working", headline="h"),
            runner=r, board_sh=self.sh)
        self.assertFalse(ok)   # fire-and-forget: never propagates

    def test_write_sends_all_required(self) -> None:
        ok = board.write(
            board.build_fragment(name="maw", status="working", headline="h"),
            runner=self._runner(), board_sh=self.sh)
        self.assertTrue(ok)
        cmd, frag = self.sent[0]
        self.assertEqual(cmd[1:], ["update", "maw"])
        for k in ("name", "kind", "status", "headline"):
            self.assertTrue(frag.get(k))

    # --- claim / update / wrap ----------------------------------------------

    def test_claim_writes_working_seat_fragment_with_arm(self) -> None:
        with mock.patch.object(board, "_arm_fields",
                               return_value=("2026-07-21T05:33:00Z", "2026-07-21T11:33:00Z")):
            ok = board.claim(name="maw", goal_plain="clear the backlog",
                             runner=self._runner(), board_sh=self.sh)
        self.assertTrue(ok)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["status"], "working")
        self.assertEqual(frag["src"], "seat")
        self.assertEqual(frag["goal_plain"], "clear the backlog")
        self.assertEqual(frag["goal_armed_at"], "2026-07-21T05:33:00Z")
        self.assertEqual(frag["armed_until"], "2026-07-21T11:33:00Z")
        self.assertEqual(frag["shipped"], [])   # a fresh run resets shipped

    def test_claim_unarmed_omits_armed_until(self) -> None:
        with mock.patch.object(board, "_arm_fields", return_value=(None, None)):
            board.claim(name="maw", goal_plain="g", runner=self._runner(), board_sh=self.sh)
        _cmd, frag = self.sent[0]
        self.assertNotIn("armed_until", frag)   # renderer shows 'not armed'

    def test_update_appends_shipped_and_carries_goal(self) -> None:
        self._seed({"name": "maw", "kind": "pair", "status": "working",
                    "headline": "old", "goal_plain": "the goal",
                    "shipped": [{"what": "a", "proof": "x"}]})
        with mock.patch.object(board, "_arm_fields", return_value=(None, "exp")):
            ok = board.update(name="maw", headline="merged #1", what="feature X",
                              proof="main abc123", runner=self._runner(),
                              board_sh=self.sh, board_dir=self.bdir)
        self.assertTrue(ok)
        _cmd, frag = self.sent[0]
        self.assertEqual(len(frag["shipped"]), 2)
        self.assertEqual(frag["shipped"][-1], {"what": "feature X", "proof": "main abc123"})
        self.assertEqual(frag["goal_plain"], "the goal")   # carried from the claim
        self.assertEqual(frag["src"], "seat")

    def test_update_without_what_leaves_shipped(self) -> None:
        self._seed({"name": "maw", "kind": "pair", "status": "working",
                    "headline": "old", "shipped": [{"what": "a", "proof": "x"}]})
        board.update(name="maw", headline="status ping", runner=self._runner(),
                     board_sh=self.sh, board_dir=self.bdir)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["shipped"], [{"what": "a", "proof": "x"}])

    def test_wrap_sets_wrapped_keeps_shipped(self) -> None:
        self._seed({"name": "maw", "kind": "pair", "status": "working",
                    "headline": "old", "goal_plain": "g",
                    "shipped": [{"what": "a", "proof": "x"}]})
        ok = board.wrap(name="maw", headline="run wrapped", detail="all merged",
                        runner=self._runner(), board_sh=self.sh, board_dir=self.bdir)
        self.assertTrue(ok)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["status"], "wrapped")
        self.assertEqual(frag["shipped"], [{"what": "a", "proof": "x"}])
        self.assertEqual(frag["src"], "seat")

    def test_update_no_prior_fragment_starts_shipped(self) -> None:
        # No existing fragment (board_dir empty) -> shipped starts from the new entry.
        board.update(name="maw", headline="first merge", what="thing", proof="sha",
                     runner=self._runner(), board_sh=self.sh, board_dir=self.bdir)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["shipped"], [{"what": "thing", "proof": "sha"}])

    # --- progress-age: last_progress_at / last_evidence_ref (#149 F10) --------

    def test_claim_stamps_progress_at_run_start(self) -> None:
        # The run start anchors progress-age; evidence_ref is the optional base SHA.
        with mock.patch.object(board, "_arm_fields", return_value=(None, None)), \
                mock.patch.object(board, "_now_utc", return_value="2026-07-22T12:00:00Z"):
            board.claim(name="maw", goal_plain="g", evidence_ref="base def456",
                        runner=self._runner(), board_sh=self.sh)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["last_progress_at"], "2026-07-22T12:00:00Z")
        self.assertEqual(frag["last_evidence_ref"], "base def456")

    def test_update_with_proof_restamps_progress(self) -> None:
        # A real merge event re-stamps progress-age; evidence defaults to the proof.
        self._seed({"name": "maw", "kind": "pair", "status": "working",
                    "headline": "old", "shipped": [],
                    "last_progress_at": "2026-07-22T10:00:00Z",
                    "last_evidence_ref": "old sha"})
        with mock.patch.object(board, "_arm_fields", return_value=(None, None)), \
                mock.patch.object(board, "_now_utc", return_value="2026-07-22T12:30:00Z"):
            board.update(name="maw", headline="merged #1", what="lane 1", proof="main abc123",
                         runner=self._runner(), board_sh=self.sh, board_dir=self.bdir)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["last_progress_at"], "2026-07-22T12:30:00Z")
        self.assertEqual(frag["last_evidence_ref"], "main abc123")

    def test_update_explicit_evidence_ref_wins(self) -> None:
        with mock.patch.object(board, "_arm_fields", return_value=(None, None)), \
                mock.patch.object(board, "_now_utc", return_value="2026-07-22T13:00:00Z"):
            board.update(name="maw", headline="suite green", evidence_ref="suite:1380",
                         runner=self._runner(), board_sh=self.sh, board_dir=self.bdir)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["last_progress_at"], "2026-07-22T13:00:00Z")
        self.assertEqual(frag["last_evidence_ref"], "suite:1380")

    def test_bare_status_ping_carries_forward_progress(self) -> None:
        # The crux: a 'still working' ping with NO real event must NOT reset
        # progress-age — a stall stays derivable from the last real event.
        self._seed({"name": "maw", "kind": "pair", "status": "working",
                    "headline": "old", "shipped": [],
                    "last_progress_at": "2026-07-22T09:00:00Z",
                    "last_evidence_ref": "sha stale"})
        with mock.patch.object(board, "_arm_fields", return_value=(None, None)), \
                mock.patch.object(board, "_now_utc", return_value="2026-07-22T14:00:00Z"):
            board.update(name="maw", headline="still working", status="working",
                         runner=self._runner(), board_sh=self.sh, board_dir=self.bdir)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["last_progress_at"], "2026-07-22T09:00:00Z")  # NOT re-stamped
        self.assertEqual(frag["last_evidence_ref"], "sha stale")

    def test_wrap_carries_forward_progress(self) -> None:
        self._seed({"name": "maw", "kind": "pair", "status": "working",
                    "headline": "old", "shipped": [],
                    "last_progress_at": "2026-07-22T11:00:00Z",
                    "last_evidence_ref": "last merge"})
        board.wrap(name="maw", headline="wrapped",
                   runner=self._runner(), board_sh=self.sh, board_dir=self.bdir)
        _cmd, frag = self.sent[0]
        self.assertEqual(frag["last_progress_at"], "2026-07-22T11:00:00Z")
        self.assertEqual(frag["last_evidence_ref"], "last merge")

    def test_progress_at_is_parseable_iso_z(self) -> None:
        # homebase derives progress-age = now - last_progress_at, so the stamp must
        # be a parseable UTC Z timestamp (the real _now_utc, not a pinned fake).
        with mock.patch.object(board, "_arm_fields", return_value=(None, None)):
            board.claim(name="maw", goal_plain="g", runner=self._runner(), board_sh=self.sh)
        _cmd, frag = self.sent[0]
        from datetime import datetime
        parsed = datetime.strptime(frag["last_progress_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNotNone(parsed)

    # --- armed_until: VALIDATED, never a stale/dead token (#156 gate-10 fix) --

    def test_arm_fields_stale_token_yields_none(self) -> None:
        # A token session_auth would REFUSE (dead session / expired) must never
        # publish an armed_until — the exact defect the live gate-10 caught.
        from guards import session_auth
        with mock.patch.object(board, "_candidate_roots", return_value=[Path("/x")]), \
                mock.patch.object(session_auth, "status",
                                  return_value=(False, "NOT ARMED [session-gone]")):
            self.assertEqual(board._arm_fields(None), (None, None))

    def test_arm_fields_live_token_yields_fields(self) -> None:
        from guards import session_auth
        with mock.patch.object(board, "_candidate_roots", return_value=[Path("/x")]), \
                mock.patch.object(session_auth, "status", return_value=(True, "ARMED")), \
                mock.patch.object(session_auth, "_load_token",
                                  return_value=({"armed_at": "A", "expires_at": "E"}, None)):
            self.assertEqual(board._arm_fields(None), ("A", "E"))

    def test_arm_fields_skips_dead_root_finds_live(self) -> None:
        # Two candidate roots: the first is a dead arm (status refuses), the second
        # is live — the helper must skip the dead one and use the live token.
        from guards import session_auth
        dead, live = Path("/dead"), Path("/live")

        def fake_status(root, **_):
            return (True, "ARMED") if root == live else (False, "NOT ARMED [session-gone]")

        with mock.patch.object(board, "_candidate_roots", return_value=[dead, live]), \
                mock.patch.object(session_auth, "status", side_effect=fake_status), \
                mock.patch.object(session_auth, "_load_token",
                                  return_value=({"armed_at": "A2", "expires_at": "E2"}, None)):
            self.assertEqual(board._arm_fields(None), ("A2", "E2"))

    def test_claim_with_stale_arm_omits_armed_until(self) -> None:
        # End-to-end through claim(): a dead arm -> the fragment carries NO
        # armed_until, so the renderer shows 'not armed' rather than a live-looking
        # countdown for a dead session.
        from guards import session_auth
        with mock.patch.object(board, "_candidate_roots", return_value=[Path("/x")]), \
                mock.patch.object(session_auth, "status",
                                  return_value=(False, "NOT ARMED [session-gone]")):
            board.claim(name="maw", goal_plain="g", runner=self._runner(), board_sh=self.sh)
        _cmd, frag = self.sent[0]
        self.assertNotIn("armed_until", frag)
        self.assertNotIn("goal_armed_at", frag)

    # --- CLI fire-and-forget -------------------------------------------------

    def test_cli_exits_zero_even_when_board_absent(self) -> None:
        with mock.patch.dict(os.environ, {"MAW_BOARD_SH": "/nonexistent/board.sh"}):
            rc = board.main(["claim", "--name", "maw", "--goal", "g"])
        self.assertEqual(rc, 0)   # a board miss NEVER blocks the seat

    def test_cli_prints_updated_line_on_success_only(self) -> None:
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with mock.patch.object(board, "write", return_value=True), redirect_stderr(buf):
            board.main(["claim", "--name", "maw", "--goal", "g"])
        self.assertIn("board: maw updated", buf.getvalue())
        buf = io.StringIO()
        with mock.patch.object(board, "write", return_value=False), redirect_stderr(buf):
            board.main(["claim", "--name", "maw", "--goal", "g"])
        self.assertNotIn("updated", buf.getvalue())
        self.assertIn("write skipped", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
