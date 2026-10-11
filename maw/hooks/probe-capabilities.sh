#!/usr/bin/env bash
# Capability probe gate (SessionStart) — Phase 1c-part-3.
#
# Deferred tools (CronCreate, PushNotification, TeamCreate, RemoteTrigger)
# can only be invoked by Claude in-session, so the probe itself is prose.
# This hook keeps everything around the prose deterministic: init.probe's
# gate decides (config present? snapshot missing/stale/malformed? forced
# via MAW_REPROBE=1?) and prints the probe instructions on stdout ONLY
# when a probe is due. SessionStart stdout is injected as session context,
# so a due probe becomes an instruction Claude sees at start; a fresh
# snapshot costs nothing — no output, no tokens.
#
# Like every maw hook: NEVER blocks a session start; all error paths exit 0.

# No `set -u`: an unset CLAUDE_PLUGIN_ROOT would abort at expansion time
# with a non-zero exit, BEFORE any `|| exit 0` guard — breaking the
# never-block guarantee. Default-empty expansion + explicit check instead.

# Only fires in repos that carry the substrate.
[ -f ".maw/config.yaml" ] || exit 0

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[ -n "$PLUGIN_ROOT" ] || exit 0

"$PLUGIN_ROOT/bin/maw-run" init.probe gate \
  --repo-root "$PWD" --plugin-root "$PLUGIN_ROOT" 2>/dev/null || exit 0

exit 0
