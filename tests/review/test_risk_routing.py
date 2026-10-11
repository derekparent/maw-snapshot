"""Tests for risk-tier ROUTING — the tier actually selects the gate (not cosmetic).

The load-bearing claim of item 4: an oracle/UI path routes to a gate that
REQUIRES the state-forcing harness + build-from-SHA provenance, while a docs
path stays light. Assert the Gate fields, not just the tier label.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import risk  # noqa: E402


class OraclePathRoutingTest(unittest.TestCase):
    def test_ui_path_routes_to_harness_and_worktree_gate(self) -> None:
        gate = risk.gate_for_paths(["Gizmo/Views/MapView.swift"])
        self.assertEqual(gate.tier, risk.ORACLE)
        # the heavier gate is SELECTED: harness + provenance + suite + worktree
        self.assertTrue(gate.harness, "oracle tier must require the state-forcing harness")
        self.assertTrue(gate.worktree)
        self.assertTrue(gate.suite)

    def test_html_and_css_are_oracle(self) -> None:
        for p in ["web/embed.html", "styles/map.css", "ui/Tab.tsx"]:
            self.assertEqual(risk.classify_path(p), risk.ORACLE, p)


class DocsPathRoutingTest(unittest.TestCase):
    def test_docs_path_stays_light_no_harness_no_suite(self) -> None:
        gate = risk.gate_for_paths(["maw/docs/non-reproducible-oracle-review.md"])
        self.assertEqual(gate.tier, risk.DOCS)
        self.assertFalse(gate.harness, "docs tier must NOT require the harness")
        self.assertFalse(gate.suite)
        self.assertFalse(gate.worktree)


class RuntimePathRoutingTest(unittest.TestCase):
    def test_runtime_code_full_but_no_harness(self) -> None:
        gate = risk.gate_for_paths(["maw/review/harness.py"])
        self.assertEqual(gate.tier, risk.RUNTIME)
        self.assertTrue(gate.suite)
        self.assertTrue(gate.worktree)
        # runtime is full review but does NOT pull the oracle harness
        self.assertFalse(gate.harness)


class MixedAndFailSafeTest(unittest.TestCase):
    def test_docs_plus_ui_routes_to_oracle_harness(self) -> None:
        # the most-thorough tier wins — a docs+UI PR must get the harness gate
        gate = risk.gate_for_paths(["README.md", "ui/View.swift"])
        self.assertEqual(gate.tier, risk.ORACLE)
        self.assertTrue(gate.harness)

    def test_empty_changeset_is_not_waved_through_as_docs(self) -> None:
        gate = risk.gate_for_paths([])
        self.assertEqual(gate.tier, risk.UNKNOWN)
        self.assertTrue(gate.suite)

    def test_code_in_docs_dir_is_runtime_not_docs(self) -> None:
        self.assertEqual(risk.classify_path("docs/deploy.py"), risk.RUNTIME)


if __name__ == "__main__":
    unittest.main()
