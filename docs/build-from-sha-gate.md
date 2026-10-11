# Build-from-SHA gate — never verify a stale artifact

*Backlog item #3 (2026-06-16). The precondition that makes item #2's oracle
evidence trustworthy at all.*

## The hazard

For interpreted maw code, "is this artifact from this SHA" is trivially true —
the worktree at the SHA *is* the system under test. For **compiled** targets
(iOS) the build can silently lag the source. The advisor's own first Ventusky
screenshot was a stale `DerivedData` build showing 4 tabs, not 5 — caught by
luck. **A reviewer (or a builder screenshot) verifying against a stale build
passes a broken state.** Every behavior/UI capture in item #2 is only worth
anything if the artifact under test was actually built from the SHA under
review.

## The gate

> Before any UI/behavior verification, confirm the artifact under test was built
> from the SHA under review — from a **clean** worktree. Never trust a
> pre-existing build artifact.

Item #1 makes "a clean worktree at the SHA" the reviewer's default state, so the
rebuild is cheap. This item makes the *check* mechanical.

## The tool: `maw/review/provenance.py`

**Compiled targets** — stamp at build, verify before review:

```bash
# post-build step (e.g. after xcodebuild): record what SHA produced this artifact
python3 -m review.provenance stamp build/App.app --repo .

# reviewer gate: FAILS (exit 2) on stale SHA, dirty build, or no provenance
python3 -m review.provenance verify build/App.app --sha bad2602
```

`stamp` writes a `.maw-build-provenance.json` sidecar recording the HEAD SHA and
whether the tree was dirty. `verify` fails when:

- **no provenance** — the artifact was never stamped (can't trust it);
- **stale** — recorded SHA ≠ SHA under review (short-SHA prefixes match);
- **dirty** — built from a tree with uncommitted changes, so the SHA does not
  fully describe it (override with `--allow-dirty` only when you mean it).

**Interpreted targets** (maw itself — no build step) — the worktree IS the
artifact:

```bash
python3 -m review.provenance head --sha bad2602 --repo .
# exit 0 iff worktree HEAD matches the SHA under review AND the tree is clean
```

The Python API (`stamp`, `verify`, `head_sha`, `is_clean`) is importable for
harnesses; git is shelled through an injectable runner so logic is testable
offline.

## Why this demotes the screenshot (ties to item #2)

A screenshot is the builder choosing what to show, and it carries **no proof of
which build produced it**. A stale-build screenshot manufactures false
confidence. The honest chain is: rebuild clean at the SHA → `provenance verify`
the artifact → run the item #2 state-forcing harness / `oracle` snapshot against
*that* verified artifact. Skip the first two steps and the third proves nothing.

## Playbook addendum

When a finding's verification involves a built artifact (compiled app, bundled
asset, generated output):

1. Rebuild from the clean worktree at the SHA under review (item #1 makes this
   the default state).
2. `provenance verify` the artifact (or `provenance head` for interpreted
   targets) **before** any capture.
3. Only then trust the item #2 oracle evidence. A capture of an unverified build
   is theater.
