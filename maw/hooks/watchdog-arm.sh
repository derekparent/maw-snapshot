#!/bin/sh
# Supervision-watchdog arming (SessionStart) — the no-habit guarantee for F10.
#
# The 2026-07-21 stall (issue #149/F10): a seat stalled ~8h, machine-
# visible via net.watch, and BOTH supervision layers missed it because CONSUMING
# the sweep was optional habit. This hook removes the habit: every paired seat is
# TOLD, at session start, to arm a Monitor over its pair's net.watch sweep, so a
# peer's stall WAKES it instead of sitting in a log.
#
# A hook cannot START the Monitor (it runs a shell command that exits; a Monitor
# is an agent-invoked harness primitive) — so it INJECTS the mandatory arming
# instruction. The seat command documents the exact loop; this hook is the
# always-on, un-skippable reminder that closes the "seat never ran its command"
# residual. Layered with: the seat REPORT must state watcher-armed:yes, and the
# CoS fleet sweep is the fleet-level backstop (the CoS half). Recorded residual:
# if both seats skip arming AND the CoS is down, nothing sweeps — accepted,
# because the alternative is an unowned background daemon (the orphan-process
# class the fleet learnings already paid for).
#
# Role-gated: only the bridge pair (advisor/executor) supervises a peer; a
# non-paired session (cos, a bare session) gets nothing. Fail-open: POSIX sh,
# every path exits 0 — an arming reminder must never block a session start.

ROLE="${MAW_NET_NAME:-${MAW_BRIDGE_ROLE:-}}"
# The launcher of THIS plugin copy: a marketplace install has no pinned worktree.
MAW_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)}"
MAW_RUN="${MAW_ROOT}/bin/maw-run"
# The seat pastes this into a shell, so a path with a space or a quote is
# single-quoted, as selfpath does for the Python hints. A plain path stays bare.
case "$MAW_RUN" in
  *[!A-Za-z0-9_@%+=:,./-]*)
    MAW_RUN="'$(printf '%s' "$MAW_RUN" | sed "s/'/'\\\\''/g")'"
    ;;
esac

case "$ROLE" in
  advisor*|executor*)
    printf '%s\n' "[maw-watchdog] Arm the pair-sweep supervision watchdog NOW, before your first /maw:goal — this is NOT optional habit (it is the exact gap that let the 2026-07-21 8h stall pass BOTH supervision layers, #149/F10). Wrap in a Monitor (timeout_ms 1800000: the tool caps a watch at 30 minutes and has no persistent mode, so RE-ARM it on every expiry notice; see ${MAW_ROOT}/docs/watch-recipes.md) a 45-second loop that runs \`${MAW_RUN} net.watch --for ${ROLE} --wake\` (one sweep: header + detail of NEW findings only; the seen-set survives a re-arm) then \`net.deadman check --for ${ROLE}\`: when it surfaces a finding (stuck-while-working / blocked-seat / dark-session / deadlock) for your pair, that is a WAKE. On wake, do the artifact-grounded check YOURSELF before acting on or relaying the peer's self-report: read the peer's pane (herdr agent_status + fresh tool output / active thinking) — if it shows real work, AUTO-ACK (record the pane evidence, do not escalate); only a pane that ALSO looks wedged leaves the finding for the dead-man page (lane 3). A blocked-seat pane holds a prompt that may be the operator's own: never answer another seat's prompt or press its options — page the operator. Watch the WORK, not the words. Report \`watcher-armed: yes\` in your seat status." 2>/dev/null
    ;;
  *)
    : # non-paired session — no peer to supervise, nothing to arm
    ;;
esac

exit 0
