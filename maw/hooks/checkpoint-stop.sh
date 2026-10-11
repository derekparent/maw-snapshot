#!/usr/bin/env bash
# Stop hook — auto-WIP checkpoint (backlog item 7a). OPT-IN: not wired in
# hooks.json by default (it runs on every turn end; the operator opts in after reviewing
# the blast radius — see docs/succession-hardening.md). Snapshots the dirty tree
# to refs/maw-wip/<seat> so a crash leaves recoverable state. Fail-open: never
# blocks the stop, never disturbs the working tree.
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" succession.checkpoint \
  --seat "${MAW_BRIDGE_ROLE:-}" >/dev/null 2>&1 || true
exit 0
