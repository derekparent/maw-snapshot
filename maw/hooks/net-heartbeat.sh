#!/usr/bin/env bash
# Switchboard heartbeat (UserPromptSubmit) — last_seen = last conversational turn.
#
# net.watch flags a registered session as dark when last_seen exceeds the
# threshold. Without this hook, last_seen only updates on *send*, so a
# healthy session that's thinking or idle pages the operator — and an alarm that's
# noisy in its first week gets muted, which un-closes the exact gap the
# watcher exists to close (advisor review of PR #7). Refreshing on every
# user prompt makes last_seen mean what --dark-min silently assumes.
#
# Same discipline as net-register.sh: NEVER blocks a prompt; every error
# path exits 0, no output.

name="${MAW_NET_NAME:-${MAW_BRIDGE_ROLE:-}}"
[ -n "$name" ] || exit 0
[ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || exit 0

"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" net.cli heartbeat "$name" \
  >/dev/null 2>&1 || true
exit 0
