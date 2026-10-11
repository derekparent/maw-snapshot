#!/bin/sh
# Tools-awareness gate (SessionStart) — surface the two on-demand tools.
#
# MCP tools are frequently deferred / loaded-on-demand, so an agent does NOT
# "know" it holds them until it tries (and often won't try). This hook closes
# that gap by injecting a short, ACTIONABLE awareness line at session start:
#
#   - computer-use  — the agent's visual EYES: screenshot + verify RENDERED UI.
#                     Timing rule (refit R2, 2026-09-23): headless simctl /
#                     XCUITest by default; computer-use only when needed, and
#                     requested in the FIRST turn while the operator is arming, because
#                     its per-session app approval cannot be answered once he
#                     has left.
#                     This is the reviewer's INDEPENDENT check for the
#                     non-reproducible-UI gap (the ADVISOR-PLAYBOOK
#                     non-reproducible-oracle gate): the
#                     advisor can SEE the app itself rather than signing a UI
#                     verdict on the builder's screenshots.
#   - Xcode MCP     — the iOS builder's tool: build/run the app. Emitted ONLY
#                     in an iOS/Xcode-shaped repo (stack-aware), to avoid noise
#                     in non-iOS repos.
#
# Kept deliberately SEPARATE from probe-capabilities.sh: that hook gates on a
# TTL'd snapshot and is about deferred-tool PROBING; this one is a stateless,
# always-on reminder. Entangling them would couple two different lifecycles.
#
# Like every maw hook: it NEVER blocks or fails a session start. POSIX sh,
# everything wrapped, every path exits 0. No `set -e`/`set -u` — an unset var
# must not abort before a guard runs.

# --- computer-use line: ALWAYS emitted (it is the universal reviewer eyes) ---
printf '%s\n' "[maw-tools] Visual checks: default to HEADLESS — drive the app from a script, a test or its CLI and capture what it shows. No approval prompt, so it works after the operator leaves. **computer-use** (your visual eyes) only when headless cannot show it, and then call request_access in your FIRST turn, while the operator is present, so they approve it once. Its app approval is per session: it cannot be pre-approved, Remote Control does not forward it, and bypass mode does not skip it. After the operator has left, a visual check is a PARKED lane, never a prompt you sit on. The reviewer's own eyes are still the non-reproducible-oracle review gate (see ADVISOR-PLAYBOOK.md). If computer-use is unresponsive, enable Accessibility + Screen-Recording grants for the host app." 2>/dev/null

# --- Xcode MCP line: only in an iOS/Xcode env (stack-aware) ---
# Degrade gracefully: if the stack check can't run (no plugin root, python
# missing, import error), we simply skip the Xcode line — the computer-use
# line above already shipped, so the session is never left empty-handed.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -n "$PLUGIN_ROOT" ]; then
  is_xcode=$(PYTHONPATH="$PLUGIN_ROOT" python3 -c '
import sys
from pathlib import Path
try:
    from init.stack_detection import is_xcode_env
    print("yes" if is_xcode_env(Path(".")) else "no")
except Exception:
    print("skip")
' 2>/dev/null) || is_xcode="skip"

  if [ "$is_xcode" = "yes" ]; then
    printf '%s\n' "[maw-tools] iOS/Xcode repo detected — also: **Xcode MCP** — use it to build/run the iOS app; if it is not connected, connect it via /mcp. Headless on iOS: \`xcrun simctl launch\` with launch args to force a state, \`xcrun simctl io booted screenshot\` to capture it, XCUITest for taps." 2>/dev/null
  fi
fi

exit 0
