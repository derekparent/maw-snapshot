"""Per-seat git worktrees — dissolve the shared-tree root cause (backlog item 1).

N agent seats sharing ONE git working directory is the root cause behind a
cluster of failures the 2026-06-16 backlog traced to a single fact: a realized
review-integrity slip (an edit dropped between two scripts in the shared tree
shipped a false code-complete claim), the lost-spinner `git checkout` over a
peer's uncommitted work, the base-deletion races, and the stream-timeout rescue
where a seat's mid-refactor work survived only because a peer manually committed
it on the shared tree. Give each seat its own `git worktree` and uncommitted
work, branch state, and a clean-at-SHA checkout all become per-seat — an
ungraceful exit stops being everyone's hazard.

This module is the repo-side support the launchers call. The shipped launcher
(`maw/bin/maw-seat`, module `launch.seat`) calls `ensure` and starts the seat in
the path it returns; a hand-rolled shell alias can do the same with
`python3 -m net.worktree ensure --seat <role>`.

Scope (per the backlog item):
  - STABLE per-seat worktrees only. The advisor's ephemeral per-PR review
    worktrees stay ephemeral (that is item #6's `maw review`); this builds the
    durable seat home those sit on top of.
  - Keyed on seat ROLE (advisor/executor), not the generational session name —
    a rotation (advisor → advisor2) is the same role and reuses the same
    worktree. The transient dual-seat overlap during a handoff is item #7's
    problem, not this module's.
  - No destructive ops. `ensure` creates-or-attaches and never clobbers; an
    inconsistent state is REFUSED with an explanation, never auto-repaired.
    Worktree removal is left to the operator (`git worktree remove`).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Seat is keyed on ROLE: a generational name (advisor2) maps to its role's
# worktree. Strict membership doubles as path-injection defense — the seat
# string lands in a filesystem path, so it may not be an arbitrary token.
SEATS = ("advisor", "executor")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class WorktreeError(Exception):
    pass


def _run(cmd: list[str], cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)


def worktree_home() -> Path:
    """Where per-seat worktrees live. MAW_WORKTREE_DIR overrides (tests).

    Deliberately ~/.maw-worktrees, NOT ~/.maw/worktrees: the bridge's
    repo_root resolver walks cwd+ancestors for the first dir containing a
    `.maw/`. Nesting worktrees under a `.maw`-named parent would make that
    walk hit the parent and misresolve repo_root to it (and the slug to its
    basename) in the pre-seed window. Living outside any `.maw` ancestor
    means an unseeded worktree falls through to `return cwd` = the worktree —
    correct repo_root even before the slug seed lands. Structural fix, not a
    mask (advisor hardening, item 1).
    """
    override = os.environ.get("MAW_WORKTREE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".maw-worktrees"


def _git(args: list[str], cwd: str | None, runner: Runner) -> str:
    cp = runner(["git", *args], cwd)
    if cp.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed (rc={cp.returncode}): {cp.stderr.strip()}"
        )
    return cp.stdout.strip()


def _git_ok(args: list[str], cwd: str | None, runner: Runner) -> bool:
    return runner(["git", *args], cwd).returncode == 0


def repo_common_dir(cwd: str | None = None, runner: Runner = _run) -> Path:
    """Absolute path to the shared .git dir — the logical-repo identity.

    From inside a LINKED worktree, `--git-common-dir` resolves to the MAIN
    repo's .git, not the per-worktree `.git/worktrees/<name>`. That shared
    path is what makes N worktrees of one repo recognizable as the same
    logical repo — the switchboard `repo_root` ambiguity the backlog names
    (distinct working dirs, one `.git` and one PR graph).
    """
    out = _git(["rev-parse", "--git-common-dir"], cwd, runner)
    p = Path(out)
    if not p.is_absolute():
        base = Path(cwd) if cwd else Path.cwd()
        p = base / p
    # resolve() the ABSOLUTE branch too, not just the relative one: git emits a
    # relative ".git" from the main worktree but an absolute path from a linked
    # worktree. Canonicalizing only one means a repo behind a symlink (e.g.
    # macOS /var → /private/var) yields two different keys for the same repo, so
    # same_repo() would fail to group the pair (advisor Note 1).
    return p.resolve()


def main_worktree_root(cwd: str | None = None, runner: Runner = _run) -> Path:
    """Top-level dir of the MAIN worktree (parent of the shared .git).

    The repo name (basename) keys the per-seat worktree path, so it must be
    stable no matter which worktree the caller sits in.
    """
    common = repo_common_dir(cwd, runner)
    # Normal repo: common dir is <root>/.git. Bare repo: it IS the repo dir.
    return common.parent if common.name == ".git" else common


def default_branch(cwd: str | None = None, runner: Runner = _run) -> str:
    """Name of the repo's default branch (for the seat-branch base)."""
    cp = runner(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd
    )
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip().rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        if _git_ok(["show-ref", "--verify", "--quiet", f"refs/heads/{cand}"], cwd, runner):
            return cand
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd, runner)


def base_ref(cwd: str | None = None, runner: Runner = _run) -> str:
    """A committish that exists, to base a fresh seat branch on.

    Prefer `origin/<default>` (most current; a fresh clone may not have a
    local default branch at all), then the local default, then HEAD.
    """
    return base_refs(cwd, runner)[0]


def remote_default_ref(db: str) -> str:
    """The full name of origin's default branch, for a git argument. git
    resolves a short name through refs/tags/ BEFORE refs/heads/ and
    refs/remotes/, so a pushed tag named `origin/main` shadows the short form;
    a full refs/ path is matched exactly."""
    return f"refs/remotes/origin/{db}"


def base_refs(cwd: str | None = None, runner: Runner = _run) -> "tuple[str, str]":
    """(printed name, git argument) of the base `base_ref` picks. The printed
    name stays short (`origin/main`); the git argument is the full ref."""
    db = default_branch(cwd, runner)
    for label, full in ((f"origin/{db}", remote_default_ref(db)), (db, f"refs/heads/{db}")):
        if _git_ok(["rev-parse", "--verify", "--quiet", f"{full}^{{commit}}"], cwd, runner):
            return label, full
    return "HEAD", "HEAD"


