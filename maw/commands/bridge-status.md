---
description: Show last 5 turns each direction with consumed/pending state
allowed-tools: [Bash]
---

# /maw:bridge-status — bridge state at a glance

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" bridge.cli status
```

Prints:
- Resolved `role`, `repo_root`, `repo_slug`, `bridge_dir`
- Last 5 turns in `from-<my-role>.md` (turns this session has written)
- Last 5 turns in `from-<other-role>.md` (turns the counter-party has written)
- Each turn shows: `turn_id`, `revision`, `ts`, `[consumed]` or `[pending]`,
  and a 60-char body preview. Bodies are scanned first (whole body, read-only):
  a quarantined turn shows `[quarantined]`, a body that would trip the
  forbidden-pattern scan shows `[would quarantine: N pattern(s)]`, and if the
  pattern registry cannot load no preview is shown at all. Never raw text.

Use this when the user asks "what's on the bridge?" or "did they reply
yet?" or "what was that turn from earlier?". Don't paraphrase the
output; the user wants the literal state.

## Drain pattern — count `[pending]` lines before `/maw:bridge-in`

Per `bridge-in.md`'s "Selector semantic" section, `/maw:bridge-in` returns
ONE turn per call (latest unconsumed `turn_id` wins). If status shows
multiple `[pending]` lines in the counter-party file, the user needs to
run `/maw:bridge-in` once per pending turn to drain — not just once.

When the user is catching up after a stretch of inactivity, suggest
they `/maw:bridge-status` first to count what's pending in their inbox,
then drain via repeated `/maw:bridge-in`. This avoids the "I ran
/maw:bridge-in once and got the latest, where are turns 4-6?" surprise that
the 2026-05-09 Phase 2a smoke (Round 3, Test 1) surfaced.
