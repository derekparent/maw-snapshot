---
description: Assemble a review request — auto-fill SHA, diff-stat, suite result, risk tier; prompt for adversarial targets
argument-hint: "[--base <ref>]"
allowed-tools: [Bash]
---

# /maw:review-request — assemble a review request for the counter-party

The user (the EXECUTOR seat, usually) wants to hand work to the advisor for
review. This command auto-fills everything machine-derivable and leaves the
ONE thing a human must supply blank: the adversarial targets.

## Run the CLI

`PYTHONPATH=${CLAUDE_PLUGIN_ROOT}` makes `review.cli` importable from the
user's cwd (the target repo, not the maw repo):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" review.cli request ${ARGUMENTS:+$ARGUMENTS}
```

`$ARGUMENTS` may carry `--base <ref>` (default: the FRESH `origin/<default>`
— the CLI fetches origin first) and `--allow-revert` (see below). The CLI runs
`git` in the cwd, so the user must be inside the repo under review.

## Pre-file freshness gate (exit 3 = REFUSED)

Before assembling anything, the CLI fetches origin and checks the branch:

- **Stale base.** If `origin/<default>` is not an ancestor of HEAD, it refuses
  with exit 3, says how many commits behind, and prints the fix:
  `"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" net.worktree restack`.
  Restack, re-run the suite, then request again.
- **Silent revert (the `reset --soft` trap).** A branch whose parent IS current
  main can still carry OLD versions of files recent main PRs changed. For each
  changed path, if the branch's content equals the pre-image of one of the last
  30 first-parent main commits that touched it (and differs from main now), or
  the branch deletes a file a recent main commit added, it refuses with exit 3
  and lists `path reverts <sha7> (<subject>)`. Rebuild the branch on a fresh
  worktree off current main. For an INTENTIONAL revert, re-run with
  `--allow-revert`: the request then carries a `REVERTS:` line per path so the
  reviewer sees it. `--depth N` changes the 30.
- **Offline / no origin ref:** warns on stderr and proceeds; the block says
  `freshness: unchecked`.

On success the block carries a `freshness:` line (e.g. `current with
origin/main@<sha7>`). Exit 3 is a refusal, not a crash: print stderr verbatim,
fix the branch, and do not hand-assemble the request around it.

## What this does

Emits a `REVIEW REQUEST` block: branch, HEAD SHA, base, diff-stat, and the
**risk tier + recommended gate** (from `review.risk`). For an oracle/UI tier
the gate block names the state-forcing harness + build-from-SHA provenance as
required evidence; for docs it says the suite is skipped.

The `suite:` line is a placeholder — `<RUN the repo's canonical test runner (maw: python3 tests/run_all.py) AND PASTE
THE RESULT>` — UNLESS the tier is docs (then `n/a`). Before surfacing the
block to the user, **run the suite yourself** when the tier calls for it,
then substitute the real result into the `suite:` line so the advisor gets a
truthful suite verdict, not a TODO.

## Surface the block, then stop

Print the assembled block. The `ADVERSARIAL TARGETS (REQUIRED)` section is
intentionally left with `<TARGET N>` slots — only the human author can name
the specific risks (injection surface, races, reject paths, fail-open holes).
Ask the user to fill them before sending the request to the advisor. Do not
invent targets on their behalf.

## Lint before you send (REQUIRED)

Once the targets are filled, save the request to a file and lint it before it
goes to the advisor:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" review.cli lint /tmp/review-request.txt
```

Exit 2 means REFUSED: there is no ADVERSARIAL TARGETS section, or it names
nothing real (placeholders, "none", "TBD", or one-word items). Fix it and lint
again. Do not send a refused request. A request that names nothing to attack
hides your risk model and gives the reviewer no floor to exceed. Send the
linted file with `net send <advisor> --from-file` (or `--stdin` with a quoted
heredoc). If the request must QUOTE strings the send-scan blocks, add
`--allow-literal` (see net.md).

## On error

If the CLI exits non-zero, print stderr verbatim and stop. Common cause: not
inside a git repo, or `--base` naming a ref that does not exist.