_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _refuse_branch_in_live_main(cwd: str | None, runner: Runner) -> None:
    """#144: `git checkout -b` runs in the CWD's worktree. Run from the MAIN
    (live/editing) tree of a seat-based repo, it puts the operator's editing tree on the
    lane branch — the exact contamination the feedback reported (recovery needed
    a manual checkout of main). Refuse when CWD resolves to the main worktree AND
    per-seat worktrees exist (a plain single-worktree repo has nowhere else to
    branch, so it is left alone)."""
    here = Path(_git(["rev-parse", "--show-toplevel"], cwd, runner)).resolve()
    if here == main_worktree_root(cwd, runner) and seat_worktrees(cwd, runner):
        seats = ", ".join(p for _s, p, _b in seat_worktrees(cwd, runner))
        raise WorktreeError(
            f"refusing to create a lane branch in the MAIN worktree ({here}): this "
            f"repo has per-seat worktrees, and a checkout here would put the live "
            f"editing tree on your lane branch (#144). Run it from your seat "
            f"worktree instead ({seats})."
        )


def new_branch(
    name: str,
    cwd: str | None = None,
    runner: Runner = _run,
    onto: str | None = None,
) -> str:
    """Create + switch to `name`, based on a FRESH `origin/<default>` — never the
    current branch. Returns the base ref used.

    #102: `git checkout -b <new>` (no base) uses the CURRENT HEAD as base, so a
    new lane started from a feature branch silently STACKS on that branch and
    inherits its commits — the maw-executor hit this twice in one /maw:goal arc,
    contaminating two PRs until a `git rebase --onto main`. This fetches first so
    `origin/<default>` is current, then bases the new branch on it explicitly.
    Uncommitted changes carry over (you keep your work on the new branch); only
    the BASE is forced.

    #144: refuses outright when run in the live/main worktree of a seat-based
    repo — a checkout there contaminates the operator's editing tree.

    `onto`: base on this ref instead of `origin/<default>` — for a deliberate
    LOCAL-ONLY baseline (a commit not on origin) that the fresh-origin default
    would silently discard. An unresolvable ref refuses before anything is created.
    """
    if not _BRANCH_NAME_RE.match(name or "") or ".." in name:
        raise WorktreeError(
            f"invalid branch name {name!r}: use letters/digits/._/- and no '..'"
        )
    _refuse_branch_in_live_main(cwd, runner)
    if onto is not None:
        # Check out the RESOLVED sha, never the raw ref: a ref that moves after
        # the check, or one that starts with '-', cannot change what is based on.
        cp = runner(["git", "rev-parse", "--verify", "--quiet", "--end-of-options",
                     f"{onto}^{{commit}}"], cwd)
        sha = cp.stdout.strip() if cp.returncode == 0 else ""
        if not sha:
            raise WorktreeError(
                f"--onto {onto!r} does not resolve to a commit; no branch created"
            )
        _git(["checkout", "--no-track", "-b", name, sha], cwd, runner)
        _track_own_name(name, cwd, runner)
        return onto
    # Best-effort fetch — an offline/remoteless repo still gets a local-base fallback.
    runner(["git", "fetch", "origin", "--quiet"], cwd)
    base, base_arg = base_refs(cwd, runner)
    # --no-track: `checkout -b x origin/main` would make origin/main the lane's
    # upstream (#354) — a bare `git push` then fails, or under
    # push.default=upstream lands on main. The lane tracks its own name instead.
    _git(["checkout", "--no-track", "-b", name, base_arg], cwd, runner)
    _track_own_name(name, cwd, runner)
    return base


def _track_own_name(name: str, cwd: str | None, runner: Runner) -> bool:
    """Point `name`'s upstream at origin/<name> (#354), before its first push:
    a bare `git push` then lands on the lane's own branch. No origin → no-op."""
    if not _git_ok(["remote", "get-url", "origin"], cwd, runner):
        return False
    _git(["config", f"branch.{name}.remote", "origin"], cwd, runner)
    _git(["config", f"branch.{name}.merge", f"refs/heads/{name}"], cwd, runner)
    return True


def _origin_on_github(cwd: str | None, runner: Runner) -> bool:
    cp = runner(["git", "remote", "get-url", "origin"], cwd)
    return cp.returncode == 0 and "github" in (cp.stdout or "").lower()


