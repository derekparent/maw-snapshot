---
description: Review a PR/ref in a clean ephemeral worktree — reproduce the suite at the SHA, print the risk tier, auto-clean
argument-hint: "<PR#|ref> [--base <ref>] [--suite-cmd '<cmd>'] [--allow-attribution <sha>]"
allowed-tools: [Bash]
---

# /maw:review — the reviewer's clean-worktree ritual, automated

The user (the ADVISOR seat) wants to review a PR or branch HEAD the honest
way: in a CLEAN ephemeral worktree at the ref, NOT in the executor's editing
tree (which may carry uncommitted drift — the advisor must run from a stable
checkout, never the builder's live tree).

## Run the CLI

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" review.cli pr $ARGUMENTS
```

`$ARGUMENTS` is `<PR#|ref>` plus optional `--base <ref>` (default `main`) and
`--suite-cmd '<cmd>'`. Suite resolution: `--suite-cmd` flag, else `review.suite_cmd` in `.maw/config.yaml` as committed on the repo's **default branch**, else `stack.test_command` from the same file, else `python3 tests/run_all.py` (maw's runner). A bare integer is
resolved to a PR head via `gh pr view`; anything else is a git ref.

## Per-repo config

Set the repo's runner once in `.maw/config.yaml` instead of passing it every time:

```yaml
review:
  suite_cmd: "npx vitest run"        # or "xcodebuild test ...", "python3 tests/run_all.py"
  link: [".venv", "venv", "node_modules"]    # optional; these are the defaults
```

`stack.test_command` is used as the suite when `review.suite_cmd` is absent. A
top-level `suite_cmd:` is accepted as shorthand.

**Where the config is read from.** Unreviewed code must never choose the
command the reviewer runs, so the config is read (via `git show`) from one
place only: the file committed on the default branch, addressed by its full
ref `refs/remotes/origin/<default>` as named by `origin/HEAD`. Never from the
PR's tree or the working tree, and never from `--base`:

- A tag or branch named like the base (`origin/main`, `main`) cannot shadow
  it: a full `refs/…` path is matched exactly.
- `--base <a non-default branch>` (a stacked review) still diffs against that
  branch, but the config comes from the default branch and the command says
  so. The base of a stack is itself unreviewed.
- With an `origin` remote but no `origin/HEAD`, the default branch is unknown:
  the config is not read, the reason is printed, and the built-in defaults
  apply. Fix with `git remote set-head origin -a`. A repo with no `origin` at
  all uses its local `main`/`master`.
- No config on the default branch, or a failure reading it: built-in defaults,
  with the reason in the `suite:` line.

The `suite:` line of the output always names where the command came from.

**What the reader accepts.** It is a small reader for these three keys, not a
YAML parser. Write each value on one line: a bare string, a `'single'` or
`"double"` quoted string (`\"` and `\\` are the only escapes), and `link` as a
one-line `[a, b]` list. Anything else for these keys is **refused with the line
number and the reason**, and the review does not run: block scalars (`|`, `>`),
a value continued on the next line, text after a closing quote, a block-style
(`- item`) list, an inline `review: {…}` mapping, quoted or duplicate keys,
tabs. It never runs a shortened command. Pass `--suite-cmd` to review anyway
while the config is being fixed.

**Linked env dirs.** Each existing `link` path in the main checkout is
symlinked into the clean review worktree (a fresh worktree lacks untracked
`.venv` / `venv` / `node_modules`). Paths already tracked in the worktree,
missing, or escaping the repo are skipped. Suite commands that hardcode
absolute checkout paths still escape the worktree; use relative paths.

By design these are the main checkout's **real** directories, not copies: the
suite under review executes the PR's code, and that code can write into them
(install a package, drop a `.pth` file, edit a script in `node_modules/.bin`).
The clean worktree isolates the source tree, not the environment. If a PR is
not trusted to that degree, set `link: []` on the default branch or review it
with throwaway env dirs.

## What this does

1. Resolves the ref to a SHA.
2. Creates a **clean detached worktree** at that SHA in a temp dir and links the configured env dirs (`.venv`, `venv`, `node_modules`) into it.
3. Classifies the changed paths (vs `--base`) into a **risk tier** and prints
   the recommended gate. An oracle/UI tier flags that a passing suite is NOT
   sufficient — the state-forcing harness + build-from-SHA provenance are
   required (see `/maw:risk`, `/maw:provenance`, and the harness in
   `${CLAUDE_PLUGIN_ROOT}/docs/non-reproducible-oracle-review.md`).
4. Runs the suite **inside the clean worktree**.
   It also scans the commit messages in `--base..<ref>` for tool-attribution
   trailers (Co-Authored-By naming an AI, "Generated with", session links) and
   prints a **BLOCKING** finding with the offending SHAs; exit code 3 if the
   suite passed but such a trailer is present. `review.cli commits` runs just
   this check (builders: run it before requesting review). The range is
   measured against the full `refs/remotes/origin/<base>` path, so a tag
   named like the base cannot shadow it. A folded trailer and a space before
   the colon still count; text past 1000 characters of a line gets substring
   checks only.
   **False positive** (a co-author really named Claude, prose quoting the
   footer): detection is not loosened. The reviewer reruns with
   `--allow-attribution <sha>` (repeatable, 7+ hex chars), which drops that
   commit's findings and prints that it did. It is a reviewer-side flag;
   nothing in a commit can set it.
5. **Auto-removes** the worktree (in a `finally`) — a failed suite never
   leaves a stale tree.

The command's exit code is the suite's exit code.

## After the CLI

Surface the gate block and the suite result verbatim. If the tier is
oracle/UI, remind the user that the suite passing does not discharge the
oracle gate: a screenshot is not evidence — the harness verdict and the
provenance check are. Do not sign a verdict on a UI/external path from the
suite alone.

## On error

Print stderr verbatim and stop. Common causes: an unknown PR number / ref, or
`gh` not authenticated.
