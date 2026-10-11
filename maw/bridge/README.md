# Layer 2 — `/bridge`

Turn-boundary IPC between two cooperating Claude sessions via shared filesystem. Kills the advisor↔executor copy-paste loop while preserving the operator's editorial gate.

**Status (v0.7, hardened 2026-06-12)**: shipped. File-format module + KD26 gate +
slash commands + forbidden-patterns scan + quarantine + PushNotification all live.
The 2026-06-12 quality sweep added: a three-layer Unicode-evasion-resistant scan
(Cf-strip → NFKC → TR39-style confusables — NFKC alone is a FALSE fix), and atomic
turn-id mint + quarantine-clear under file locks (`.idlock` / `.quarantine.lock`).
L1 channel plugin lives in `maw-bridge/`.

See: PRD §4.2 (`docs/PRD-maw-v0.7.md`), `findings/2026-05-07-bridge-spike.md` (Phase 0), `findings/2026-05-08-2a-prerequisites.md` (architectural decisions), `findings/2026-05-09-2a-bridge-smoke.md` (Phase 2a smoke validation), `findings/2026-05-09-smoke-design-questions.md` (parked design decisions surfaced by smoke).

## Behaviors users should know (v0.6 doc batch — surfaced from 2026-05-09 smoke)

- **Selector semantic**: `bridge.cli in` returns the OLDEST unconsumed turn (`find_unconsumed_oldest`) by default — FIFO drains, no pending turns stranded behind newer ones. Pass `--latest` to opt into the older `find_unconsumed_latest` semantic. Use `bridge.cli status` to see how many turns are pending. (Q1 closeout, 2026-05-12.)
- **Scan-on-read**: `bridge_content_forbidden_patterns` scan runs at `bridge.cli in` time, NOT at `bridge.cli out`. A successful `out` does not mean the body will pass the counter-party's `in`. Architectural choice per PRD §4.2.0 — readers protect themselves before injecting body content into Claude context.
- **Quarantine recovery (v0.6)**: `bridge.cli quarantine clear --turn N` reverts the turn's `consumed_at` so the next `in` re-reads it, plus dumps the body to the quarantine attic for inspection. (Q2 closeout, 2026-05-12.) `--allow-quarantine-once <id>` (ingest-time bypass) remains a v0.7 candidate per PRD §11.
- **Body size cap**: 65536 bytes (64 KiB, NOT 64 KB) at write time. The error message names byte counts only; trim or split bodies above the cap. (Q3 closeout: the `attached_file:` reference is gone from the error message; contract is parked for v0.7 design, `findings/2026-05-09-smoke-design-questions.md` §3.)

## Modules

- `format.py` — bridge file format: Turn dataclass, render/parse, body_sha256 integrity check, line-by-line parser, append (flock-protected), in-place mark_consumed, `find_unconsumed_oldest` (FIFO default) and `find_unconsumed_latest` (`--latest` opt-in). Stdlib-only.
- `gate.py` — KD26 channel-handler enforcement: reads OOL window state + active scope token, returns permit/deny verdict. Reference Python implementation; the L1 Bun channel plugin mirrors this logic in TypeScript.

Tests: `tests/bridge/test_format.py`, `tests/bridge/test_gate.py`. Run with `python3 -m unittest discover -s tests -t .`.

## Slash commands

- `/maw:bridge-out <message>` — writes to `~/.claude/bridge/<repo-slug>/from-{role}.md`, fires PushNotification
- `/maw:bridge-in [--from advisor|--from executor] [--latest]` — reads counter-party file (FIFO/oldest unconsumed by default; `--latest` opt-in for the older selector), treats as next user prompt, marks consumed
- `/maw:bridge-status` — prints last 5 turns, who-wrote-what, consumed/pending state

## Bridge file format

Markdown with HTML-comment delimiters + base64-encoded body + per-block `body_sha256` (PRD §3.2). Append-only; rotated at 10 unconsumed turns.

```
<!--BRIDGE-TURN-BEGIN turn_id=42-->
---
turn_id: 42
ts: 2026-05-08T03:15:22Z
role: executor
session_id: 16dbdc50-...
revision: 1
consumed_at: null
body_encoding: base64
body_sha256: a3f8c10245...
---
[base64-encoded markdown body, single line]
<!--BRIDGE-TURN-END turn_id=42-->
```

## Phase 0 spike

The operator runs the spike at the keyboard before Phase 2 begins. Three outcome paths in PRD §4.2.1.1:
- (a) mid-session ingestion contract works → Phase 2 proceeds as specified
- (b) only session-boundary works → Phase 2 ships with session-boundary semantics + amended success criterion
- (c) neither works → escalate to RemoteTrigger; if unavailable, Layer 2 blocks until primitive lands

Spike result MUST commit to `findings/2026-XX-XX-bridge-spike.md` before Phase 2 work begins.
