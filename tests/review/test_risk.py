"""Tests for review.risk — path-classified review depth (backlog item 6)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import risk  # noqa: E402


class ClassifyPathTest(unittest.TestCase):
    def test_docs(self) -> None:
        for p in ("README.md", "docs/x.md", "findings/2026-y.md", "LICENSE", "notes.txt"):
            self.assertEqual(risk.classify_path(p), risk.DOCS, p)

    def test_runtime(self) -> None:
        for p in ("maw/net/cli.py", "maw/hooks/x.sh", "a.js", "config.yaml", "x.toml"):
            self.assertEqual(risk.classify_path(p), risk.RUNTIME, p)

    def test_oracle(self) -> None:
        for p in ("App/View.swift", "web/components/Btn.tsx", "ui/page.html", "Main.storyboard"):
            self.assertEqual(risk.classify_path(p), risk.ORACLE, p)

    def test_unknown(self) -> None:
        for p in ("data.bin", "image.png", "weird.xyz", "Makefile.frob"):
            self.assertEqual(risk.classify_path(p), risk.UNKNOWN, p)

    def test_extension_beats_directory(self) -> None:
        # fail-safe: code in a docs dir is runtime, not docs
        self.assertEqual(risk.classify_path("docs/deploy.py"), risk.RUNTIME)
        # a .md inside a components dir is still docs (ext authoritative)
        self.assertEqual(risk.classify_path("web/components/README.md"), risk.DOCS)
        # extensionless file uses the directory hint
        self.assertEqual(risk.classify_path("ui/thing"), risk.ORACLE)
        self.assertEqual(risk.classify_path("docs/diagram"), risk.DOCS)


class ClassifyPathsTest(unittest.TestCase):
    def test_precedence_runtime_over_docs(self) -> None:
        self.assertEqual(risk.classify_paths(["a.md", "b.py"]), risk.RUNTIME)

    def test_precedence_oracle_over_runtime(self) -> None:
        self.assertEqual(risk.classify_paths(["b.py", "v.swift"]), risk.ORACLE)

    def test_docs_only(self) -> None:
        self.assertEqual(risk.classify_paths(["a.md", "docs/b.md"]), risk.DOCS)

    def test_unknown_failsafe_over_docs(self) -> None:
        # an unrecognized path must NOT be waved through as docs
        self.assertEqual(risk.classify_paths(["a.md", "weird.xyz"]), risk.UNKNOWN)

    def test_empty_is_unknown(self) -> None:
        self.assertEqual(risk.classify_paths([]), risk.UNKNOWN)
        self.assertEqual(risk.classify_paths(["", "   "]), risk.UNKNOWN)


class RecommendTest(unittest.TestCase):
    def test_docs_is_light(self) -> None:
        g = risk.recommend(risk.DOCS)
        self.assertFalse(g.suite)
        self.assertFalse(g.worktree)
        self.assertFalse(g.harness)

    def test_runtime_is_full(self) -> None:
        g = risk.recommend(risk.RUNTIME)
        self.assertTrue(g.suite)
        self.assertTrue(g.worktree)
        self.assertFalse(g.harness)

    def test_oracle_adds_harness(self) -> None:
        g = risk.recommend(risk.ORACLE)
        self.assertTrue(g.suite)
        self.assertTrue(g.harness)

    def test_unknown_is_full_failsafe(self) -> None:
        g = risk.recommend(risk.UNKNOWN)
        self.assertTrue(g.suite)
        self.assertTrue(g.worktree)

    def test_gate_for_paths_end_to_end(self) -> None:
        self.assertEqual(risk.gate_for_paths(["a.md"]).tier, risk.DOCS)
        self.assertTrue(risk.gate_for_paths(["maw/net/x.py"]).suite)


if __name__ == "__main__":
    unittest.main()


class L0PromptSurfaceIsRuntimeTest(unittest.TestCase):
    """L0 #10: markdown the model executes is not docs."""

    def test_command_agent_skill_and_claude_md_are_runtime(self) -> None:
        for path in ("maw/commands/advisor.md", "commands/goal.md",
                     "maw/agents/reviewer.md", "skills/handoff/SKILL.md",
                     "CLAUDE.md", "maw/init/templates/CLAUDE.md"):
            with self.subTest(path):
                self.assertEqual(risk.classify_path(path), risk.RUNTIME)

    def test_ordinary_docs_stay_docs(self) -> None:
        for path in ("docs/x.md", "README.md", "maw/docs/ADVISOR-PLAYBOOK.md"):
            with self.subTest(path):
                self.assertEqual(risk.classify_path(path), risk.DOCS)

