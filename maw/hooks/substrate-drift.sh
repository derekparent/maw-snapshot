#!/usr/bin/env bash
# Substrate freshness warning (SessionStart) — #201.
#
# A repo's substrate is transplanted once and then sits. When doctrine moves in
# the plugin, the repo does not: an iOS repo's pair ground on June-era text
# for a week (the operator escalated 2026-07-23), and nothing in the system said "you are
# behind" — the stale text just read as business-as-usual.
#
# This prints ONE loud line when the repo's transplant predates the last
# breaking doctrine change, so a seat sees it before its first /maw:goal rather than
# after complying with a retired ritual.
#
# NEVER blocks a session start: every path exits 0, and a repo with no substrate
# (or no stamp comparison to make) prints nothing at all.

set -u

repo="${MAW_BRIDGE_REPO_ROOT:-$PWD}"
[ -f "$repo/.maw/state.json" ] || exit 0

out=$("${CLAUDE_PLUGIN_ROOT}/bin/maw-run" init.substrate \
  --repo-root "$repo" 2>/dev/null) || exit 0
[ -n "$out" ] && echo "$out"
exit 0
