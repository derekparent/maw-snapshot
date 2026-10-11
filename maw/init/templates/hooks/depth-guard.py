#!/usr/bin/env python3
"""depth-guard.py — PreToolUse on `Task|Agent`. Per PRD §4.3 + D24.

Refuses Task/Agent spawn if MAW_DEPTH >= 3. Structural enforcement
(env var passes through every spawned shell), not LLM-compliance.

Exit:
  0 = allow
  2 = block (depth limit hit)
"""
from __future__ import annotations

import json
import os
import sys

MAX_DEPTH = 3


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    if event.get("tool_name") not in ("Task", "Agent"):
        return 0

    try:
        depth = int(os.environ.get("MAW_DEPTH", "0"))
    except ValueError:
        depth = 0

    if depth >= MAX_DEPTH:
        sys.stderr.write(
            f"depth-guard: BLOCKED — MAW_DEPTH={depth} ≥ {MAX_DEPTH}; refuse to spawn another agent layer.\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
