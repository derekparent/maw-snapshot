---
description: Install the maw substrate into the current repository
argument-hint: "[--profile <name>] [--force] [--dry-run] [--upgrade]"
allowed-tools: [Bash]
---

# /maw:init — substrate transplant for the current repository

Run the CLI subcommand (PYTHONPATH injects the maw plugin install dir so
`init.cli` is importable from the user's cwd):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" init.cli $ARGUMENTS
```

## What this does

`/maw:init` installs hooks, git-hooks, schemas, and `.maw/config.yaml` from
the maw substrate templates into the current repository. The behavior is
governed by the input contract at
`findings/2026-05-10-maw-init-idempotency-contract.md`.

- **Fresh repo**: installs the full substrate; writes `.maw/config.yaml` and
  `.maw/state.json`.
- **Re-run, no drift**: no-op; reports "current" for each file.
- **Re-run with target edits**: preserves the edits; writes the freshly
  rendered version as a `<file>.maw-recommended` sibling next to each
  drifted file.
- **`--force`**: overwrites drifted files, backing up the originals under
  `.claude/maw/.attic/<filename>.<utc-iso8601>`.
- **`--dry-run`**: prints the plan; no filesystem writes.
- **`--upgrade`**: same as re-run but verbose.

## Exit codes (per contract §6)

- 0 — success
- 1 — refuse (not a git repo, target is maw itself, unknown profile, etc.)
- 2 — parse / path / placeholder error
- 3 — permission denied
- 4 — disk full / IO failure

## Surface the CLI output verbatim

The CLI prints a six-field banner per contract §8 (profile, capabilities,
per-file actions, bridge slug, summary counts, next-steps). Surface stdout
and stderr exactly as printed — do not paraphrase, do not skip lines. The
operator depends on the action list to verify what landed where.

## On refusal (exit 1) or parse error (exit 2)

Print the stderr verbatim. Do not retry automatically. The contract
deliberately fails closed on unset placeholders and on stack-profile
changes; retrying would mask the problem.

## After a successful run

The contract's "next-steps" line points at `/maw:status` (Phase 1d) and
the `MAW_HUMAN=1` alias. /maw:init does NOT modify `~/.zshrc` or any file
outside the target repo — surface the suggested alias to the user so they
can add it themselves.
