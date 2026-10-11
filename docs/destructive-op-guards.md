# Structural destructive-op guards

*Backlog item #4 (2026-06-16). The residual hazards after item #1's per-seat
worktrees.*

## Why structural, not behavioral

Two-sided evidence from the arcs: **behavioral** rules failed to prevent repeats
— base-branch deletion happened *twice* after a "retarget first" reminder —
while a **structural** classifier block *succeeded* (it caught a mass-merge and
an over-scoped flag). Item #1 isolates a seat's own uncommitted work, but two
hazards survive worktree isolation, and this is the structural guard for them.

## What it guards (a PreToolUse hook on Bash)

1. **Base-branch deletion with open child PRs** — a remote/PR-graph fact, not a
   tree fact (worktrees do NOT fix it). Before a branch deletion (`git branch
   -d/-D/--delete`, `git push --delete`, `git push :branch`):
   - deleting a protected base (`main`/`master`/`dev`/`develop`) → **BLOCK**;
   - else `gh pr list --base <branch> --state open` → **BLOCK** if any open PR
     targets it (it would orphan them);
   - if `gh` can't answer → **warn and allow** (don't break deletes when gh is
     down; the message says to verify manually).
2. **Destructive-on-dirty-tree** — `git reset --hard`, `git clean -f…`,
   `git checkout -- …`/`.`, `git restore` (worktree), `git stash drop/clear`
   against a tree with uncommitted changes → **BLOCK**.
   - A discard limited to named paths (`git checkout -- a.py`, `git restore
     a.py`) is gated only on uncommitted changes in those paths, so a
     mutation-test restore is not refused because another file is dirty (#349).
     Only a plain path (letters, digits and `_ . / @ % + , : -`) counts as
     named: anything the shell may expand (`{a,b}`, `$F`, `~`, a glob, a zsh
     form such as `b.py(N)` or `(a|b).py`) is judged on the whole tree.
   - `git stash drop stash@{N}` (a literal ref) is allowed; a bare `drop`, a
     ref the shell expands, a drop inside a loop or `xargs` (it would run
     again and again), and `clear` stay gated (#349).
   - Inside a throwaway review worktree (the `/maw:review` temp shape, or a
     linked worktree in the Claude Code session scratchpad) discards are not
     gated. Stash ops in a scratchpad worktree still are: the stash stack is
     shared by every worktree of the repo.
3. **`git worktree remove --force` of a dirty worktree** → **BLOCK** (it deletes
   the worktree's uncommitted and untracked files). Removing a scratch review
   worktree or a `~/.claude-builds/<name>` builder worktree is allowed; without
   `--force` git refuses a dirty worktree on its own.

A blocked compound command is refused whole: the message names each segment
that matched and says that no segment ran, so split the safe parts into their
own call. A heredoc body is read line by line, so a note whose prose names
a gated op is refused as if it ran it; write such a note with a call of its
own that holds nothing else, or with the Write tool.

## Overrides (the "confirm")

A PreToolUse hook can't pop an interactive confirm, so the gate is block-with-a-
documented-override — a conscious act, which is exactly what the unconscious
base-deletion races lacked:

```
MAW_ALLOW_BASE_DELETE=1   # permit base / PR-targeted branch deletion
MAW_ALLOW_DESTRUCTIVE=1   # permit a destructive op against a dirty tree
```

## Threat model & robustness

- **Accident speed-bump, not an adversary block** (KD18 trust boundary): a shell
  session can set the override; the point is to force a conscious act. It defends
  a model that *forgot the rule*, the way the seat-env check defends a *deceived*
  model — not one choosing to defy it.
- **Fails OPEN everywhere.** It runs before every Bash command, so any parse or
  runtime error → allow. It adds value when its checks succeed, never by breaking
  the shell.
- **shlex-guarded detection.** `git commit -m "use git reset --hard"` is not
  mistaken for the op (the message is one token; the subcommand is `commit`). A
  segment with unbalanced quotes is skipped (fail-open), not force-blocked.
- **Command-position anchoring.** `git` must be the command (token 0, or after
  benign *bare* wrappers like `sudo`/`env`/`VAR=val`), so `echo git reset --hard`
  — git-as-data — does not false-block (it's not running git). `sudo git clean
  -fd` and `FOO=bar git reset --hard` still parse as git.
  - *Known gap (acceptable per the speed-bump model):* a wrapper carrying its OWN
    options before `git` — `sudo -u user git …`, `nice -n5 git …`, `env -i git …`
    — is not parsed past those options, so the op is missed (a false negative,
    not a false positive). The common forms are covered; the wrapper-with-options
    forms are rare in agent commands and the guard is an accident speed-bump, not
    a complete sandbox.
- **Conservative classification.** Only the specific dangerous forms match;
  `git reset --soft`, `git clean -n`, `git checkout <branch>`, `git restore
  --staged`, `git stash pop` are all allowed.

## Deployment

Wired as a plugin PreToolUse(Bash) hook in `maw/hooks/hooks.json`, so it protects
every maw-enabled session — including the pair, where the races happened. It is
Tier-2 by nature (PreToolUse; parent-session, best-effort — KD31): it does not
survive subagents and is not the guarantee the push-gate is. It activates on the
next **plugin repin** (see CLAUDE.md); merging alone does not change a running
session.

The classifier (`maw/guards/destructive.py`) is importable and fully unit-tested
with injected git/gh runners, so the logic is verified offline independent of the
hook plumbing.