def branch_pr(branch: str, cwd: str | None = None, runner: Runner = _run) -> "dict | None":
    """The PR gh associates with `branch` ({state, number, baseRefName}), or None
    when there is none or gh cannot say. Asked only when origin is on GitHub, so
    a local/remoteless repo never shells out to gh. gh prefers an OPEN PR for the
    branch and falls back to a merged/closed one — the state the guards need."""
    if not _origin_on_github(cwd, runner):
        return None
    try:
        cp = runner(["gh", "pr", "view", branch, "--json", "state,number,baseRefName"], cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
        return None
    return data


def remote_branch_exists(name: str, cwd: str | None = None, runner: Runner = _run) -> "bool | None":
    """True/False from `git ls-remote`, None when origin cannot be asked."""
    cp = runner(["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{name}"], cwd)
    if cp.returncode == 0:
        return True
    if cp.returncode == 2:
        return False
    return None


def _refuse_merged(target: str, pr: "dict | None", what: str, override: str) -> None:
    if pr and pr.get("state") == "MERGED":
        raise WorktreeError(
            f"PR #{pr.get('number', '?')} for {target!r} is already MERGED — {what} reaches no "
            f"open PR: new commits are stranded, and a branch re-pushed after its merge "
            f"invites a PR that re-carries (or reverts) merged work (#354). Start a fresh lane "
            f"off current main — net.worktree branch <new-name> — and cherry-pick the new "
            f"commits onto it. {override}"
        )


def _push_cmd(target: str, force: bool) -> list[str]:
    """Always name origin/<target> and set it as upstream (#354): a bare push
    fails with no upstream, and a lane tracking origin/main fails (simple) or
    pushes to main (upstream)."""
    return ["git", "push", *(["--force-with-lease"] if force else []), "-u", "origin", target]


@dataclass
class RestackResult:
    """Outcome of a restack. `ok` is True only on a clean rebase (pushed unless
    --no-push); a conflict or a failed push is `ok=False` with the branch left
    for the caller to finish by hand."""

    ok: bool
    branch: str
    base: str
    action: str  # "restacked" | "conflicts" | "push-failed"
    detail: str
    conflicts: list[str] = field(default_factory=list)
    onto: str = ""          # the old-parent SHA used (stacked restack only)
    onto_method: str = ""   # how it was named: "gh" | "cherry" (via --onto auto) | "explicit"


ONTO_AUTO = "auto"


def _gh_old_parent(target: str, db: str, cwd: str | None, runner: Runner) -> str:
    """The merged parent PR's last head SHA, via gh — or '' when gh cannot say.

    The child PR's base branch names the parent; the parent PR, merged with that
    branch as its head, records its last head SHA. Accepted only if it resolves
    locally AND is an ancestor of the child (a SHA outside the child's history is
    not the commit the child branched from).
    """
    cp = runner(["gh", "pr", "view", target, "--json", "baseRefName", "-q", ".baseRefName"], cwd)
    if cp.returncode != 0:
        return ""
    parent_branch = cp.stdout.strip()
    if not parent_branch or parent_branch == db:
        return ""
    cp = runner(["gh", "pr", "list", "--state", "merged", "--head", parent_branch,
                 "--json", "headRefOid,number", "--limit", "5"], cwd)
    if cp.returncode != 0:
        return ""
    try:
        prs = json.loads(cp.stdout or "[]")
    except ValueError:
        return ""
    for pr in prs if isinstance(prs, list) else []:
        sha = str(pr.get("headRefOid", "")) if isinstance(pr, dict) else ""
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            continue
        if not _git_ok(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], cwd, runner):
            continue
        if _git_ok(["merge-base", "--is-ancestor", sha, target], cwd, runner):
            return sha
    return ""


def _cherry_old_parent(target: str, base: str, cwd: str | None, runner: Runner) -> str:
    """The newest commit of the LEADING run of already-upstream (`-`) commits in
    the child's own history — the parent's commits whose patches main now holds.
    '' when the child's oldest unmerged commit is its own (`+`)."""
    cp = runner(["git", "cherry", base, target], cwd)
    if cp.returncode != 0:
        return ""
    last = ""
    for ln in cp.stdout.splitlines():
        mark, _, sha = ln.strip().partition(" ")
        if mark != "-":
            break
        last = sha.strip()
    return last


def discover_old_parent(
    target: str, cwd: str | None = None, runner: Runner = _run,
) -> "tuple[str, str]":
    """(sha, method) for `restack --onto auto` (#197): gh first, `git cherry`
    second; WorktreeError when neither can name the old parent."""
    db = default_branch(cwd, runner)
    sha = _gh_old_parent(target, db, cwd, runner)
    if sha:
        return sha, "gh"
    sha = _cherry_old_parent(target, base_refs(cwd, runner)[1], cwd, runner)
    if sha:
        return sha, "cherry"
    raise WorktreeError(
        f"--onto auto could not find {target!r}'s old parent: no merged parent PR via "
        f"gh, and no leading run of already-upstream commits (`git cherry`) — a "
        f"squash-merged multi-commit parent leaves none. Pass the old parent SHA "
        f"explicitly: --onto <sha>"
    )


def _current_branch(cwd: str | None, runner: Runner) -> str:
    cp = runner(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd)
    if cp.returncode != 0 or not cp.stdout.strip():
        raise WorktreeError("detached HEAD — checkout the branch before restack")
    return cp.stdout.strip()


