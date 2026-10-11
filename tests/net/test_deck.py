"""net.deck — the seat's runs report to the operator's Deck (lane R4).

The property under attack: a seat writes ONLY the `runs` collection. A seat's
write to `cards` or `signals` is a forged operator decision (the seat calls
ArtifactData as the operator's account). `render` must be unable to name
another collection, and `audit` must fire on any non-runs write in a transcript
while staying quiet on runs writes and on reads.

Falsifier: a transcript holding an ArtifactData `update` to `cards` (or a
`batch` with one `cards` entry) for which `audit` exits 0. If any test in
AuditFiresTest passes with the collection check deleted from
`write_violations`, the check is decorative.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

MAW = Path(__file__).resolve().parents[2] / "maw"
sys.path.insert(0, str(MAW))

from net import deck  # noqa: E402

URL = "https://claude.ai/artifact/TESTDECK"


def full_doc(**over) -> dict:
    """Every field, through the real builder."""
    kw = dict(repo="maw", goal="Ship the deck runs report", stage="working",
              advisor_pane="w1:p1", executor_pane="w1:p2", prs_merged=[290, 291],
              prs_open=[293], blocked_on="", next_step="Cut the audit PR")
    kw.update(over)
    return deck.build_run(**kw)


def record(tool_inputs: list[tuple[str, dict]]) -> dict:
    """One assistant record shaped like a real Claude Code transcript line
    (envelope keys copied from a live transcript; content is synthetic)."""
    return {
        "parentUuid": "p-1", "isSidechain": False, "userType": "external",
        "cwd": "/tmp/x", "sessionId": "s-1", "version": "2.1.0", "gitBranch": "main",
        "type": "assistant", "uuid": "u-1", "timestamp": "2026-09-30T09:30:00.000Z",
        "requestId": "req_1",
        "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "reporting"}] + [
                {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp,
                 "caller": {"type": "direct"}}
                for i, (name, inp) in enumerate(tool_inputs)],
            "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


class Transcript:
    def __init__(self, test: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "sess-1.jsonl"
        self.path.write_text(json.dumps({"type": "user", "message": {
            "role": "user", "content": "go"}}) + "\n", encoding="utf-8")

    def add(self, *tool_inputs: tuple[str, dict]) -> "Transcript":
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record(list(tool_inputs))) + "\n")
        return self

    def run(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = deck.main(["audit", "--transcript", str(self.path)])
        return rc, out.getvalue(), err.getvalue()


class RenderTest(unittest.TestCase):
    def test_full_field_set_and_order(self) -> None:
        doc = full_doc()
        self.assertEqual(tuple(doc), deck.FIELDS)
        self.assertEqual(doc["prs_merged"], [290, 291])
        datetime.strptime(doc["last_report"], "%Y-%m-%dT%H:%M:%SZ")

    def test_first_write_is_set_later_is_pinned_update(self) -> None:
        first = deck.build_call(url=URL, doc_id="maw-deck-0930", doc=full_doc(), if_version=None)
        self.assertEqual(first["action"], "set")
        self.assertNotIn("if_version", first)
        later = deck.build_call(url=URL, doc_id="maw-deck-0930", doc=full_doc(), if_version=3)
        self.assertEqual((later["action"], later["if_version"]), ("update", 3))
        for call in (first, later):
            self.assertEqual(call["collection"], "runs")

    def test_cli_render_prints_runs_call(self) -> None:
        out = io.StringIO()
        with mock.patch.object(deck, "repo_slug", return_value="maw"), \
                mock.patch.object(deck, "seat_panes",
                                  return_value={"advisor_pane": "a", "executor_pane": "e"}), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = deck.main(["render", "--doc-id", "maw-deck-0930", "--stage", "blocked",
                            "--goal", "g", "--next", "n", "--blocked-on", "Q7 relay header",
                            "--prs-merged", "1,#2", "--prs-open", "", "--url", URL,
                            "--if-version", "4"])
        self.assertEqual(rc, 0)
        call = json.loads(out.getvalue())
        self.assertEqual(call["collection"], "runs")
        self.assertEqual(call["action"], "update")
        self.assertEqual(call["data"]["prs_merged"], [1, 2])
        self.assertEqual(call["data"]["prs_open"], [])
        self.assertEqual(call["data"]["advisor_pane"], "a")

    def test_no_collection_flag_exists(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            deck.main(["render", "--doc-id", "x", "--stage", "working", "--goal", "g",
                       "--next", "n", "--url", URL, "--collection", "cards"])

    def test_field_drift_raises_even_under_python_O(self) -> None:
        """The field-set check must be a raise, not an assert that -O strips."""
        with mock.patch.object(deck, "FIELDS", deck.FIELDS + ("extra",)), \
                self.assertRaises(deck.DeckError):
            full_doc()
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from net import deck\n"
            "deck.FIELDS = deck.FIELDS[:-1]\n"
            "try:\n"
            "    deck.build_run(repo='maw', goal='g', stage='working', advisor_pane='',"
            " executor_pane='', prs_merged=[], prs_open=[], blocked_on='', next_step='n')\n"
            "except deck.DeckError:\n    sys.exit(0)\n"
            "sys.exit(3)\n")
        cp = subprocess.run([sys.executable, "-O", "-c", code, str(MAW)],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_stage_enum_rejected(self) -> None:
        with self.assertRaises(deck.DeckError):
            full_doc(stage="done")

    def test_newlines_rejected(self) -> None:
        for field, val in (("goal", "a\nb"), ("next_step", "a\rb"),
                           ("blocked_on", "x y"), ("repo", "m\naw")):
            with self.subTest(field=field), self.assertRaises(deck.DeckError):
                full_doc(**{field: val})

    def test_blocked_needs_blocked_on(self) -> None:
        with self.assertRaises(deck.DeckError):
            full_doc(stage="blocked", blocked_on="")

    def test_pr_lists_are_ints(self) -> None:
        with self.assertRaises(deck.DeckError):
            deck.parse_ints("--prs-merged", "1,abc")
        with self.assertRaises(deck.DeckError):
            full_doc(prs_open=["3"])
        with self.assertRaises(deck.DeckError):
            full_doc(prs_merged=[True])

    def test_bad_doc_id_and_url_rejected(self) -> None:
        with self.assertRaises(deck.DeckError):
            deck.build_call(url=URL, doc_id="a/b", doc=full_doc(), if_version=None)
        with self.assertRaises(deck.DeckError):
            deck.build_call(url="https://evil.example/x", doc_id="a", doc=full_doc(),
                            if_version=None)


class DocIdTest(unittest.TestCase):
    STARTED = datetime(2026, 9, 30, 9, 30, tzinfo=timezone.utc)

    def test_brief_id_wins(self) -> None:
        brief = "Goal: ship R4\n- doc id maw-deck-0930\nmore\n"
        self.assertEqual(deck.resolve_doc_id(brief_text=brief, repo="maw",
                                             started=self.STARTED), "maw-deck-0930")

    def test_derived_from_confirm_start_utc(self) -> None:
        self.assertEqual(deck.resolve_doc_id(brief_text="no id here", repo="maw",
                                             started=self.STARTED), "maw-20260930-0930")

    def test_two_brief_ids_refuse(self) -> None:
        with self.assertRaises(deck.DeckError):
            deck.brief_doc_id("doc id a-1\ndoc id: b-2\n")

    REAL_BRIEF = (
        "# Launch brief: maw pair, lane R4\n"
        "Goal: seats report run status to the operator's Deck.\n"
        "Report to runs (doc id maw-deck-0930) at start, each merge, block and wrap, "
        "once R4 lands, by hand before that. Wrap at about 30% context. No per-seat "
        "HTML report. No AI trailers in commits.\n"
        "Lanes: R4 first, then feedback triage.\n")

    def test_real_cos_brief_line_mid_sentence(self) -> None:
        """Falsifier: the verbatim CoS line resolving to anything but its id."""
        self.assertEqual(deck.resolve_doc_id(brief_text=self.REAL_BRIEF, repo="maw",
                                             started=self.STARTED), "maw-deck-0930")
        self.assertEqual(deck.brief_doc_id(self.REAL_BRIEF.splitlines()[2]), "maw-deck-0930")

    def test_brief_id_variants(self) -> None:
        for text in ("Doc ID: `maw-deck-0930`.", "doc_id=maw-deck-0930,",
                     "the doc-id is maw-deck-0930", "**doc id** maw-deck-0930"):
            with self.subTest(text=text):
                self.assertEqual(deck.brief_doc_id(text), "maw-deck-0930")

    def test_same_id_twice_is_fine(self) -> None:
        self.assertEqual(deck.brief_doc_id(
            "doc id maw-deck-0930\nreminder: (doc id maw-deck-0930)"), "maw-deck-0930")

    def test_placeholders_are_not_ids(self) -> None:
        for text in ("Doc ID: TBD", "doc id: todo", "doc id XXX", "doc id none",
                     "doc id n-a", "doc id <id>", "doc id: <repo>-<YYYYMMDD>-<HHMM>",
                     "doc id `<repo>-0930`", "doc id pending"):
            with self.subTest(text=text):
                self.assertIsNone(deck.brief_doc_id(text))
        self.assertEqual(deck.resolve_doc_id(brief_text="Doc ID: TBD", repo="maw",
                                             started=self.STARTED), "maw-20260930-0930")

    def test_placeholder_does_not_trigger_two_ids_refusal(self) -> None:
        self.assertEqual(deck.brief_doc_id(
            "Doc ID: TBD\ntemplate: doc id <repo>-<YYYYMMDD>-<HHMM>\n"
            "Report to runs (doc id maw-deck-0930) at start."), "maw-deck-0930")

    def test_repo_slug_from_remote_not_cwd(self) -> None:
        for url in ("git@github.com:operator/maw.git",
                    "https://github.com/operator/maw", "/srv/git/maw.git"):
            self.assertEqual(deck._slug_from_remote(url), "maw")


class AuditQuietTest(unittest.TestCase):
    def test_quiet_on_exactly_what_render_emits(self) -> None:
        first = deck.build_call(url=URL, doc_id="maw-deck-0930", doc=full_doc(), if_version=None)
        later = deck.build_call(url=URL, doc_id="maw-deck-0930",
                                doc=full_doc(stage="wrapped"), if_version=2)
        t = Transcript(self).add(("ArtifactData", first), ("ArtifactData", later))
        self.assertEqual(t.run(), (0, "", ""))

    def test_quiet_on_reads_of_cards_and_signals(self) -> None:
        t = Transcript(self).add(
            ("ArtifactData", {"url": URL, "action": "query", "collection": "cards",
                              "query": {"limit": 5}}),
            ("ArtifactData", {"url": URL, "action": "list", "collection": "signals"}),
            ("ArtifactData", {"url": URL, "action": "get", "collection": "cards",
                              "doc_id": "c1"}))
        self.assertEqual(t.run()[0], 0)

    def test_quiet_on_runs_batch_and_other_tools(self) -> None:
        t = Transcript(self).add(
            ("ArtifactData", {"url": URL, "action": "batch", "writes": [
                {"op": "update", "collection": "runs", "doc_id": "r1", "data": {},
                 "if_version": 2}]}),
            ("Bash", {"command": "echo collection cards"}))
        self.assertEqual(t.run()[0], 0)


class AuditFiresTest(unittest.TestCase):
    def assertFires(self, t: Transcript, needle: str) -> None:
        rc, out, err = t.run()
        self.assertEqual(rc, 1, out + err)
        self.assertIn(needle, out)
        self.assertIn("page the operator", err)

    def test_fires_on_cards_update(self) -> None:
        t = Transcript(self).add(("ArtifactData", {
            "url": URL, "action": "update", "collection": "cards", "doc_id": "c1",
            "data": {"decision": "approve"}, "if_version": 4}))
        self.assertFires(t, "'cards'")

    def test_fires_when_the_line_escapes_the_tool_name(self) -> None:
        # Valid JSON may spell any character as \uXXXX; a substring prefilter on
        # "ArtifactData" (or "tool_use") would skip this line entirely.
        t = Transcript(self).add(("ArtifactData", {
            "url": URL, "action": "set", "collection": "cards", "doc_id": "c1",
            "data": {"decision": "approve"}}))
        raw = t.path.read_text(encoding="utf-8").splitlines()
        raw[-1] = (raw[-1].replace("ArtifactData", "\\u0041rtifactData")
                   .replace("tool_use", "tool\\u005fuse"))
        t.path.write_text("\n".join(raw) + "\n", encoding="utf-8")
        self.assertNotIn("ArtifactData", raw[-1])
        self.assertFires(t, "'cards'")

    def test_fires_on_signals_set(self) -> None:
        t = Transcript(self).add(("ArtifactData", {
            "url": URL, "action": "set", "collection": "signals", "doc_id": "s1",
            "data": {"x": 1}}))
        self.assertFires(t, "'signals'")

    def test_fires_on_batch_hiding_a_cards_op(self) -> None:
        t = Transcript(self).add(("ArtifactData", {"url": URL, "action": "batch", "writes": [
            {"op": "update", "collection": "runs", "doc_id": "r1", "data": {}},
            {"op": "delete", "collection": "cards", "doc_id": "c1", "if_version": 1}]}))
        self.assertFires(t, "batch[1] delete to collection 'cards'")

    def test_fires_on_mcp_prefixed_name_and_str_replace_and_subcollection(self) -> None:
        t = Transcript(self).add(
            ("mcp__claude_ai__ArtifactData", {"url": URL, "action": "str_replace",
                                              "collection": "cards", "doc_id": "c",
                                              "field": "f", "old_str": "a", "new_str": "b"}),
            ("ArtifactData", {"url": URL, "action": "update",
                              "collection": "runs/r1/cards", "doc_id": "c", "data": {}}))
        rc, out, _ = t.run()
        self.assertEqual(rc, 1)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_unknown_action_and_unreadable_batch_fire(self) -> None:
        t = Transcript(self).add(
            ("ArtifactData", {"url": URL, "action": "upsert", "collection": "cards"}),
            ("ArtifactData", {"url": URL, "action": "batch", "writes": "oops"}))
        rc, out, _ = t.run()
        self.assertEqual(rc, 1)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_fires_on_subagent_transcript(self) -> None:
        t = Transcript(self)
        sub = t.dir / "sess-1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text(json.dumps(record([("ArtifactData", {
            "url": URL, "action": "set", "collection": "cards", "doc_id": "c",
            "data": {}})])) + "\n", encoding="utf-8")
        self.assertFires(t, "agent-a1.jsonl")

    def test_missing_transcript_is_not_clean(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = deck.main(["audit", "--transcript", "/nonexistent/x.jsonl"])
        self.assertEqual(rc, 2)


class BriefProseIsNotAnIdTest(unittest.TestCase):
    """Advisor round 2 on #294: prose after "doc id is" read as an id."""

    def test_hyphenated_prose_is_not_an_id(self) -> None:
        for text in ("the doc id is self-chosen by the advisor",
                     "doc id: re-derived", "the doc id is derived from the start",
                     "Doc ID: TBD"):
            self.assertIsNone(deck.brief_doc_id(text), text)

    def test_real_id_beside_prose_still_wins(self) -> None:
        text = ("Report to runs (doc id maw-deck-0930) at start.\n"
                "Later: the doc id is re-derived never.\n")
        self.assertEqual(deck.brief_doc_id(text), "maw-deck-0930")


