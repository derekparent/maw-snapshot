---
description: Build-from-SHA provenance gate — stamp a built artifact with its source SHA, or verify the artifact under test was built from the SHA under review
argument-hint: "build <artifact> -- <cmd...> | stamp <artifact> | verify <artifact> --sha <SHA> | head --sha <SHA>"
allowed-tools: [Bash]
---

# /maw:provenance — never verify a stale artifact

For COMPILED targets the build can silently lag the source — a stale
`DerivedData` build showing 4 tabs instead of 5. Any UI/behavior capture is
only trustworthy if the artifact under test was actually built from the SHA
under review. This is the checkable gate (backlog item 3 / ADVISOR-PLAYBOOK
gate 11).

## Run the CLI

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" review.provenance $ARGUMENTS
```

Four subcommands:

- **build** — `build <artifact> [--repo <path>] -- <build command...>`: the
  preferred path. Captures HEAD + dirty bit BEFORE running the build, refuses
  (nothing stamped) if the build fails or the source changed while it ran, then
  stamps the artifact with the captured identity and its digest.
- **stamp** — `stamp <artifact> [--repo <path>]`: write a provenance sidecar
  (`.maw-build-provenance.json`) for an artifact built elsewhere, recording the
  repo's CURRENT HEAD SHA + dirty bit and the artifact's sha256. It cannot know
  which source an existing artifact came from, so it is the builder's claim.
- **verify** — `verify <artifact> --sha <SHA-under-review> [--allow-dirty]`:
  the reviewer gate. FAILS (exit 2) if the artifact is missing, its digest no
  longer matches the stamp, the sidecar is absent or malformed, it was built
  from a different SHA, or (by default) from a dirty tree. Run it **before
  trusting any UI/behavior capture** or the state-forcing harness.
- **head** — `head --sha <SHA> [--repo <path>]`: interpreted-target
  convenience (maw itself has no build step) — compares the worktree's HEAD +
  clean bit to the SHA under review.

## When this fires

The oracle/UI risk tier (`/maw:risk`) requires this evidence. Before a harness run
on a compiled artifact, pass `--artifact`/`--sha` so the harness runs this
gate FIRST and aborts on a stale build. An empty/unset `--sha` is REJECTED —
it must never false-pass the gate.

## Surface the verdict verbatim

Print the CLI line and honor the exit code: a non-zero verify means the
capture under review is off a stale/dirty artifact — do NOT sign a verdict on
it. Rebuild clean at the SHA and re-stamp.

The builder is trusted: this catches a stale or swapped artifact reaching a
reviewer by mistake, not a hostile builder (who can write the sidecar too).

Details that bite:

- **Build output goes outside the repo, or is gitignored.** `build` refuses
  when the dirty bit changes during the build, so a build writing into tracked
  or untracked-but-not-ignored paths is refused.
- **Sidecars from before the digest existed are malformed.** Rebuild (or
  re-stamp) those artifacts.
- **What the digest covers.** A top-level symlink artifact is resolved. Inside
  a directory artifact, inner symlinks count by their target text and are never
  followed, so content behind an inner symlinked directory is not covered.
  FIFOs, sockets and devices count by path and type only. An unreadable entry
  refuses the stamp rather than record a partial digest.
- A `--allow-dirty` pass reports `(dirty, allowed)`, never `(clean)`.
