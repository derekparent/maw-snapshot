# Risk gear + review helpers

*Backlog item #6 (2026-06-16). Review ceremony that scales to the change.*

## The problem

Review ceremony didn't scale down: a 1-line docs PR got the same
reproduce+suite as a runtime change, and per-PR assembly was heavy both ways
(~13 builder pings, ~9 reviewer worktree-add→suite→remove cycles).

## Risk gear — `review risk`

Path-classified review depth. A PR's changed paths → a tier → a proportionate
gate:

| tier | when | gate |
|---|---|---|
| `docs` | only `.md`/`.txt`/`docs/`/`findings/`/LICENSE/README | light: diff + scope, **skip suite** |
| `runtime` | code (`.py`/`.sh`/`.js`/config/hooks/…) | full adversarial + reproduce suite in a clean worktree |
| `oracle` | UI/external (`.swift`/`.tsx`/`ui/`/`components/`/…) | full + **state-forcing harness + build-from-SHA provenance** (items 2/3) |
| `unknown` | anything unrecognized | full (**fail-safe**) |

**Fail-safe direction:** ceremony only scales *down* for provably-light changes.
A multi-tier PR gets the **most-thorough** tier present (docs+runtime → runtime),
and an unrecognized path forces full review (never waved through as docs).

```
python3 -m review.risk            # (importable: classify_paths / recommend / gate_for_paths)
python3 -m review.cli risk maw/net/cli.py docs/x.md   # → RISK TIER: runtime
python3 -m review.cli risk --base main                 # classify the working diff
```

## `review request` — assemble a review request

Auto-fills SHA + branch + diff-stat + the risk tier/gate, and prompts only for
the **one part a human must state — the adversarial targets** (the single best
forcing function, per both seats):

```
python3 -m review.cli request --base main
```

The suite line is auto-set per the gear: runtime/oracle/unknown → "RUN
`python3 tests/run_all.py` and paste the result"; docs → "skipped".

## `review pr <ref>` — clean ephemeral worktree, automated

The reviewer's clean-worktree ritual as one command: resolve a PR number (via
`gh`) or a git ref to its SHA, `git worktree add --detach` at that SHA, run the
suite there, print the diff-stat + risk tier, and **auto-clean** (the worktree is
removed in a `finally`, so a failed suite never strands a tree). Returns the
suite's exit code.

```
python3 -m review.cli pr 27 --base main                 # review PR #27
python3 -m review.cli pr feature/x --suite-cmd "pytest" # any ref / suite command
```

This pairs with item #1: once per-seat worktrees land, the reviewer's stable seat
worktree hosts these ephemeral per-PR review worktrees on top.

## Notes

- Stdlib-only; git/gh shelled through an injectable runner (logic is tested
  offline; the ephemeral-worktree lifecycle is covered with a real-git test).
- The oracle tier's harness/provenance recommendation references items #2/#3; the
  risk gear names them but doesn't import them (independent code).
