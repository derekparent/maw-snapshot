"""Every doc a command or hook points a seat to is inside the plugin (final pass
2026-10-09, finding 4).

A marketplace install copies maw/ alone into a cache directory: the repo's
docs/ does not travel. So these checks run against a COPY OF maw/ ALONE. Run
against the checkout they could not see the gap, because docs/ is right there.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAW = ROOT / "maw"

IN_PLUGIN = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/(docs/[A-Za-z0-9_./-]+\.md)")
# A repo-relative doc path with nothing in front of it: in a seat's cwd (the
# target repo) or in a cache copy that file does not exist.
BARE = re.compile(r"(?<![\w/}.-])docs/[A-Za-z0-9_./-]+\.md")
# Not citations: /maw:risk's usage example names a made-up path to classify.
EXAMPLES = {"docs/foo.md"}


class DocsTravelWithThePluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.copy = Path(self.tmp.name).resolve() / "cache" / "maw" / "maw" / "0.0.0"
        shutil.copytree(MAW, self.copy, ignore=shutil.ignore_patterns("__pycache__"))
        self.commands = sorted((self.copy / "commands").glob("*.md"))
        self.assertGreaterEqual(len(self.commands), 10)

    def test_every_doc_a_command_cites_resolves_in_the_copy(self) -> None:
        cited = {(f.name, rel) for f in self.commands
                 for rel in IN_PLUGIN.findall(f.read_text(encoding="utf-8"))}
        self.assertIn(("advisor.md", "docs/ADVISOR-PLAYBOOK.md"), cited)
        self.assertIn(("executor.md", "docs/watch-recipes.md"), cited)
        missing = sorted(f"{name}: {rel}" for name, rel in cited
                         if not (self.copy / rel).is_file())
        self.assertEqual(missing, [])

    def test_no_command_cites_a_doc_by_a_repo_relative_path(self) -> None:
        hits = sorted(f"{f.name}:{i}: {m}"
                      for f in self.commands
                      for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
                      for m in BARE.findall(line) if m not in EXAMPLES)
        self.assertEqual(hits, [], "cite it as ${CLAUDE_PLUGIN_ROOT}/docs/<name> and keep it in maw/docs/")

    def test_docs_cross_references_resolve_in_the_copy(self) -> None:
        """A plugin doc does not name a sibling by its old repo path."""
        docs = self.copy / "docs"
        names = {p.relative_to(docs).as_posix() for p in docs.rglob("*.md")}
        self.assertGreaterEqual(len(names), 5)
        for f in sorted(docs.rglob("*.md")):
            text = f.read_text(encoding="utf-8")
            # A citation of a doc that was left in the repo's docs/ is a dead
            # end in an install, not a broken instruction; one that names a
            # doc living HERE by its old repo path is a stale reference.
            stale = [m for m in BARE.findall(text) if m[len("docs/"):] in names]
            self.assertEqual(stale, [], f.name)

    def test_the_watchdog_hook_names_a_doc_that_exists(self) -> None:
        for env_root in (str(self.copy), None):
            with self.subTest(env_root=env_root):
                env = {"PATH": os.environ.get("PATH", ""), "MAW_BRIDGE_ROLE": "advisor"}
                if env_root:
                    env["CLAUDE_PLUGIN_ROOT"] = env_root
                r = subprocess.run(["sh", str(self.copy / "hooks" / "watchdog-arm.sh")],
                                   env=env, capture_output=True, text=True, timeout=30)
                paths = re.findall(r"see (\S+/docs/watch-recipes\.md)", r.stdout)
                self.assertEqual(len(paths), 1, r.stdout[:400])
                self.assertTrue(Path(paths[0]).is_file(), paths[0])

    def test_the_repo_docs_dir_does_not_keep_a_second_copy(self) -> None:
        for f in sorted((MAW / "docs").rglob("*.md")):
            rel = f.relative_to(MAW / "docs")
            self.assertFalse((ROOT / "docs" / rel).exists() or (ROOT / "docs" / rel).is_symlink(), rel)


if __name__ == "__main__":
    unittest.main()
