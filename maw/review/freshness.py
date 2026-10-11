"""Pre-file freshness gate for a review request (#197).

Two shapes cost a pair real cycles, and both are visible BEFORE a request is
filed:

  STALE BASE   — the branch was cut from a main that has since moved, so
                 `origin/<default>` is not an ancestor of HEAD. The reviewer
                 probe-merges, finds drift, the builder restacks, the suite
                 runs again. Refuse up front and print the restack command.

  SILENT REVERT — the `git reset --soft origin/main` + commit trap: the branch's
                 parent IS current main (so the ancestry check passes), but the
                 TREE still holds the old version of files recent main PRs
                 changed. Merging it silently undoes merged work, suite green.
                 Sniff it: a branch blob equal to the PRE-image of a recent
                 first-parent main change (and unequal to main's current blob)
                 is a revert of that change; a deleted file a recent main commit
                 ADDED is too.

Deterministic and cheap: one `git log --raw` per changed path (the raw line
carries the pre-image blob, so no per-commit lookups). Offline / no origin ref
warns and proceeds — the gate never blocks on missing information.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable

from net.worktree import WorktreeError, default_branch  # noqa: F401 (re-exported for cli)
from selfpath import MAW_RUN

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

REFUSE_EXIT = 3
DEFAULT_DEPTH = 30
_NULL_BLOB = "0" * 40
RESTACK_CMD = f"{MAW_RUN} net.worktree restack"


@dataclass
class Revert:
    path: str
    commit: str
    subject: str
    kind: str  # "reverts" | "deletes-added"

    def describe(self) -> str:
        if self.kind == "deletes-added":
            return f"{self.path} deletes the file {self.commit[:7]} ({self.subject}) added"
        return f"{self.path} reverts {self.commit[:7]} ({self.subject})"


@dataclass
class Freshness:
    ref: str                 # origin/<default>, the PRINTED name
    git_ref: str = ""        # refs/remotes/origin/<default>, the name git is given
    ref_sha: str = ""        # empty when the ref is missing (offline/local-only)
    current: bool = False    # ref is an ancestor of HEAD
    behind: int = 0
    reverts: list[Revert] = field(default_factory=list)
    warning: str = ""
    fetch_error: str = ""    # non-empty when the fetch failed: ref_sha may be stale

    @property
    def stale(self) -> bool:
        return bool(self.ref_sha) and not self.current

    def line(self) -> str:
        if not self.ref_sha:
            return f"unchecked (no {self.ref}: offline/local-only)"
        if not self.current:
            return f"STALE: {self.behind} commit(s) behind {self.ref}@{self.ref_sha[:7]}"
        if self.fetch_error:
            return (f"current with {self.ref}@{self.ref_sha[:7]} as last fetched — "
                    f"FETCH FAILED, the remote may be ahead")
        return f"current with {self.ref}@{self.ref_sha[:7]}"


def _ok(runner: Runner, args: list[str], cwd: str | None) -> bool:
    return runner(["git", *args], cwd).returncode == 0


def _out(runner: Runner, args: list[str], cwd: str | None) -> str:
    cp = runner(["git", *args], cwd)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _blob(runner: Runner, rev: str, path: str, cwd: str | None) -> str:
    """Blob SHA of `path` at `rev`, or '' when the path is absent there."""
    out = _out(runner, ["ls-tree", "--full-tree", rev, "--", path], cwd)
    for ln in out.splitlines():
        meta, _, name = ln.partition("\t")
        parts = meta.split()
        if name == path and len(parts) == 3 and parts[1] == "blob":
            return parts[2]
    return ""


def _changed_paths(runner: Runner, ref: str, cwd: str | None, head: str = "HEAD") -> list[str]:
    """Paths whose content differs between ref and `head` — both the three-dot
    (what the branch did since the merge-base) and the two-dot tree diff (what
    merging would change), deduped. They coincide when the ref is an ancestor."""
    seen: dict[str, None] = {}
    for spec in ([f"{ref}...{head}"], [ref, head]):
        out = _out(runner, ["diff", "--name-only", "--no-renames", *spec], cwd)
        for ln in out.splitlines():
            if ln.strip():
                seen.setdefault(ln.strip(), None)
    return list(seen)


def sniff_reverts(
    ref: str, runner: Runner, cwd: str | None = None, depth: int = DEFAULT_DEPTH,
    head: str = "HEAD",
) -> list[Revert]:
    """Changed paths whose branch content (at `head`) restores a recent main
    change's pre-image. `head` defaults to the checkout; the merge wrapper
    passes a PR's head SHA (#355)."""
    found: list[Revert] = []
    for path in _changed_paths(runner, ref, cwd, head):
        head_blob = _blob(runner, head, path, cwd)
        ref_blob = _blob(runner, ref, path, cwd)
        if head_blob == ref_blob:
            continue
        log = _out(runner, [
            "log", "--first-parent", "--diff-merges=first-parent", "--no-renames",
            "--raw", "--no-abbrev", f"-n{depth}", "--format=%x01%H%x09%s",
            ref, "--", path,
        ], cwd)
        commit = subject = ""
        for ln in log.splitlines():
            if ln.startswith("\x01"):
                commit, _, subject = ln[1:].partition("\t")
                continue
            if not ln.startswith(":") or not commit:
                continue
            meta, _, name = ln.partition("\t")
            if name != path:
                continue
            parts = meta.split()
            if len(parts) < 5:
                continue
            pre = parts[2]
            if head_blob and pre != _NULL_BLOB and head_blob == pre:
                found.append(Revert(path, commit, subject, "reverts"))
                break
            if not head_blob and pre == _NULL_BLOB and ref_blob:
                found.append(Revert(path, commit, subject, "deletes-added"))
                break
    return found


