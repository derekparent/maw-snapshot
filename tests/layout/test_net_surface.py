"""Layout smoke: the switchboard plugin surface (hooks.json + /maw:net command + hook script).

The Python suite is blind to the plugin loader's surface — a malformed
hooks.json or a hook command pointing at a missing/non-executable script fails
only at session-start time, not in unit tests. This covers that class for the
net (switchboard) additions, in the spirit of test_plugin_manifest.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "maw"
HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"
NET_REGISTER = PLUGIN_ROOT / "hooks" / "net-register.sh"
NET_HEARTBEAT = PLUGIN_ROOT / "hooks" / "net-heartbeat.sh"
NET_COMMAND = PLUGIN_ROOT / "commands" / "net.md"


class NetPluginSurfaceTests(unittest.TestCase):
    def test_hooks_json_parses(self) -> None:
        self.assertTrue(HOOKS_JSON.is_file(), f"{HOOKS_JSON} missing")
        with HOOKS_JSON.open() as f:
            data = json.load(f)
        self.assertIn("hooks", data)
        self.assertIn("SessionStart", data["hooks"])

    def test_sessionstart_references_existing_executable_script(self) -> None:
        with HOOKS_JSON.open() as f:
            data = json.load(f)
        entries = data["hooks"]["SessionStart"]
        commands = [
            h["command"]
            for group in entries
            for h in group.get("hooks", [])
            if h.get("type") == "command"
        ]
        self.assertTrue(commands, "no command hooks declared for SessionStart")
        # The declared command must reference the net-register script by name.
        self.assertTrue(
            any("net-register.sh" in c for c in commands),
            f"SessionStart commands do not reference net-register.sh: {commands}",
        )
        self.assertTrue(NET_REGISTER.is_file(), f"{NET_REGISTER} missing")
        self.assertTrue(
            os.access(NET_REGISTER, os.X_OK),
            f"{NET_REGISTER} is not executable (chmod +x)",
        )

    def test_userpromptsubmit_heartbeat_registered_and_executable(self) -> None:
        # last_seen must mean "last conversational turn" or net.watch's
        # dark-session detection pages on every healthy-but-quiet session.
        with HOOKS_JSON.open() as f:
            data = json.load(f)
        entries = data["hooks"].get("UserPromptSubmit", [])
        commands = [
            h["command"]
            for group in entries
            for h in group.get("hooks", [])
            if h.get("type") == "command"
        ]
        self.assertTrue(
            any("net-heartbeat.sh" in c for c in commands),
            f"UserPromptSubmit commands do not reference net-heartbeat.sh: {commands}",
        )
        self.assertTrue(NET_HEARTBEAT.is_file(), f"{NET_HEARTBEAT} missing")
        self.assertTrue(
            os.access(NET_HEARTBEAT, os.X_OK),
            f"{NET_HEARTBEAT} is not executable (chmod +x)",
        )

    def test_net_command_exists_with_frontmatter(self) -> None:
        self.assertTrue(NET_COMMAND.is_file(), f"{NET_COMMAND} missing")
        text = NET_COMMAND.read_text(encoding="utf-8")
        self.assertTrue(
            text.startswith("---"), "command file must open with YAML frontmatter"
        )
        self.assertIn("description:", text)
        self.assertIn("allowed-tools:", text)

    def test_hook_uses_plugin_root_placeholder(self) -> None:
        # ${CLAUDE_PLUGIN_ROOT} is how the hook finds net.cli regardless of
        # where the plugin is installed — a bare path would break on install.
        with HOOKS_JSON.open() as f:
            text = f.read()
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", text)

    def test_seat_commands_exist_with_frontmatter(self) -> None:
        # The advisor/executor seat commands make the pair reproducible
        # (WS3). Auto-discovered, so they only need valid frontmatter.
        for name in ("advisor", "executor"):
            path = PLUGIN_ROOT / "commands" / f"{name}.md"
            self.assertTrue(path.is_file(), f"{path} missing")
            text = path.read_text(encoding="utf-8")
            self.assertTrue(
                text.startswith("---"),
                f"{name}.md must open with YAML frontmatter",
            )
            self.assertIn("description:", text)
            self.assertIn("allowed-tools:", text)

    def test_seat_commands_reference_their_doctrine(self) -> None:
        # The seats are only reproducible if they point at the load-bearing
        # artifacts: advisor → the playbook; executor → the canonical runner.
        advisor = (PLUGIN_ROOT / "commands" / "advisor.md").read_text(encoding="utf-8")
        executor = (PLUGIN_ROOT / "commands" / "executor.md").read_text(encoding="utf-8")
        self.assertIn("ADVISOR-PLAYBOOK.md", advisor)
        self.assertIn("run_all.py", executor)

    def test_consultant_command_pins_its_posture(self) -> None:
        # #97: the consultant role is only safe if its posture is load-bearing
        # in the command itself — junior in authority, ask/pull-only,
        # build-nothing, never a verdict/merge/relay source.
        path = PLUGIN_ROOT / "commands" / "consultant.md"
        self.assertTrue(path.is_file(), f"{path} missing")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"), "consultant.md needs frontmatter")
        self.assertIn("description:", text)
        self.assertIn("allowed-tools:", text)
        for anchor in ("junior in authority", "Ask/pull-only", "Build-nothing",
                       "verdict source", "merge authority", "never relay"):
            self.assertIn(anchor, text, f"consultant posture missing: {anchor!r}")


if __name__ == "__main__":
    unittest.main()
