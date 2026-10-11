#!/usr/bin/env bash
# Switchboard auto-registration (SessionStart).
#
# Registers this session in the net switchboard so peers can discover it
# (`net who`) and push to it (`net send`). Only sessions that declare an
# identity register — an anonymous session would just clutter the registry,
# and a name with no owner is useless for the wake line.
#
# Identity precedence: MAW_NET_NAME > MAW_BRIDGE_ROLE. The operator's launch aliases
# already set MAW_BRIDGE_ROLE (executor/advisor), so the bridge pair
# auto-registers for free; MAW_NET_NAME overrides when a session wants a
# distinct switchboard name.
#
# This hook NEVER blocks or fails a session start: every error path exits 0.
# Its one line of stdout is injected into the session as context so the
# session knows whether it is wakeable.

set -u

name="${MAW_NET_NAME:-${MAW_BRIDGE_ROLE:-}}"
if [ -z "$name" ]; then
  # Anonymous session — nothing to register. Silent, non-fatal.
  exit 0
fi

role="${MAW_BRIDGE_ROLE:-$name}"
repo="${MAW_BRIDGE_REPO_ROOT:-$PWD}"

# The SessionStart payload carries this session's transcript path. Pin it on the
# registry entry: net.liveness stats THAT file as the seat's proof of life,
# because a project dir also holds transcripts for headless children and rotated
# seats — "newest file in the dir" lets a sibling vouch for a wedged seat. Every
# failure path here leaves it empty (the weaker dir-max basis), never fatal.
payload=$(cat 2>/dev/null || true)
transcript=$(printf '%s' "$payload" | python3 -c \
  'import json,sys
try:
    print(json.load(sys.stdin).get("transcript_path") or "")
except Exception:
    print("")' 2>/dev/null || true)

out=$("${CLAUDE_PLUGIN_ROOT}/bin/maw-run" net.cli register \
  --name "$name" --role "$role" --repo-root "$repo" \
  --transcript-path "$transcript" 2>&1) || {
  case "$out" in
    *"held by live pid"*)
      # Cross-repo collision: the short name (usually MAW_BRIDGE_ROLE) is
      # owned by a live session in another repo. Self-qualify with this
      # repo's basename instead of failing dark — the registry refuses
      # the collision; the hook resolves it visibly.
      # Sanitize the repo basename to fit NAME_RE (lowercase alnum/dash/
      # underscore) — an uppercase or dotted dir name would otherwise make
      # the retry fail validation and skip registration.
      slug=$(basename "$repo" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-' | sed 's/-*$//')
      qualified="${name}-${slug}"
      out2=$("${CLAUDE_PLUGIN_ROOT}/bin/maw-run" net.cli register \
        --name "$qualified" --role "$role" --repo-root "$repo" \
        --transcript-path "$transcript" 2>&1) || {
        echo "[maw-net] auto-register skipped: ${out2:-unknown error}"
        exit 0
      }
      echo "[maw-net] name '$name' taken by another live session — registered as '$qualified'. $out2"
      exit 0
      ;;
  esac
  echo "[maw-net] auto-register skipped: ${out:-unknown error}"
  exit 0
}

echo "[maw-net] $out"
exit 0
