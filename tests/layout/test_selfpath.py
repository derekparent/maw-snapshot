"""No hint, hook or shipped doc names the operator's pinned checkout, and every
launcher path maw prints exists in the copy that printed it (final pass
2026-10-09, finding 3).

A marketplace install copies maw/ alone into a cache directory. It has no
~/.maw/plugin-main, and `CLAUDE_PLUGIN_ROOT` is not in a seat's Bash
environment, so a printed command must be derived from the running copy."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAW = ROOT / "maw"

# The pinned-worktree deploy is the one place that legitimately names the pin:
# the repin tooling, the stamp lookup that reads it when it exists, and the
# post-merge hook that drives it.
PIN_OWNERS = ("repin/", "init/substrate.py", "init/templates/git-hooks/post-merge")

PROBE = r"""
import json, sys
from bridge import cli as bridge_cli
from guards import human_grant
from net import registry, transport
from phone_mint import relay_client
from review import freshness
import selfpath

def entry(name):
    return registry.Entry(name=name, role=name, repo_root="", cwd="", pid=1, tty=None,
                          tmux_pane=None, session_id=name, started_at="", last_seen="")

import time
from pathlib import Path
grant = {"batch": [{"repo": "r"}], "ttl_seconds": 60,
         "issued_at": time.strftime(human_grant._TS_FMT, time.gmtime(0))}
