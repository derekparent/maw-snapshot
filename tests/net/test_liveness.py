"""net.liveness — liveness evidence that ticks WITHOUT a prompt submit (#190).

Hermetic: the projects root is injected, so no test reads the real
~/.claude/projects. Time is injected via now_s.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from net import liveness
from net.registry import Entry


class LivenessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.root = Path(self._root.name)
        self.now = 1_800_000_000.0

    def _transcript(self, cwd: str, age_min: float, name: str = "s1.jsonl") -> Path:
        proj = liveness.project_dir(cwd, self.root)
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / name
        path.write_text("{}\n", encoding="utf-8")
        mtime = self.now - age_min * 60
        os.utime(path, (mtime, mtime))
        return path


class TestTranscriptAge(LivenessTestCase):
    def test_age_of_the_freshest_transcript(self) -> None:
        cwd = "/home/user/.maw-worktrees/maw-executor"
        self._transcript(cwd, 90, "old.jsonl")
        self._transcript(cwd, 3, "new.jsonl")
        age = liveness.transcript_age_min(cwd, self.now, self.root)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 3.0, places=3)

    def test_no_project_dir_is_no_evidence(self) -> None:
        self.assertIsNone(
            liveness.transcript_age_min("/nowhere/at/all", self.now, self.root))

    def test_empty_project_dir_is_no_evidence(self) -> None:
        cwd = "/home/user/empty"
        liveness.project_dir(cwd, self.root).mkdir(parents=True)
        self.assertIsNone(liveness.transcript_age_min(cwd, self.now, self.root))

    def test_future_mtime_clamps_to_zero_not_negative(self) -> None:
        # A clock skew must never read as "stale"; it reads as fresh-now.
        cwd = "/home/user/skewed"
        self._transcript(cwd, -10)
        self.assertEqual(liveness.transcript_age_min(cwd, self.now, self.root), 0.0)


class TestEvidenceFor(LivenessTestCase):
    def _entry(self, name: str, cwd: str, transcript_path: str = "") -> Entry:
        return Entry(name=name, role=name, repo_root=cwd, cwd=cwd, pid=1,
                     tty=None, tmux_pane=None, session_id="sid",
                     started_at="2026-08-30T00:00:00Z",
                     last_seen="2026-08-30T00:00:00Z",
                     transcript_path=transcript_path)

    def test_pinned_transcript_is_the_evidence(self) -> None:
        cwd = "/home/user/.maw-worktrees/maw-advisor"
        mine = self._transcript(cwd, 4, "mine.jsonl")
        ev = liveness.evidence_for(
            "advisor", self.now,
            entries=[self._entry("advisor", cwd, str(mine))],
            projects_root=self.root)
        self.assertAlmostEqual(ev.age_min, 4.0, places=3)
        self.assertEqual(ev.basis, "session-transcript")

    def test_a_fresh_SIBLING_transcript_does_not_prove_the_seat_is_alive(self) -> None:
        """The #210 review finding. A seat's project dir holds every session that
        ran in that cwd — headless `claude -p` children, a rotated seat. A wedged
        seat whose sibling keeps writing must still read STALE."""
        cwd = "/home/user/.maw-worktrees/maw-executor"
        mine = self._transcript(cwd, 20, "mine.jsonl")
        self._transcript(cwd, 0.1, "a-subagent-child.jsonl")
        ev = liveness.evidence_for(
            "executor", self.now,
            entries=[self._entry("executor", cwd, str(mine))],
            projects_root=self.root)
        self.assertAlmostEqual(ev.age_min, 20.0, places=3)
        self.assertEqual(ev.basis, "session-transcript")

    def test_pinned_transcript_gone_is_no_evidence(self) -> None:
        cwd = "/home/user/gone"
        ev = liveness.evidence_for(
            "executor", self.now,
            entries=[self._entry("executor", cwd, "/nope/missing.jsonl")],
            projects_root=self.root)
        self.assertIsNone(ev.age_min)
        self.assertEqual(ev.basis, "none")

    def test_entry_without_a_pinned_transcript_falls_back_and_SAYS_SO(self) -> None:
        """Old transplants and hand-registered seats have no transcript_path. The
        weaker dir-max basis is allowed, but it is never silent."""
        cwd = "/home/user/.maw-worktrees/maw-advisor"
        self._transcript(cwd, 4)
        ev = liveness.evidence_for(
            "advisor", self.now, entries=[self._entry("advisor", cwd)],
            projects_root=self.root)
        self.assertAlmostEqual(ev.age_min, 4.0, places=3)
        self.assertEqual(ev.basis, "dir-max-fallback")

    def test_unknown_seat_is_no_evidence(self) -> None:
        ev = liveness.evidence_for("ghost", self.now, entries=[],
                                   projects_root=self.root)
        self.assertIsNone(ev.age_min)
        self.assertEqual(ev.basis, "none")

    def test_registry_read_failure_is_no_evidence_not_a_crash(self) -> None:
        with mock.patch("net.liveness.who", side_effect=OSError("boom")):
            ev = liveness.evidence_for("advisor", self.now, projects_root=self.root)
        self.assertIsNone(ev.age_min)
        self.assertEqual(ev.basis, "none")


class TestRegisterHookPinsTheTranscript(unittest.TestCase):
    """The hook is the only place the transcript path exists; if it does not reach
    the registry, every seat silently runs on the weaker dir-max basis."""

    def test_hook_parses_transcript_path_from_the_sessionstart_payload(self) -> None:
        import subprocess
        hook = Path(__file__).resolve().parents[2] / "maw" / "hooks" / "net-register.sh"
        src = hook.read_text(encoding="utf-8")
        self.assertIn("--transcript-path", src)
        # the extractor itself, run against a real SessionStart-shaped payload
        extractor = src.split("transcript=$(printf '%s' \"$payload\" | python3 -c \\\n")[1]
        extractor = extractor.split("' 2>/dev/null")[0].lstrip().lstrip("'")
        payload = json.dumps({"session_id": "abc", "transcript_path": "/tmp/abc.jsonl"})
        got = subprocess.run(["python3", "-c", extractor], input=payload,
                             capture_output=True, text=True)
        self.assertEqual(got.stdout.strip(), "/tmp/abc.jsonl")

    def test_extractor_survives_a_payload_without_the_field(self) -> None:
        import subprocess
        hook = Path(__file__).resolve().parents[2] / "maw" / "hooks" / "net-register.sh"
        src = hook.read_text(encoding="utf-8")
        extractor = src.split("transcript=$(printf '%s' \"$payload\" | python3 -c \\\n")[1]
        extractor = extractor.split("' 2>/dev/null")[0].lstrip().lstrip("'")
        for payload in ("", "not json", json.dumps({"session_id": "abc"})):
            got = subprocess.run(["python3", "-c", extractor], input=payload,
                                 capture_output=True, text=True)
            self.assertEqual(got.returncode, 0, payload)
            self.assertEqual(got.stdout.strip(), "", payload)


if __name__ == "__main__":
    unittest.main()