def restack(
    branch: str | None = None,
    cwd: str | None = None,
    runner: Runner = _run,
    push: bool = True,
    onto: str | None = None,
) -> RestackResult:
    """Rebase `branch` (default: current) onto a FRESH `origin/<default>` and
    finish with `--force-with-lease` — the canonicalized amend-PR /
    rebase-after-siblings-merge dance (#117), the sibling of `new_branch` (#112).

    The MERGE side is one auditable step (`session_auth merge --pr N`); the
    restack side was a hand-assembled `rebase` + `checkout --theirs` +
    `push --force-with-lease` that bit the executor ≥3 times in two runs (#92,
    #99, #113). This does the safe, mechanical part and surfaces the part that
    needs judgment:

    - Refuses (WorktreeError, before touching anything): a detached HEAD, an
      invalid branch name, the DEFAULT branch (you never rebase main), a branch
      that does not exist, a branch checked out in ANOTHER worktree (rebasing it
      from here would fight that worktree — the #112 worktree-safety property),
      or a dirty working tree (rebase would refuse anyway; surface it cleanly).
    - On a CLEAN rebase: `git push --force-with-lease` (unless `push=False`) and
      return ok. A no-op rebase (already current) is a clean rebase.
    - On CONFLICTS: leave the rebase IN PROGRESS and return the conflicted files
      + the next steps (resolve, `git add`, `git rebase --continue`, re-run to
      push; or `git rebase --abort`). It NEVER auto-resolves — a conflict is a
      judgment call, not a mechanical one.

    Scope: the default form handles rebase-onto-fresh-default (the recurring
    bite). For a STACKED PR whose parent branch was merged + auto-deleted
    mid-flight (#138), pass `onto=<old-parent-sha>` (the SHA the child was
    branched from): this replays ONLY the child's OWN commits onto fresh
    `origin/<default>` via `git rebase --onto <base> <old-parent> <child>`,
    instead of re-applying the now-merged parent's commits. `onto="auto"` (the
    keyword — a branch literally named `auto` is `refs/heads/auto`) discovers the
    old parent: the merged parent PR's last head SHA via gh (ancestor-checked),
    else the newest of the child's leading already-upstream commits via
    `git cherry` (#197); neither → refused, pass the SHA. After a clean
    stacked restack, RETARGET the child PR's base to the default branch
    (`gh pr edit <child> --base <default>`) — the surfaced next step.
    """
    target = branch or _current_branch(cwd, runner)
    if not _BRANCH_NAME_RE.match(target) or ".." in target:
        raise WorktreeError(
            f"invalid branch name {target!r}: use letters/digits/._/- and no '..'"
        )
    db = default_branch(cwd, runner)
    if target == db:
        raise WorktreeError(
            f"refusing to restack the default branch {target!r} — restack a "
            f"feature/fix branch, not main"
        )
    if not _git_ok(["show-ref", "--verify", "--quiet", f"refs/heads/{target}"], cwd, runner):
        raise WorktreeError(f"branch {target!r} does not exist")
    here = Path(_git(["rev-parse", "--show-toplevel"], cwd, runner)).resolve()
    for path, br in list_worktrees(cwd, runner).items():
        if br == target and Path(path).resolve() != here:
            raise WorktreeError(
                f"branch {target!r} is checked out in another worktree ({path}) — "
                f"restack it from there, not here"
            )
    # --untracked-files=no: only TRACKED modifications block a rebase. Bare
    # --porcelain is truthy for untracked files, and every real seat worktree
    # carries an untracked .maw/ — so a bare check would refuse ALWAYS in exactly
    # the environment this helper serves (advisor catch, 2026-07-18). git rebase
    # itself ignores untracked files; we match that.
    if _git(["status", "--porcelain", "--untracked-files=no"], cwd, runner):
        raise WorktreeError(
            "working tree has uncommitted TRACKED changes — commit or stash "
            "before restack (rebase would refuse anyway)"
        )
    # #354: a merged PR's branch is not restacked + force-pushed (the push would
    # reach no PR), and the BARE form is wrong for a PR stacked on another branch
    # (it would pull the parent's commits into this PR).
    pr = branch_pr(target, cwd, runner)
    if push:
        _refuse_merged(target, pr, "a restack + force-push",
                       "(Rebase locally only with --no-push.)")
    pbase = str(pr.get("baseRefName") or "") if pr and pr.get("state") == "OPEN" else ""
    if onto is None and pbase and pbase != db:
        gone = remote_branch_exists(pbase, cwd, runner) is False
        if gone:
            how = (f"its base {pbase!r} no longer exists on origin (merged + deleted?): "
                   f"replay only this PR's commits with `restack --onto auto` (or "
                   f"--onto <old-parent-sha>), then `gh pr edit {target} --base {db}`")
        elif (branch_pr(pbase, cwd, runner) or {}).get("state") == "MERGED":
            how = (f"its parent {pbase!r} already merged (the branch still exists on "
                   f"origin): replay only this PR's commits with `restack --onto auto` "
                   f"(or --onto <old-parent-sha>), then `gh pr edit {target} --base {db}`")
        else:
            how = (f"its parent {pbase!r} is still open: rebase on the parent instead "
                   f"(`git rebase origin/{pbase}`), or once the parent merges, "
                   f"`restack --onto auto`")
        raise WorktreeError(
            f"refusing a bare restack of {target!r}: PR #{pr.get('number', '?')} is stacked "
            f"on {pbase!r}, not {db!r}, and a bare restack onto origin/{db} would pull the "
            f"parent's commits into this PR — {how}"
        )
    method = ""
    if onto == ONTO_AUTO:
        # The keyword, never a branch named `auto` (pass refs/heads/auto for that).
        # Fetch first so origin/<default> is current for the cherry comparison.
        runner(["git", "fetch", "origin", "--quiet"], cwd)
        onto, method = discover_old_parent(target, cwd, runner)
    elif onto is not None:
        method = "explicit"
    if onto is not None:
        if not _BRANCH_NAME_RE.match(onto) and not re.fullmatch(r"[0-9a-fA-F]{7,40}", onto):
            raise WorktreeError(f"invalid --onto {onto!r}: pass a commit SHA or ref name")
        if not _git_ok(["rev-parse", "--verify", "--quiet", f"{onto}^{{commit}}"], cwd, runner):
            raise WorktreeError(
                f"--onto {onto!r} is not a resolvable commit — pass the OLD parent "
                f"SHA the child was branched from (the merged+deleted parent's tip)"
            )
    # Best-effort fetch so origin/<default> is current (offline → local fallback).
    fetched = runner(["git", "fetch", "origin", "--quiet"], cwd).returncode == 0
    base, base_arg = base_refs(cwd, runner)
    # A failed fetch leaves the last-fetched base, which may be behind the remote.
    stale = "" if fetched else " (WARNING: git fetch failed, so this base may be stale)"
    # Stacked (#138): replay only the child's own commits (after <onto>) onto the
    # fresh base. Default: rebase the whole branch onto the base.
    rebase_cmd = (["git", "rebase", "--onto", base_arg, onto, target]
                  if onto is not None else ["git", "rebase", base_arg, target])
    cp = runner(rebase_cmd, cwd)
    if cp.returncode != 0:
        cf = runner(["git", "diff", "--name-only", "--diff-filter=U"], cwd)
        conflicts = [ln for ln in cf.stdout.splitlines() if ln.strip()] if cf.returncode == 0 else []
        return RestackResult(
            ok=False,
            branch=target,
            base=base,
            action="conflicts",
            detail=(
                f"rebase onto {base} hit conflicts in {len(conflicts)} file(s); the "
                f"rebase is IN PROGRESS. Resolve them (edit, or `git checkout "
                f"--theirs/--ours <file>`), `git add`, `git rebase --continue`, then "
                f"re-run restack to push — or `git rebase --abort` to bail."
            ),
            conflicts=conflicts,
            onto=onto or "",
            onto_method=method,
        )
    if not push:
        return RestackResult(True, target, base, "restacked", f"rebased onto {base} (not pushed){stale}",
                             onto=onto or "", onto_method=method)
    pp = runner(_push_cmd(target, force=True), cwd)
    if pp.returncode != 0:
        return RestackResult(
            ok=False,
            branch=target,
            base=base,
            action="push-failed",
            detail=(
                f"rebased onto {base} cleanly, but `git push --force-with-lease` "
                f"failed: {pp.stderr.strip()} — push by hand once resolved"
            ),
            onto=onto or "",
            onto_method=method,
        )
    return RestackResult(True, target, base, "restacked", f"rebased onto {base} + pushed --force-with-lease{stale}",
                         onto=onto or "", onto_method=method)


