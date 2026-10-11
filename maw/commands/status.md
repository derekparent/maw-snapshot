---
description: Show maw substrate, capability and bridge state for the current repository
argument-hint: ""
allowed-tools: [Bash]
---

# /maw:status — substrate observability for the current repository

Run the CLI (PYTHONPATH injects the maw plugin install dir so `init.status`
is importable from the user's cwd):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" init.status
```

## What this does

Read-only report over the substrate's on-disk state, five lines:

1. **Substrate** — install state from `.maw/state.json`: version, profile,
   tracked file count, pending `.maw-recommended` drift siblings.
2. **Capabilities** — the probed capability snapshot (same renderer as the
   `/maw:init` banner). `deferred-probe` means the SessionStart probe has
   not run yet; `[stale]` means it will re-run at the next session start.
3. **Bridge** — repo slug and bridge directory presence. Turn-level detail
   lives in `/maw:bridge-status`, not here.
4. **Sensors** — whether the optional sensor client is installed and on. A
   plain install does not include it and prints `not installed`.
5. **Merge posture** — echo-required vs no-echo for this repo (from
   `guards.session_auth`'s own classifier), whether the repo's advisor is
   armed, the arm's scope, and when the token expires. Read-only: it never
   arms. A broken read prints `unknown (reason)` and status still exits 0.

## Surface the CLI output verbatim

Print stdout exactly as rendered. The command always exits 0 — a repo with
no substrate reports "not installed", which is a valid status, not an error.
