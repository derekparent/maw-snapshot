"""Tests for bridge.livemodel — the shared live-model transcript reader."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from bridge import livemodel  # noqa: E402


def _rec(model: str, sidechain: bool = False) -> str:
    rec = {"type": "assistant", "message": {"model": model}}
    if sidechain:
        rec["isSidechain"] = True
    return json.dumps(rec)


class LiveModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _transcript(self, name: str, lines: list[str]) -> Path:
        p = self.root / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_project_dir_encoding(self) -> None:
        self.assertEqual(
            livemodel.project_dir("/home/user/.maw-worktrees/x", self.root).name,
            "-home-user--maw-worktrees-x",
        )

    def test_last_main_model(self) -> None:
        p = self._transcript("t.jsonl", [_rec("claude-fable-5"), _rec("claude-opus-4-8")])
        self.assertEqual(livemodel.tail_last_main_model(p), "claude-opus-4-8")

    def test_sidechain_record_skipped(self) -> None:
        # CR1: a subagent (sidechain) record NEWER than the main-loop one must
        # not be read as the seat's model.
        p = self._transcript(
            "t.jsonl",
            [_rec("claude-opus-4-8"), _rec("claude-haiku-4-5", sidechain=True)],
        )
        self.assertEqual(livemodel.tail_last_main_model(p), "claude-opus-4-8")

    def test_unreadable_transcript_is_none(self) -> None:
        self.assertIsNone(livemodel.tail_last_main_model(self.root / "nope.jsonl"))

    def test_live_model_reads_freshest(self) -> None:
        proj = livemodel.project_dir("/x/y", self.root)
        proj.mkdir(parents=True)
        (proj / "old.jsonl").write_text(_rec("claude-fable-5") + "\n")
        new = proj / "new.jsonl"
        new.write_text(_rec("claude-opus-4-8") + "\n")
        os.utime(new, (time.time() + 10, time.time() + 10))
        self.assertEqual(livemodel.live_model_for_cwd("/x/y", self.root), "claude-opus-4-8")

    def test_missing_dir_fails_open(self) -> None:
        self.assertIsNone(livemodel.live_model_for_cwd("/no/such", self.root))

    def test_empty_dir_fails_open(self) -> None:
        proj = livemodel.project_dir("/x/z", self.root)
        proj.mkdir(parents=True)
        self.assertIsNone(livemodel.live_model_for_cwd("/x/z", self.root))


if __name__ == "__main__":
    unittest.main()
