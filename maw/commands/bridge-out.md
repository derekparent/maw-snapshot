---
description: Write a turn to the bridge from this session
argument-hint: <message body>
allowed-tools: [Bash, Write, PushNotification]
---

# /maw:bridge-out — write a turn to the bridge

The user wants to send `$ARGUMENTS` as a turn from this session to the
counter-party (advisor → executor or vice versa).

## How to ingest the user's message

The CLI lives inside the maw plugin install dir. Reach it by injecting
`PYTHONPATH=${CLAUDE_PLUGIN_ROOT}` so `bridge.cli` is importable from the
user's cwd (which is typically the target repo, not the maw repo).

If `$ARGUMENTS` is a single-line, simple message, pass it directly via the
`--message` flag:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" bridge.cli out --message "$ARGUMENTS"
```

If `$ARGUMENTS` is multi-line, contains shell metacharacters (`$`, `` ` ``,
unbalanced quotes), or is long enough that command-line escaping would be
fragile, write the message to a tempfile first and use `--from-file`:

```bash
TMP=$(mktemp -t bridge-out.XXXXXX.md)
# (write $ARGUMENTS to $TMP using the Write tool, NOT a heredoc — heredocs
# trip on the same metacharacters that motivate the tempfile path)
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" bridge.cli out --from-file "$TMP"
rm -f "$TMP"
```

## Role + repo resolution

The CLI resolves `role` and `repo_root` from environment variables set by
the launching shell (`MAW_BRIDGE_ROLE`, `MAW_BRIDGE_REPO_ROOT`) with
fallbacks to `<repo>/.maw/role` and cwd. You should not pass `--role`
unless the user explicitly asked you to override.

## On error

If the CLI exits non-zero, print stderr verbatim to the user and stop —
do not retry silently. Common errors: empty body, oversized body
(>64KB), no existing turn to revise.

## Reporting back + PushNotification

Print the CLI's stdout to the user (it confirms `wrote turn N (rev M)`).
That's the user-visible output for this command.

After a successful write (CLI exit 0), fire a `PushNotification` so the
counter-party session is alerted out-of-band per PRD §4.2.1 step 5:

```
PushNotification(message: "[<role>] wrote turn <N> (rev <M>) to bridge")
```

Substitute `<role>`, `<N>`, `<M>` from the CLI's output line. If the
`PushNotification` tool is not available (capability detection per
PRD §4.1.1.1), skip silently — the bridge write itself was successful
regardless of notification. Don't fail the command on a missing
notification primitive.

## Important: scan happens at /maw:bridge-in, not /maw:bridge-out

`/maw:bridge-out` does NOT run the `bridge_content_forbidden_patterns`
scan. Confirmed empirically in the 2026-05-09 Phase 2a smoke (Round 3,
Test 3) — the scanner is on the read side. Architectural choice per PRD
§4.2.0: writers can't be trusted to self-police; readers protect
themselves before injecting body content into Claude context.

Implication for users: a `/maw:bridge-out` succeeding does NOT mean the
body will pass the counter-party's `/maw:bridge-in`. If the user is sending
a body that mentions destructive commands or matches another forbidden
category, expect a quarantine on the receiving side. Surface this
expectation proactively when the user writes such a body — particularly
if they're explaining "don't do X" in a body that contains a literal
example of X.

For known false-positive shapes (legitimate body text that the scanner
will trip on), suggest the user rewrite the body to avoid the literal
pattern, or accept the quarantine + manual-source-edit recovery
workflow documented in `bridge-in.md`.

## Body size cap (>64KB rejected at write time)

The CLI rejects bodies > 65536 bytes (64 KiB, NOT 64 KB) at write time
with `BridgeSizeExceededError`. The error message references an
`attached_file:` mechanism for larger payloads — that mechanism is
**not yet implemented in v0.6** (flagged for v0.6 doc batch). For v0.6,
the user must either trim the body or split it across multiple turns.