class TranscriptResolutionTest(unittest.TestCase):
    def test_session_id_pins_the_file_not_newest_in_dir(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cwd = "/home/operator/wt/maw-advisor"
            proj = Path(root) / "-home-operator-wt-maw-advisor"
            proj.mkdir()
            (proj / "mine.jsonl").write_text("", encoding="utf-8")
            (proj / "newer-child.jsonl").write_text("", encoding="utf-8")
            with mock.patch("bridge.livemodel.PROJECTS_ROOT", Path(root)):
                p = deck.resolve_transcript(path=None, session_id="mine", cwd=cwd, seat=None)
            self.assertEqual(p, proj / "mine.jsonl")

    def test_session_id_path_traversal_refused(self) -> None:
        with self.assertRaises(deck.DeckError):
            deck.resolve_transcript(path=None, session_id="../x", cwd="/a", seat=None)

    def test_seat_uses_registry_pinned_transcript_and_refuses_unpinned(self) -> None:
        from net import registry
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"MAW_NET_DIR": d}):
            base = dict(name="advisor", role="advisor", repo_root="/r", cwd="/r",
                        pid=os.getpid(), tty=None, tmux_pane=None, session_id="s",
                        started_at="t", last_seen="t")
            registry._write_entry(registry.Entry(**base, transcript_path="/t/s.jsonl"))
            self.assertEqual(deck.resolve_transcript(path=None, session_id=None, cwd=None,
                                                     seat="advisor"), Path("/t/s.jsonl"))
            registry._write_entry(registry.Entry(**{**base, "name": "executor"}))
            with self.assertRaises(deck.DeckError):
                deck.resolve_transcript(path=None, session_id=None, cwd=None, seat="executor")


