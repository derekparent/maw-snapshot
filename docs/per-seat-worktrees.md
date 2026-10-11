# Per-seat git worktrees

*Backlog item #1 (2026-06-16). Dissolves the shared-tree root cause: N agent
seats sharing one working directory.*

## Why

The interactive pair ran as separate tmux sessions but shared **one** git
working directory. One tree for N seats produced, across the real arcs: a
realized review-integrity slip (an edit dropped between two scripts in the
shared tree shipped a false code-complete claim), the lost-spinner
`git checkout` over a peer's uncommitted work, base-deletion races, and a
stream-timeout where a seat's mid-refactor work survived only because a peer
manually committed it. Give each seat its **own** `git worktree` and
uncommitted work, branch state, and a clean-at-SHA checkout all become
per-seat — an ungraceful exit stops being everyone's hazard, and a clean
worktree at the SHA becomes the reviewer's *default* state, not a ritual.

## The repo-side tool

```
PYTHONPATH=<repo>/maw python3 -m net.worktree ensure --seat <advisor|executor>
```

- **stdout is ONLY the worktree path** — so a launcher can `cd "$(… ensure …)"`.
- status / notes go to **stderr**.
- **Idempotent**: create-or-attach. A second call attaches and re-seeds; it
  never creates a duplicate and never clobbers. An inconsistent on-disk or
  branch state is **refused with an explanation** (the operator decides, not
  the tool).
- `python3 -m net.worktree list` shows the per-seat worktrees (`seat/*`).

### What it sets up

| Aspect | Value | Why |
|---|---|---|
| Worktree path | `~/.maw-worktrees/<repo>-<seat>` | **Not** `~/.maw/worktrees/…` — see below |
| Branch | `seat/<seat>` (e.g. `seat/advisor`) | distinct per seat → no checkout contention |
| Bridge identity | `<worktree>/.maw/config.yaml` `repo_slug:` = shared slug | both seats share one `~/.claude/bridge/<slug>` |
| Switchboard identity | registry `repo_common_dir` = shared `.git` | `net who` recognizes both worktrees as one repo |

### Why the path is `~/.maw-worktrees`, not `~/.maw/worktrees`

The bridge resolves its repo root by walking cwd + ancestors for the first
directory containing a `.maw/`. If worktrees lived under a `.maw`-named parent
(`~/.maw/`), that walk would hit the parent and misresolve `repo_root` to it
(and the slug to its basename) in the window before the per-worktree `.maw/` is
seeded. Living **outside** any `.maw` ancestor means an unseeded worktree falls
through to its own directory — correct `repo_root` even pre-seed. (Structural
fix, not a mask — advisor hardening.)

## The bridge-split hazard this closes (load-bearing)

`git worktree add` does **not** copy untracked files, and `.maw/` is untracked.
So a naive per-seat worktree gets a different bridge identity than its peer —
the advisor seat would read `~/.claude/bridge/maw-advisor`, the executor
`…/maw-executor`, and **the pair's bridge channel would split**. `ensure`
prevents this by seeding each worktree's `.maw/config.yaml` with the **shared**
`repo_slug`, derived by reusing the bridge's own `_resolve_repo_slug(main_root)`
so registry identity and bridge identity can't diverge.

**Continuity guarantee:** for the `maw` repo the shared slug computes to `maw`
(no `config.yaml` in the main tree → basename), so both seats keep reading
`~/.claude/bridge/maw` — existing bridge history survives the migration.

## What a launcher must do

`maw/bin/maw-seat` does this for you. A launcher of your own (a shell alias or
function) must `cd` into the **seat worktree** before it starts `claude`, not
into the shared repo. Capture the path and guard it: a bare `cd "$(ensure)"`
silently falls back to the shared tree when `ensure` fails, because `cd ""` is
a no-op.

Quick reference:

- `ensure` must run **from inside the repo** (it resolves the repo from cwd), so
  the launcher `cd $repo` first, captures the path, guards it non-empty, then
  `cd`s into it.
- A pair launcher runs `ensure --seat advisor` for the advisor pane and
  `--seat executor` for the executor pane.
- `MAW_NET_DIR` (switchboard isolation per repo) is unchanged and orthogonal:
  the worktree path already keys on the repo basename, so `maw-advisor` and
  `<other-repo>-advisor` never collide.
- Generational seats (`advisor2`, …) pass the **role** (`--seat advisor`) and
  reuse the role's worktree — a rotation is the same role.

## New branch namespace

`seat/*` joins the model in `CLAUDE.md` (alongside `agent/*`, `feature/*`, …):
the long-lived parking branch for a seat's worktree. Seats still create
`feature/*` / `fix/*` / `docs/*` branches off it for actual work; the advisor's
per-PR review worktrees remain **ephemeral** (that is item #6's `maw review`,
built on top of this stable seat home).

## Registry schema + the repin requirement (load-bearing)

This change adds a `repo_common_dir` field to the switchboard registry `Entry`.
The reader is now **tolerant of unknown fields** (`registry._entry_from_data`),
so the schema can evolve without a newer entry crashing a current reader. But a
**deployed (cached) reader stays strict until repinned** — until the post-merge
repin lands, a registry entry written by the new code carries a field the cached
SessionStart/UserPromptSubmit hooks can't parse, and they read "no live
sessions" (this bit the live switchboard during the build). **Repin promptly on
merge** (already the standing rule for any `maw/net/` change) so the cache and
the working tree run the same registry schema.

## Migration / first run

First `ensure` per seat creates the worktree on a fresh `seat/<seat>` branch
based off `origin/<default>` (else local default, else HEAD) and seeds its
bridge identity. Re-running is safe and repairs drift (a deleted `.maw/`
self-heals). To retire a seat worktree, the operator uses `git worktree remove`
(the tool never deletes — destructive ops are out of its scope; cf. item #4).