@dataclass
class PushResult:
    ok: bool
    branch: str
    lines: list[str] = field(default_factory=list)


def push(
    cwd: str | None = None,
    runner: Runner = _run,
    base: str | None = None,
    allow_merged: bool = False,
    allow_revert: bool = False,
) -> PushResult:
    """Push the current lane to origin/<branch> with the lane-safety checks
    (#354). Refuses (WorktreeError, nothing pushed):

      - the default branch;
      - a branch whose PR already MERGED (`--allow-merged` overrides);
      - a SILENT REVERT: the branch's parent is current main but its tree holds
        the pre-image of recent main changes (the `reset --soft` / squash-after-
        main-moved trap — a green suite carrying reverted merged work). The sniff
        is review.freshness's (#322), run before the work leaves the machine.
        `--allow-revert` pushes an intentional revert and names it.

    Warns (pushes anyway): a base behind main, and a PR base — `--base`, or the
    open PR's base — that no longer exists on origin (check it BEFORE
    `gh pr create --base <parent>`). Always sets the upstream to origin/<branch>.
    """
    from review import freshness  # lazy: review.freshness imports this module

    target = _current_branch(cwd, runner)
    if not _BRANCH_NAME_RE.match(target) or ".." in target:
        raise WorktreeError(f"invalid branch name {target!r}")
    db = default_branch(cwd, runner)
    if target == db:
        raise WorktreeError(f"refusing to push the default branch {target!r} — push a lane branch")
    lines: list[str] = []
    pr = branch_pr(target, cwd, runner)
    if not allow_merged:
        _refuse_merged(target, pr, "a push", "(--allow-merged overrides.)")
    elif pr and pr.get("state") == "MERGED":
        lines.append(f"WARN: PR #{pr.get('number', '?')} for {target!r} already MERGED "
                     f"(--allow-merged)")

    fetched = runner(["git", "fetch", "origin", "--quiet"], cwd).returncode == 0
    if not fetched:
        lines.append("WARN: git fetch origin failed — main and base checks use the last fetch")
    ref, gref = f"origin/{db}", remote_default_ref(db)
    if _git_ok(["rev-parse", "--verify", "--quiet", f"{gref}^{{commit}}"], cwd, runner):
        if _git_ok(["merge-base", "--is-ancestor", gref, "HEAD"], cwd, runner):
            reverts = freshness.sniff_reverts(gref, runner, cwd)
            if reverts and not allow_revert:
                listed = "\n".join(f"  - {r.describe()}" for r in reverts)
                raise WorktreeError(
                    f"{target!r} silently reverts recent {ref} work — its parent is current "
                    f"main but its tree is old (the `reset --soft` / squash-after-main-moved "
                    f"trap; a green suite does not catch it):\n{listed}\n"
                    f"Rebuild the lane: net.worktree branch <new-name> off current main, then "
                    f"cherry-pick your commits. An INTENTIONAL revert: --allow-revert."
                )
            for r in reverts:
                lines.append(f"REVERTS (allowed): {r.describe()}")
        else:
            behind = _git(["rev-list", "--count", f"HEAD..{gref}"], cwd, runner)
            lines.append(f"WARN: {behind} commit(s) behind {ref} — restack before review "
                         f"(net.worktree restack); review-request refuses a stale base")
    else:
        lines.append(f"WARN: no {ref} to compare against — revert and freshness checks skipped")

    check_base = base or (str(pr.get("baseRefName") or "")
                          if pr and pr.get("state") == "OPEN" else "")
    if check_base and check_base != db:
        exists = remote_branch_exists(check_base, cwd, runner)
        if exists is False:
            stale = _git_out_ok(["rev-parse", "--verify", "--quiet",
                                 f"refs/remotes/origin/{check_base}^{{commit}}"], cwd, runner)
            onto = f"--onto {stale[:12]}" if stale else "--onto auto"
            lines.append(
                f"WARN: base {check_base!r} no longer exists on origin (merged + deleted "
                f"parent?) — a PR against it cannot be opened, and an open one is closed or "
                f"retargeted. Move the lane onto {db} first: net.worktree restack {onto} "
                f"(the old parent tip), then gh pr create --base {db}"
            )
        elif exists is None:
            lines.append(f"WARN: could not ask origin whether base {check_base!r} exists")

    cp = runner(_push_cmd(target, force=False), cwd)
    if cp.returncode != 0:
        lines.append(f"push FAILED: {(cp.stderr or '').strip()}")
        return PushResult(False, target, lines)
    lines.append(f"pushed {target} -> origin/{target} (upstream origin/{target})")
    return PushResult(True, target, lines)


def _git_out_ok(args: list[str], cwd: str | None, runner: Runner) -> str:
    cp = runner(["git", *args], cwd)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def list_worktrees(cwd: str | None = None, runner: Runner = _run) -> dict[str, str]:
    """Map worktree path -> short branch name ('' if detached/bare)."""
    out = _git(["worktree", "list", "--porcelain"], cwd, runner)
    result: dict[str, str] = {}
    cur: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
            result[cur] = ""
        elif line.startswith("branch ") and cur is not None:
            ref = line[len("branch "):]
            result[cur] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return result


def logical_repo_slug(cwd: str | None = None, runner: Runner = _run) -> str:
    """The shared bridge slug for this repo, derived from the MAIN worktree.

    Reuses the bridge's own resolver so bridge identity and ours cannot
    diverge: it honors an explicit `repo_slug:` in the main tree's
    .maw/config.yaml, else basename(main_root). This is the value seeded
    into every per-seat worktree so they all share one bridge_dir.
    """
    from bridge.cli import _resolve_repo_slug  # net→bridge (cf. registry import)

    return _resolve_repo_slug(main_worktree_root(cwd, runner))


