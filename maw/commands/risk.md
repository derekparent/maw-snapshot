---
description: Classify changed paths into a review-depth tier — routes UI/oracle paths to the heavier gate
argument-hint: "[<path> ...] [--base <ref>]"
allowed-tools: [Bash]
---

# /maw:risk — path-classified review depth

The user wants to know how heavy a review a change deserves. The risk gear
classifies paths into a tier and recommends a proportionate gate, so docs-only
changes get a light pass and UI/external-oracle changes get the full gate plus
the state-forcing harness.

## Run the CLI

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" review.cli risk $ARGUMENTS
```

- With explicit paths: `/maw:risk maw/init/cli.py docs/foo.md` classifies exactly
  those.
- With no paths: it classifies the working diff vs `--base` (default `main`).

## The tiers (fail-safe by design)

- **docs** — `.md`/`.txt`/`.rst`, `docs/`, `findings/`: light pass (read the
  diff + confirm scope), suite skipped.
- **runtime** — code (`.py`, `.sh`, `.swift`, config, hooks): full adversarial
  review + reproduce the suite in a clean worktree at the SHA.
- **oracle** — UI / external-service paths (`.swift`/`.tsx`/`.html`/`.css`,
  `ui/`, `components/`, `views/`): full review **plus** the state-forcing
  harness + build-from-SHA provenance. A screenshot is not evidence.
- **unknown** — unrecognized paths: full review (fail-safe).

The tier of a mixed-tier change is the **most thorough** tier present — a
docs+UI change is oracle, never docs. Extension is authoritative, so
`docs/deploy.py` is runtime, not waved through as docs.

## Surface the gate, then act on it

Print the gate block verbatim. If the tier is **oracle**, the review is NOT
complete on a passing suite alone — route the user to `/maw:provenance` (verify
the artifact was built from the SHA under review) and the state-forcing
harness (`${CLAUDE_PLUGIN_ROOT}/docs/non-reproducible-oracle-review.md`). The tier SELECTS the
gate; honor it.