def check(
    runner: Runner, cwd: str | None = None, depth: int = DEFAULT_DEPTH, fetch: bool = True,
) -> Freshness:
    """Fetch (best effort), then measure HEAD against origin/<default>.

    The revert sniff runs only on a CURRENT branch: a stale branch is already
    refused, and its tree trivially "reverts" everything main did since it was
    cut — a sniff there would list noise, not the trap.
    """
    fetch_error = ""
    if fetch:
        cp = runner(["git", "fetch", "origin", "--quiet"], cwd)
        if cp.returncode != 0:
            fetch_error = ((cp.stderr or "").strip().splitlines() or [f"exit {cp.returncode}"])[0]
    db = default_branch(cwd, runner)
    # Printed as origin/<default>; given to git as the full ref, which a tag
    # named origin/<default> cannot shadow.
    ref, gref = f"origin/{db}", f"refs/remotes/origin/{db}"
    fr = Freshness(ref=ref, git_ref=gref, fetch_error=fetch_error)
    fr.ref_sha = _out(runner, ["rev-parse", "--verify", "--quiet", f"{gref}^{{commit}}"], cwd)
    if fr.ref_sha and fetch_error:
        # A failed fetch leaves the LAST-fetched ref, which can be stale: measured
        # against it, a stale branch reads as current. Say so; never stay silent.
        fr.warning = (f"git fetch origin failed ({fetch_error}) — measured against the "
                      f"last-fetched {ref}@{fr.ref_sha[:7]}, which may be stale")
    if not fr.ref_sha:
        fr.warning = (f"no {ref} to compare against (offline/local-only) — freshness "
                      f"and revert checks skipped")
        return fr
    fr.current = _ok(runner, ["merge-base", "--is-ancestor", gref, "HEAD"], cwd)
    if not fr.current:
        try:
            fr.behind = int(_out(runner, ["rev-list", "--count", f"HEAD..{gref}"], cwd) or 0)
        except ValueError:
            fr.behind = 0
        return fr
    fr.reverts = sniff_reverts(gref, runner, cwd, depth)
    return fr


def stale_message(fr: Freshness) -> str:
    return (
        f"branch is NOT based on the latest {fr.ref} ({fr.behind} commit(s) behind) — "
        f"rebase before filing (net.worktree restack) to dodge the stale-base merge (#138):\n"
        f"  {RESTACK_CMD}"
    )


def revert_message(fr: Freshness) -> str:
    lines = [f"the branch silently reverts recent {fr.ref} work "
             f"(the `reset --soft` trap: parent is current, tree is old):"]
    lines += [f"  - {r.describe()}" for r in fr.reverts]
    lines.append("rebuild the branch on a fresh worktree off the current main, or — for an "
                 "INTENTIONAL revert — re-run with --allow-revert (the request will carry a "
                 "REVERTS: line for the reviewer)")
    return "\n".join(lines)