# Top-level only (no leading whitespace): we manage a top-level repo_slug and
# replace only that. An indented `repo_slug` (nested under a `maw:` block) or a
# commented `# repo_slug:` is left untouched — never restructure a user's YAML.
# The bridge's own resolver is line-anchored/first-match with the same flat-key
# limitation (KD25 TODO in bridge/cli.py); resolving nested keys is its concern,
# not this seeder's.
_SLUG_LINE_RE = re.compile(r"^repo_slug:\s*(\S+)")

_SEED_HEADER = (
    "# Seeded by `net.worktree ensure` (backlog item 1). Pins the LOGICAL repo\n"
    "# slug so every per-seat worktree shares ONE bridge_dir\n"
    "# (~/.claude/bridge/<slug>); without it the pair's bridge channel splits\n"
    "# because git worktrees don't carry the untracked .maw/ config. Other keys\n"
    "# are preserved; keep repo_slug pointed at the logical (shared) repo.\n"
)


def _seed_bridge_identity(worktree: Path, slug: str) -> str:
    """Pin <worktree>/.maw/config.yaml's repo_slug to the logical slug.

    Idempotent and non-clobbering: creates the file if absent, appends the
    line if the file exists without one, replaces ONLY a stale repo_slug
    line, and leaves a correct file untouched. Returns one of
    'created' | 'set' | 'updated' | 'ok'. Creating the worktree's own .maw/
    also fixes repo_root resolution (the bridge's cwd-walk now stops at the
    worktree instead of climbing to ~/.maw).
    """
    maw_dir = worktree / ".maw"
    config = maw_dir / "config.yaml"
    line = f"repo_slug: {slug}\n"
    if not config.is_file():
        maw_dir.mkdir(parents=True, exist_ok=True)
        config.write_text(_SEED_HEADER + line, encoding="utf-8")
        return "created"
    text = config.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        m = _SLUG_LINE_RE.match(ln)
        if m:
            current = m.group(1).strip().strip('"').strip("'")
            if current == slug:
                return "ok"
            lines[i] = line  # replace a stale slug (surfaced to the operator)
            config.write_text("".join(lines), encoding="utf-8")
            return "updated"
    sep = "" if text.endswith("\n") or text == "" else "\n"
    config.write_text(text + sep + line, encoding="utf-8")
    return "set"


@dataclass
class EnsureResult:
    path: Path
    branch: str
    created: bool
    repo: str
    repo_common_dir: str
    slug: str
    seed_action: str


def occupants(target: Path) -> "list[str]":
    """Live registered sessions (any switchboard) whose repo_root or cwd sits in
    `target`, as human-readable lines. Empty when the worktree is free."""
    from net import registry

    target = target.resolve()
    out = []
    for nd, e in registry.live_entries_all_nets():
        for where in (e.repo_root, e.cwd):
            try:
                p = Path(where).resolve()
            except (OSError, TypeError, ValueError):
                continue
            if p == target or target in p.parents:
                pane = e.herdr_pane or e.tmux_pane or "-"
                net = "default" if nd.name == "switchboard" else nd.name.removeprefix("sw-")
                out.append(f"{e.name} (role {e.role}, pid {e.pid}, pane {pane}, net {net})")
                break
    return out


def seat_worktree_path(seat: str, cwd: str | None = None, runner: Runner = _run) -> Path:
    """Where `ensure` puts this seat's worktree. Side-effect free (reads git
    only), so a launcher's dry run can name the path without creating it."""
    common = repo_common_dir(cwd, runner)
    main_root = common.parent if common.name == ".git" else common
    return (worktree_home() / f"{main_root.name}-{seat}").resolve()