print(json.dumps({
    "root": str(selfpath.PLUGIN_ROOT),
    "launcher": selfpath.MAW_RUN,
    "hints": {
        "reply": transport.wrap_message("x", entry("executor")),
        "recv": transport.pointer_body(entry("executor"), entry("advisor"), 1, "hi"),
        "ingest": bridge_cli.ingest_command(1, "advisor"),
        "restack": freshness.RESTACK_CMD,
        "ack-keyset": human_grant.ACK_KEYSET_CMD,
        "batch-arm": relay_client.batch_notice(grant, 0, Path("/tmp/g.json")),
    },
}))
"""


def shipped_docs() -> "list[Path]":
    """The docs a reader outside the operator's machine gets: the export
    allowlist here, and every doc in a tree that IS an export."""
    tool = ROOT / "scripts" / "export_release.py"
    if tool.is_file():
        spec = importlib.util.spec_from_file_location("export_release", tool)
        er = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(er)
        return [ROOT / rel for rel in er.DOCS_ALLOW] + sorted((MAW / "docs").rglob("*.md"))
    return sorted((ROOT / "docs").rglob("*.md")) + sorted((MAW / "docs").rglob("*.md"))


class NoOperatorPathTests(unittest.TestCase):
    def test_nothing_in_the_plugin_names_the_pinned_checkout_but_the_deploy(self) -> None:
        hits = []
        for f in sorted(MAW.rglob("*")):
            rel = f.relative_to(MAW).as_posix()
            if not f.is_file() or "__pycache__" in f.parts or rel.startswith(PIN_OWNERS):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            hits += [f"maw/{rel}:{i}" for i, line in enumerate(text.splitlines(), 1)
                     if "plugin-main" in line]
        self.assertEqual(hits, [], "derive the launcher from selfpath, not the operator's pin")

    def test_shipped_docs_name_no_pinned_checkout_and_no_unsubstituted_root(self) -> None:
        docs = shipped_docs() + [ROOT / "README.md"]
        self.assertGreaterEqual(len(docs), 8)
        hits = [f"{f.relative_to(ROOT)}:{i}: {needle}"
                for f in docs
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
                # A doc is not a command body: nothing substitutes the variable
                # there, and a seat's Bash call does not have it either.
                for needle in ("plugin-main", "CLAUDE_PLUGIN_ROOT")
                if needle in line]
        self.assertEqual(hits, [])


class HintsFromACacheCopyTests(unittest.TestCase):
    """The falsifier: copy maw/ ALONE somewhere else, give it an empty HOME,
    and every hint it prints names a launcher inside that copy that exists."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.copy = base / "cache" / "maw" / "maw" / "0.0.0"
        shutil.copytree(MAW, self.copy, ignore=shutil.ignore_patterns("__pycache__"))
        self.home = base / "home"
        self.home.mkdir()
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home),
                    "PYTHONPATH": str(self.copy), "PYTHONDONTWRITEBYTECODE": "1"}

    def probe(self, plugin_dir: Path) -> dict:
        env = {**self.env, "PYTHONPATH": str(plugin_dir)}
        r = subprocess.run([sys.executable, "-c", PROBE], cwd=self.home, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_every_hint_names_a_launcher_that_exists_in_the_copy(self) -> None:
        out = self.probe(self.copy)
        launcher = self.copy / "bin" / "maw-run"
        self.assertEqual(out["root"], str(self.copy))
        self.assertEqual(out["launcher"], str(launcher))
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertFalse((self.home / ".maw").exists())
        for name, text in out["hints"].items():
            with self.subTest(name):
                self.assertIn(f"{launcher} ", text)
                self.assertNotIn("plugin-main", text)
                self.assertNotIn("$HOME", text)

    def test_a_symlinked_root_prints_the_real_path(self) -> None:
        link = Path(self.tmp.name).resolve() / "link"
        link.symlink_to(self.copy)
        self.assertEqual(self.probe(link)["launcher"], str(self.copy / "bin" / "maw-run"))

    def test_the_printed_launcher_runs_a_module_from_the_copy(self) -> None:
        out = self.probe(self.copy)
        r = subprocess.run(f"{out['launcher']} selfpath", shell=True, cwd=self.home,
                           env={k: v for k, v in self.env.items() if k != "PYTHONPATH"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, str(self.copy / "bin" / "maw-run")),
                         r.stderr[-2000:])

    def test_a_root_with_a_space_is_quoted(self) -> None:
        spaced = Path(self.tmp.name).resolve() / "a b" / "maw"
        shutil.copytree(self.copy, spaced)
        out = self.probe(spaced)
        self.assertEqual(out["launcher"], f"'{spaced}/bin/maw-run'")
        r = subprocess.run(f"{out['launcher']} selfpath", shell=True, cwd=self.home,
                           env={k: v for k, v in self.env.items() if k != "PYTHONPATH"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

    def test_the_watchdog_hook_names_the_copys_launcher(self) -> None:
        for env_root in (str(self.copy), None):
            with self.subTest(env_root=env_root):
                env = {**self.env, "MAW_BRIDGE_ROLE": "executor"}
                if env_root:
                    env["CLAUDE_PLUGIN_ROOT"] = env_root
                r = subprocess.run(["sh", str(self.copy / "hooks" / "watchdog-arm.sh")],
                                   cwd=self.home, env=env, capture_output=True, text=True,
                                   timeout=30)
                self.assertEqual(r.returncode, 0)
                self.assertIn(f"{self.copy}/bin/maw-run net.watch", r.stdout)
                self.assertNotIn("plugin-main", r.stdout)

    def test_the_watchdog_hook_quotes_a_root_a_shell_would_split(self) -> None:
        """The seat pastes the printed command. Run each one in a shell."""
        import re
        spaced = Path(self.tmp.name).resolve() / "it's a dir" / "maw"
        shutil.copytree(self.copy, spaced)
        for env_root in (str(spaced), None):
            with self.subTest(env_root=env_root):
                env = {**self.env, "MAW_BRIDGE_ROLE": "executor"}
                if env_root:
                    env["CLAUDE_PLUGIN_ROOT"] = env_root
                r = subprocess.run(["sh", str(spaced / "hooks" / "watchdog-arm.sh")],
                                   cwd=self.home, env=env, capture_output=True, text=True,
                                   timeout=30)
                self.assertEqual(r.returncode, 0)
                commands = re.findall(r"`([^`]*maw-run[^`]*)`", r.stdout)
                self.assertGreaterEqual(len(commands), 1, r.stdout)
                for cmd in commands:
                    # Swap the module for selfpath: it prints the launcher and
                    # proves the quoted path is one word that resolves to this copy.
                    launcher = cmd[:cmd.index("maw-run") + len("maw-run")]
                    launcher += "'" if launcher.startswith("'") else ""
                    run = subprocess.run(f"{launcher} selfpath", shell=True, cwd=self.home,
                                         env={k: v for k, v in self.env.items() if k != "PYTHONPATH"},
                                         capture_output=True, text=True, timeout=60)
                    self.assertEqual(run.returncode, 0, (cmd, run.stderr[-500:]))
                    self.assertIn(str(spaced / "bin" / "maw-run"), run.stdout.replace("'\"'\"'", "'"))


if __name__ == "__main__":
    unittest.main()
