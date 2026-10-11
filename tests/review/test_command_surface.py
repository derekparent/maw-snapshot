"""Tests for the review slash-command surface.

Behavioral, not just file-presence: each command .md must open with valid
frontmatter and reference the plugin-root placeholder, AND the CLI command it
invokes must actually RESOLVE — we run the module with --help / a real
subcommand and assert it does not blow up with a usage/import error. This
catches a command .md that points at a CLI verb that does not exist.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO / "maw"
COMMANDS = PLUGIN_ROOT / "commands"

sys.path.insert(0, str(PLUGIN_ROOT))


def _run_module(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(PLUGIN_ROOT)}
    import os

    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True, text=True, timeout=60, cwd=cwd, env=full_env,
    )


class FrontmatterTest(unittest.TestCase):
    COMMANDS = ("review-request", "review", "risk", "provenance")

    def test_each_command_has_frontmatter_and_plugin_root(self) -> None:
        for name in self.COMMANDS:
            path = COMMANDS / f"{name}.md"
            self.assertTrue(path.is_file(), f"{path} missing")
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---"), f"{name}.md needs frontmatter")
            self.assertIn("description:", text)
            self.assertIn("allowed-tools:", text)
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", text, f"{name}.md must use the placeholder")


class CliResolvesTest(unittest.TestCase):
    """The CLI verbs the .md files invoke must actually exist and import."""

    def test_escalate_cli_subcommands_resolve(self) -> None:
        text = (COMMANDS / "escalate.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"))
        self.assertIn("allowed-tools:", text)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", text)
        self.assertIn("escalate.cli", text)
        for sub in ("template", "check", "send", "list"):
            cp = _run_module(["escalate.cli", sub, "--help"])
            self.assertEqual(cp.returncode, 0, f"escalate.cli {sub} --help: {cp.stderr}")

    def test_review_cli_subcommands_resolve(self) -> None:
        # review-request.md → request; review.md → pr; risk.md → risk
        for sub in ("request", "pr", "risk", "lint"):
            cp = _run_module(["review.cli", sub, "--help"])
            self.assertEqual(cp.returncode, 0, f"review.cli {sub} --help: {cp.stderr}")

    def test_provenance_subcommands_resolve(self) -> None:
        for sub in ("stamp", "verify", "head"):
            cp = _run_module(["review.provenance", sub, "--help"])
            self.assertEqual(cp.returncode, 0, f"review.provenance {sub} --help: {cp.stderr}")

    def test_harness_and_external_oracle_resolve(self) -> None:
        for mod, sub in (("review.harness", "run"), ("review.external_oracle", "check")):
            cp = _run_module([mod, sub, "--help"])
            self.assertEqual(cp.returncode, 0, f"{mod} {sub} --help: {cp.stderr}")


class RiskCliBehaviorTest(unittest.TestCase):
    """`risk` with explicit paths routes a UI path to the harness gate end-to-end."""

    def test_risk_cli_reports_oracle_gate_for_ui_path(self) -> None:
        cp = _run_module(["review.cli", "risk", "ui/MapView.swift"])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("oracle", cp.stdout)
        self.assertIn("harness", cp.stdout)

    def test_risk_cli_reports_light_gate_for_docs(self) -> None:
        cp = _run_module(["review.cli", "risk", "docs/foo.md"])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("docs", cp.stdout)
        self.assertNotIn("harness", cp.stdout)


if __name__ == "__main__":
    unittest.main()
