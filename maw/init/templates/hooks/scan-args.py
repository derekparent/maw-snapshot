#!/usr/bin/env python3
"""scan-args.py — PreToolUse on `Bash`. Per PRD §4.5 layer 3.

Blocks dangerous flag patterns that the permission grammar (Bash(verb:*))
cannot express. Active only when MAW_AGENT=1 (the human operator can use
these flags from a non-agent session).

Blocks:
  - --no-verify  (commit/push hook bypass)
  - --no-gpg-sign  (signing bypass)
  - git push --force ... main  (branch protection bypass)
  - git push -f ... main
  - git push +<ref>:main  (force push via plus syntax)

Exit:
  0 = allow
  2 = block (with stderr explanation)
"""
from __future__ import annotations

import json
import os
import re
import sys


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    if event.get("tool_name") != "Bash":
        return 0

    if os.environ.get("MAW_AGENT") != "1":
        return 0

    cmd = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str):
        return 0

    # 1. --no-verify / --no-gpg-sign anywhere
    if re.search(r"(?<![A-Za-z0-9_-])--no-verify(?![A-Za-z0-9_-])", cmd):
        sys.stderr.write("scan-args: BLOCKED — --no-verify is forbidden under MAW_AGENT=1.\n")
        return 2
    if re.search(r"(?<![A-Za-z0-9_-])--no-gpg-sign(?![A-Za-z0-9_-])", cmd):
        sys.stderr.write("scan-args: BLOCKED — --no-gpg-sign is forbidden under MAW_AGENT=1.\n")
        return 2

    # 2. git push --force (or -f) targeting main/master
    push_force_main = re.search(
        r"git\s+(?:-c\s+\S+\s+)*push\s+(?:[^|;&\n]*\s)?(?:--force|--force-with-lease|-f)\s+[^|;&\n]*\b(?:main|master)\b",
        cmd,
    )
    if push_force_main:
        sys.stderr.write(
            "scan-args: BLOCKED — git push --force to main/master is forbidden under MAW_AGENT=1.\n"
        )
        return 2

    # 3. git push +<ref>:main (force push via plus prefix)
    push_plus_main = re.search(
        r"git\s+(?:-c\s+\S+\s+)*push\s+\S+\s+\+\S+:(?:refs/heads/)?(?:main|master)\b",
        cmd,
    )
    if push_plus_main:
        sys.stderr.write(
            "scan-args: BLOCKED — git push +<ref>:main (force push) is forbidden under MAW_AGENT=1.\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
