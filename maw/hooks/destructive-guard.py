#!/usr/bin/env python3
"""PreToolUse(Bash) hook — structural destructive-op guard (backlog item 4).

Thin wrapper: reads the PreToolUse event from stdin, delegates classification to
guards.destructive.evaluate, prints any message to stderr, and exits with its
code (0 allow, 2 block). FAILS OPEN on any error — this runs before every Bash
command and must never break the shell because the guard itself misbehaved —
with one exception: if evaluate() itself raises on a command that VISIBLY runs a
gated git op, the hook fails CLOSED (blocks), so a guard bug is never a bypass.

The override still works on that fail-closed path, with the same two sources as
evaluate(): MAW_ALLOW_DESTRUCTIVE=1 in the session env, or set by the command
itself (`MAW_ALLOW_DESTRUCTIVE=1 git ...`, `export ...; git ...`). If reading
the command for the inline form also raises, only the env counts: an error never
widens the override beyond what the parser confirmed. MAW_ALLOW_BASE_DELETE is
not consulted here; the block message names MAW_ALLOW_DESTRUCTIVE, and that one
override clears every error-path block.
"""
from __future__ import annotations

import json
import os
import sys

# Resolve the plugin root so `guards` is importable. CLAUDE_PLUGIN_ROOT is set
# for plugin hooks; fall back to this file's grandparent (the plugin root).
_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if _root not in sys.path:
    sys.path.insert(0, _root)


def _override_set(cmd: str) -> bool:
    if os.environ.get("MAW_ALLOW_DESTRUCTIVE") == "1":
        return True
    try:
        from guards.destructive import _command_sets
        return bool(_command_sets(cmd, "MAW_ALLOW_DESTRUCTIVE"))
    except Exception:
        return False


def main() -> int:
    try:
        from guards.destructive import evaluate
    except Exception:
        return 0  # guard can't load → allow (fail-open)
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        decision = evaluate(event, env=dict(os.environ))
    except Exception as e:  # noqa: BLE001
        # An internal error allows an ordinary command (fail-open keeps a guard
        # bug from wedging every Bash call), but a command that visibly runs a
        # gated git op is blocked: an error must not become a bypass.
        from guards.destructive import BLOCK, looks_destructive
        cmd = ((event or {}).get("tool_input") or {}).get("command", "") \
            if isinstance(event, dict) else ""
        if looks_destructive(cmd) and not _override_set(cmd):
            sys.stderr.write(f"destructive-guard: BLOCKED — the guard failed ({e!r}) "
                             f"on a command that looks destructive; re-run with "
                             f"MAW_ALLOW_DESTRUCTIVE=1 if you mean it\n")
            return BLOCK
        return 0
    if decision.message:
        sys.stderr.write(decision.message + "\n")
    return decision.exit_code


if __name__ == "__main__":
    sys.exit(main())