def ensure(seat: str, cwd: str | None = None, runner: Runner = _run,
           allow_occupied: bool = False,
           occupants_fn: "Callable[[Path], list[str]]" = occupants) -> EnsureResult:
    """Create-or-attach this seat's stable worktree; return where it lives.

    Idempotent: a second call (same seat, same repo) attaches to the existing
    worktree rather than creating a new one, and re-seeds its bridge identity
    so drift is repaired. Never clobbers — an unexpected on-disk or branch
    state is refused with an explanation so the operator decides, not the
    tool (the reversibility floor).
    """
    if seat not in SEATS:
        raise WorktreeError(f"unknown seat {seat!r}: expected one of {SEATS}")

    common = repo_common_dir(cwd, runner)
    main_root = common.parent if common.name == ".git" else common
    repo = main_root.name
    target = seat_worktree_path(seat, cwd, runner)
    branch = f"seat/{seat}"

    # Resolve registered worktrees to canonical paths so a symlinked or
    # trailing-slash path still matches.
    registered = {str(Path(p).resolve()): b for p, b in list_worktrees(cwd, runner).items()}

    if str(target) in registered:
        # Duplicate-pair guard (2026-09-22 ~21:00Z: a second grid-launched pair
        # into the live pair's worktrees and its HOLD reached our executor by role
        # name). A worktree a LIVE seat already occupies is not free to attach.
        held = [] if allow_occupied else occupants_fn(target)
        if held:
            raise WorktreeError(
                f"{target} is occupied by live session(s): {'; '.join(held)}. "
                f"A second seat here would share one tree and one bridge identity "
                f"with it. Stand the live seat down (or close its pane) first. For "
                f"a deliberate live handoff, pass --allow-occupied."
            )
        created = False
        branch = registered[str(target)] or branch
    elif target.exists():
        raise WorktreeError(
            f"{target} exists but git does not track it as a worktree; "
            f"refusing to clobber. Inspect it, then `git worktree prune` "
            f"and/or remove the directory manually before retrying."
        )
    else:
        branch_exists = _git_ok(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd, runner
        )
        if branch_exists and branch in registered.values():
            # The seat branch is checked out in some OTHER path — git would
            # refuse a second checkout of it, and silently moving it would be a
            # cross-seat hazard. Surface it instead.
            where = next(p for p, b in registered.items() if b == branch)
            raise WorktreeError(
                f"branch {branch!r} is already checked out at {where}; "
                f"a worktree for this seat may already exist there, or a stale "
                f"checkout needs `git worktree prune`."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if branch_exists:
            _git(["worktree", "add", str(target), branch], cwd, runner)
        else:
            _git(
                ["worktree", "add", "-b", branch, str(target), base_refs(cwd, runner)[1]],
                cwd, runner,
            )
        created = True

    slug = logical_repo_slug(cwd, runner)
    seed_action = _seed_bridge_identity(target, slug)
    return EnsureResult(
        path=target,
        branch=branch,
        created=created,
        repo=repo,
        repo_common_dir=str(common),
        slug=slug,
        seed_action=seed_action,
    )


def seat_worktrees(cwd: str | None = None, runner: Runner = _run) -> list[tuple[str, str, str]]:
    """(seat, path, branch) for any registered worktree on a seat/* branch."""
    out: list[tuple[str, str, str]] = []
    for path, br in sorted(list_worktrees(cwd, runner).items()):
        if br.startswith("seat/"):
            out.append((br[len("seat/"):], path, br))
    return out


@dataclass
class DoctorFinding:
    """One worktree-doctor check outcome. ok=False is a warning to address."""
    check: str      # "stale-base" | "stranded-artifacts" | "missing-venv"
    ok: bool
    detail: str


# Gitignored paths that are EXPECTED per-worktree noise, never "stranded data":
# environments, caches, runtime/plugin dirs. A path is noise if ANY of its path
# COMPONENTS is a known-noise dir (so a NESTED maw/net/__pycache__/ is caught, not
# just a top-level one) or it carries a noise suffix. Anything else ignored +
# present is surfaced for the operator to judge.
_NOISE_DIRS = frozenset((
    "venv", ".venv", "__pycache__", ".maw", ".git", ".claude", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".egg-info",
))
_NOISE_SUFFIXES = (".pyc", ".pyo", ".DS_Store", ".lock", ".egg-info")
_VENV_NAMES = ("venv", ".venv")


def _is_ignored_noise(path: str) -> bool:
    parts = [p for p in path.rstrip("/").split("/") if p]
    if any(p in _NOISE_DIRS for p in parts):
        return True
    return any(path.endswith(s) for s in _NOISE_SUFFIXES)


def _check_stale_base(cwd: str | None, runner: Runner) -> DoctorFinding:
    """#138 class: a lane branch cut from an OLD main merges stale. Flag when
    origin/<default> is not an ancestor of HEAD."""
    runner(["git", "fetch", "origin", "--quiet"], cwd)
    db = default_branch(cwd, runner)
    ref, gref = f"origin/{db}", remote_default_ref(db)
    if not _git_ok(["rev-parse", "--verify", "--quiet", f"{gref}^{{commit}}"], cwd, runner):
        return DoctorFinding("stale-base", True,
                             f"no {ref} to compare against (offline/local-only) — skipped")
    if _git_ok(["merge-base", "--is-ancestor", gref, "HEAD"], cwd, runner):
        return DoctorFinding("stale-base", True, f"branch is current with {ref}")
    behind = _git(["rev-list", "--count", f"HEAD..{gref}"], cwd, runner)
    return DoctorFinding(
        "stale-base", False,
        f"branch is NOT based on the latest {ref} ({behind} commit(s) behind) — "
        f"rebase before filing (net.worktree restack) to dodge the stale-base merge (#138)",
    )


def _check_stranded_artifacts(cwd: str | None, runner: Runner) -> DoctorFinding:
    """#147: gitignored files present in THIS worktree that a merge won't carry
    into the checkout that actually runs (an internal no-remote repo: training PDFs landed
    worktree-only; the main checkout would have indexed zero)."""
    cp = runner(["git", "status", "--ignored", "--porcelain"], cwd)
    stranded: list[str] = []
    for line in cp.stdout.splitlines():
        if not line.startswith("!! "):
            continue
        path = line[3:]
        if _is_ignored_noise(path):
            continue
        stranded.append(path)
    if not stranded:
        return DoctorFinding("stranded-artifacts", True, "no unexpected gitignored artifacts")
    shown = ", ".join(stranded[:10]) + (f" (+{len(stranded) - 10} more)" if len(stranded) > 10 else "")
    return DoctorFinding(
        "stranded-artifacts", False,
        f"gitignored, worktree-only artifact(s) that will NOT survive a merge into "
        f"the running checkout: {shown}",
    )


def _check_venv(cwd: str | None, runner: Runner) -> DoctorFinding:
    """#147: a worktree missing its venv FileNotFounds any test harness that
    hardcodes REPO_ROOT/venv (or /.venv)."""
    root = Path(_git(["rev-parse", "--show-toplevel"], cwd, runner))
    for name in _VENV_NAMES:
        if (root / name).exists():
            return DoctorFinding("missing-venv", True, f"venv present ({name})")
    return DoctorFinding(
        "missing-venv", False,
        f"no venv at the worktree root ({root}) — a harness hardcoding REPO_ROOT/venv "
        f"or /.venv will FileNotFound; create one or symlink the shared venv",
    )


def doctor(cwd: str | None = None, runner: Runner = _run) -> list[DoctorFinding]:
    """Bring-up health checks for a seat worktree (#147): stale base, stranded
    gitignored artifacts, and a missing venv. Read-only; each check is isolated
    so one failure does not sink the others."""
    findings: list[DoctorFinding] = []
    for check in (_check_stale_base, _check_stranded_artifacts, _check_venv):
        try:
            findings.append(check(cwd, runner))
        except (WorktreeError, subprocess.SubprocessError, OSError) as e:
            # A per-check failure must not sink the whole doctor: besides our own
            # WorktreeError, catch the git-invocation failures too — a 30s fetch
            # TimeoutExpired (subprocess.SubprocessError) or an OSError would
            # otherwise abort every remaining check (advisor note on #147).
            findings.append(DoctorFinding(check.__name__, False, f"check errored: {e}"))
    return findings


def cmd_ensure(args: argparse.Namespace) -> int:
    try:
        # The launcher (zshrc, CoS-owned) cannot grow a flag per case, so a
        # deliberate live handoff sets MAW_ALLOW_OCCUPIED=1 for that one launch.
        allow = args.allow_occupied or os.environ.get("MAW_ALLOW_OCCUPIED") == "1"
        res = ensure(args.seat, allow_occupied=allow)
    except WorktreeError as e:
        print(f"worktree ensure failed: {e}", file=sys.stderr)
        return 1
    # stdout is ONLY the path so the launcher can `cd "$(... ensure --seat x)"`.
    print(str(res.path))
    state = "created" if res.created else "attached"
    print(
        f"worktree {state}: {res.repo}-{args.seat} on {res.branch} "
        f"(logical repo {res.repo_common_dir}); bridge slug {res.slug!r} "
        f"[{res.seed_action}]",
        file=sys.stderr,
    )
    if res.seed_action == "updated":
        print(
            f"note: replaced a stale repo_slug in {res.path}/.maw/config.yaml "
            f"with the logical slug {res.slug!r} (kept other keys).",
            file=sys.stderr,
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        rows = seat_worktrees()
    except WorktreeError as e:
        print(f"worktree list failed: {e}", file=sys.stderr)
        return 1
    if not rows:
        print("no per-seat worktrees")
        return 0
    for seat, path, br in rows:
        print(f"{seat:<10} {br:<16} {path}")
    return 0


def cmd_branch(args: argparse.Namespace) -> int:
    try:
        base = new_branch(args.name, onto=args.onto)
    except WorktreeError as e:
        print(f"worktree branch failed: {e}", file=sys.stderr)
        return 1
    if args.onto is not None:
        sha = _git(["rev-parse", "--short", "HEAD"], None, _run)
        print(f"created {args.name} based on {base} ({sha}) — --onto, not origin/<default>")
        return 0
    print(f"created {args.name} based on fresh {base} (not the current branch)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        findings = doctor()
    except WorktreeError as e:
        print(f"worktree doctor failed: {e}", file=sys.stderr)
        return 1
    problems = [f for f in findings if not f.ok]
    for f in findings:
        print(f"[{'OK  ' if f.ok else 'WARN'}] {f.check}: {f.detail}")
    if problems:
        print(f"\n{len(problems)} issue(s) — address before filing.", file=sys.stderr)
    return 1 if problems else 0


def cmd_restack(args: argparse.Namespace) -> int:
    try:
        result = restack(args.branch, push=not args.no_push, onto=args.onto)
    except WorktreeError as e:
        print(f"restack refused: {e}", file=sys.stderr)
        return 1
    if result.onto_method in ("gh", "cherry"):
        print(f"{result.branch}: --onto auto found the old parent {result.onto} "
              f"via {result.onto_method}")
    print(f"{result.branch}: {result.detail}")
    if result.conflicts:
        for f in result.conflicts:
            print(f"  conflict: {f}")
    if result.ok and args.onto is not None:
        # #138: the child's base branch was merged+deleted; retarget the PR to main.
        print(f"  next: retarget the PR — `gh pr edit {result.branch} --base "
              f"{result.base.split('/')[-1]}`", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_push(args: argparse.Namespace) -> int:
    try:
        result = push(base=args.base, allow_merged=args.allow_merged,
                      allow_revert=args.allow_revert)
    except WorktreeError as e:
        print(f"push refused: {e}", file=sys.stderr)
        return 1
    for ln in result.lines:
        print(ln)
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="net.worktree", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    ens = sub.add_parser("ensure", help="create-or-attach this seat's worktree")
    ens.add_argument("--seat", required=True, choices=SEATS)
    ens.add_argument("--allow-occupied", action="store_true",
                     help="attach even though a LIVE seat already occupies the worktree "
                          "(a deliberate live handoff only)")
    ens.set_defaults(fn=cmd_ensure)

    lst = sub.add_parser("list", help="list per-seat worktrees")
    lst.set_defaults(fn=cmd_list)

    br = sub.add_parser(
        "branch",
        help="create+switch a new branch based on FRESH origin/<default> (#102)",
    )
    br.add_argument("name")
    br.add_argument("--onto", default=None,
                    help="base on this ref instead of origin/<default> (a deliberate "
                         "local-only baseline)")
    br.set_defaults(fn=cmd_branch)

    doc = sub.add_parser(
        "doctor",
        help="bring-up health checks: stale base, stranded artifacts, missing venv (#147)",
    )
    doc.set_defaults(fn=cmd_doctor)

    rs = sub.add_parser(
        "restack",
        help="rebase a branch onto FRESH origin/<default> + force-with-lease (#117)",
    )
    rs.add_argument("--onto", default=None,
                    help="STACKED (#138): OLD parent SHA the child branched from — "
                         "replays only the child's commits onto fresh origin/<default>. "
                         "The keyword `auto` discovers it (gh merged-parent PR, then "
                         "`git cherry`); a branch literally named auto: refs/heads/auto")
    rs.add_argument("branch", nargs="?", default=None,
                    help="branch to restack (default: current)")
    rs.add_argument("--no-push", action="store_true",
                    help="rebase only; do not push --force-with-lease")
    rs.set_defaults(fn=cmd_restack)

    ps = sub.add_parser(
        "push",
        help="push the current lane to origin/<branch> (sets upstream) with lane-safety "
             "checks: merged-PR refusal, silent-revert refusal, deleted-base warning (#354)",
    )
    ps.add_argument("--base", default=None,
                    help="the PR base you are about to target (gh pr create --base); "
                         "warns when it no longer exists on origin")
    ps.add_argument("--allow-merged", action="store_true",
                    help="push even though the branch's PR already merged")
    ps.add_argument("--allow-revert", action="store_true",
                    help="push an INTENTIONAL revert of recent main work (named in the output)")
    ps.set_defaults(fn=cmd_push)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