class PeerAuditDoctrineTest(unittest.TestCase):
    """The wrap audit must be the COUNTER-SEAT's, not only the claimant's own.

    Falsifier: a seat command whose CLOSE step audits only `--as <your-name>`."""

    ROOT = MAW.parent

    def read(self, rel: str) -> str:
        return (self.ROOT / rel).read_text(encoding="utf-8")

    def test_each_seat_audits_its_peer_found_by_role(self) -> None:
        for rel, peer in (("maw/commands/advisor.md", "--as <executor-name>"),
                          ("maw/commands/executor.md", "--as <advisor-name>"),
                          ("maw/commands/goal.md", "--as <peer-name>")):
            with self.subTest(rel=rel):
                text = self.read(rel)
                self.assertIn(f"net.deck audit {peer}", text)
                self.assertIn("net.cli who", text)
                self.assertIn("ROLE", text)
                self.assertIn("page the operator", text)

    def test_misses_named_in_module_and_playbook(self) -> None:
        playbook = self.read("maw/docs/ADVISOR-PLAYBOOK.md")
        self.assertIn("COUNTER-SEAT", playbook)
        for text in (deck.__doc__ or "", playbook):
            self.assertIn("unregisters", text)
            self.assertRegex(text, r"rewrites\s+its\s+registry\s+entry")

    def test_goal_md_says_pass_url_when_deck_url_unset(self) -> None:
        self.assertRegex(self.read("maw/commands/goal.md"),
                         r"when `deck_url` is unset[^\n]*\n?[^\n]*pass `--url")


if __name__ == "__main__":
    unittest.main()
