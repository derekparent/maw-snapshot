# tests

Run all: `python3 -m unittest discover -s tests -t .` (no pytest, no third-party deps — stdlib unittest only).

Current coverage:
- `tests/bridge/test_format.py` — 28 tests covering round-trip, integrity (body + hash tampering), malformed inputs, mark_consumed (revision-aware), find_unconsumed_latest, lock + perms.

## Planned test coverage (per PRD §7)

- **Unit**: stack detection, template substitution engine, bridge turn parsing (revision-aware), scope-token YAML schema validation (including SafeLoaderCapped expansion-bomb defense), audit log invariant checker, matcher tokenization preprocessor (false-positive rate on commit messages, grep, log searches)
- **Integration**: `/maw:init` end-to-end on a fresh repo (idempotent, conflict-handling), `/maw:bridge-out` → `/maw:bridge-in` round-trip, OOL window scope-token violation triggers halt, OOL window watchdog fires at duration boundary
- **Manual**: first real `/maw:init` on shop-app; first real `/bridge` session on tracker-app; first real OOL 30min window

## Phase 0 spike

The mid-session ingestion spike is NOT a unit test — it's an empirical reality check the operator runs at the keyboard. Result lands in `findings/2026-XX-XX-bridge-spike.md`, NOT here. The /bridge slash command's ingestion contract isn't unit-testable (it's a Claude Code harness behavior).
