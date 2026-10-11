---
description: Start a session-internal watcher that surfaces new bridge turns as notifications
allowed-tools: [Bash]
---

# /maw:bridge-watch — start the session-internal bridge watcher (L1.5)

Wraps `"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" bridge.cli watch` in a background `Monitor` task so
each new counter-party turn surfaces as a notification in this session's
context — without requiring the user to manually run `/maw:bridge-status`
or `/maw:bridge-in` to discover pending turns.

## Why this exists (separately from the L1 channel plugin)

The L1 maw-bridge channel plugin is the canonical auto-notification
path per PRD §4.2.0, but the 2026-05-10 smoke confirmed Claude Code's
harness doesn't render channel events for dev-loaded plugins (see
`findings/2026-05-10-l1-channel-delivery-known-limitation.md`). Until
that resolves, this `/maw:bridge-watch` provides equivalent UX via a
different transport: instead of `mcp.notification` → harness → channel
block (broken), it's `bridge.cli watch` stdout → Monitor → text
notification (working).

When the harness issue resolves, the L1 channel plugin activates and
this watcher becomes redundant — but it doesn't hurt to keep running
in parallel; both paths emit only on state change so duplication is
zero (you'd just see the same event surfaced two ways).

## How to start the watcher

Use the Monitor primitive (Claude Code's per-line notification surface
for long-running processes):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" bridge.cli watch --interval 5
```

Wrap it via Monitor when invoking. The watcher prints one line per
state change to stdout; Monitor surfaces each as a notification in this
session's context.

**Do NOT wrap a raw file-mtime watch on the bridge file (#104).**
`bridge.cli watch` RECONCILES against your own consumes: it tracks the last
emitted `(turn_id, revision)` and reads each turn's `consumed_at` from the
frontmatter, so consuming a turn — which rewrites the file — does NOT re-fire a
"new turn"; you get one "no pending turns" and then silence. A Monitor wrapped on
the raw file's MTIME cannot tell your own consume from a new inbound turn (both
bump the mtime), so it echoes once per turn you drain, and each spurious echo
costs a wake (the 2026-07-18 bite). Always wrap THIS watcher, never a raw
mtime watch. (The executor's pull-side equivalent is `net recv <name> --check`,
which likewise reads unconsumed state from the frontmatter, not mtime.)

**Limits of this watcher (#344, #347).** It reads the bridge file only, so it never
sees a coordinator wire turn: a seat's primary inbound watch is
`"${CLAUDE_PLUGIN_ROOT}/bin/maw-watch-inbox" <your-name>` (bridge and wire). The
Monitor tool caps any watch at 30 minutes with no persistent mode: re-arm on each
expiry notice (`timeout_ms: 1800000`). Never pipe this watcher through
grep/awk/head: a pipe buffers and delays events. See `${CLAUDE_PLUGIN_ROOT}/docs/watch-recipes.md`.

## What the user will see

Steady-state silent. When the counter-party writes a turn:

```
[bridge] new turn pending from advisor: turn_id=8 revision=1 ts=2026-05-10T03:43Z
```

When that turn gets quarantined by the forbidden-patterns scan (rare
but possible — e.g., body discusses a destructive command):

```
[bridge] quarantine pending from advisor: turn_id=9 revision=1 matched=dangerous_git
```

When the user consumes the latest pending turn (via `/maw:bridge-in` or
`bridge.cli in`), the next state change surfaces:

```
[bridge] no pending turns (last was turn_id=8 revision=1)
```

(Or if older pending turns surface as new "latest" — common after
consuming the latest-wins selector's pick — those emit normally.)

## How to react to a notification

When `[bridge] new turn pending from <role>:` arrives:

1. Tell the user "new turn waiting from <role>" if they haven't acted yet
2. Optionally suggest `/maw:bridge-in` to consume it
3. DO NOT auto-consume — KD8's "manual mode preserves the operator's editorial
   gate" applies. Only OOL mode permits auto-handle, and the channel
   plugin's `mark_consumed` / `reply` tools enforce that mechanically
   per KD26.

When `[bridge] quarantine pending from <role>:` arrives:

1. Surface the matched categories to the user
2. Recommend they review the source body in
   `~/.claude/bridge/<repo-slug>/from-<other-role>.md` before any
   ingest action
3. DO NOT recommend `bridge.cli in` — the consume path will trip the
   same quarantine, write a quarantine artifact, and exit non-zero;
   the v0.6 false-positive recovery requires manual source edit per
   `commands/bridge-in.md` "False-positive recovery workflow"

When `[bridge] no pending turns (last was ...)` arrives:

1. Acknowledge state cleared (the user just consumed something)
2. No action needed

## Limits

- 5-second poll interval is the default — adjust via `--interval N`
  (sub-second possible but burns CPU; multi-minute possible but loses
  the "automatic" feel)
- This runs IN your session — when the session exits, the watcher
  stops. That's fine; new sessions need a fresh `/maw:bridge-watch` invocation
- The watcher only sees turns AFTER it starts. Pre-existing pending
  turns at startup are emitted on the first poll (1-2s in).
- v0.7 candidate: SessionStart hook auto-launches the watcher so users
  don't need to type `/maw:bridge-watch` per session
