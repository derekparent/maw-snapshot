"""Auto-WIP checkpoint — crash-safe snapshot of uncommitted work (item 7a).

A graceful stand-down checkpoints fine; an UNGRACEFUL exit (a stream timeout)
does not — executor2's mid-refactor work survived only because a peer manually
committed it ("one stray command from gone"). Item #1's per-seat worktrees
isolate the loss, but the work is still only as safe as the last commit.

This is the structural backstop: snapshot the dirty tree to a findable ref
WITHOUT touching the working tree, so a crash leaves recoverable state with zero
discipline required.

  It captures BOTH tracked changes AND untracked files (advisor Note 1: new
  work is untracked — a fresh module would be crash-lost otherwise). It uses a
  TEMP index (GIT_INDEX_FILE) so the real index and working tree are never
  touched: stage everything into the temp index → `write-tree` → `commit-tree`,
  then point `refs/maw-wip/<seat>` at the commit. .gitignore is respected
  (venv/, .maw/.runtime are excluded). Recovery: `git checkout <ref> -- .` (or
  cherry-pick / stash apply the commit).

Non-destructive and fail-safe: any error is a no-op (never disturbs the tree).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_SEAT_OK = ("advisor", "executor")


def _run(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> "subprocess.CompletedProcess[str]":
    full_env = {**os.environ, **env} if env else None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd, env=full_env)


def _safe_seat(seat: str | None) -> str:
    """A filesystem/ref-safe seat label. Falls back to 'session'."""
    if seat and seat in _SEAT_OK:
        return seat
    if seat and seat.replace("-", "").replace("_", "").isalnum():
        return seat
    return "session"


@dataclass
class CheckpointResult:
    created: bool
    sha: str
    ref: str
    detail: str


def checkpoint(seat: str | None = None, cwd: str | None = None, runner: Runner = _run) -> CheckpointResult:
    """Snapshot the dirty tree (tracked AND untracked) to refs/maw-wip/<seat>.

    Captures untracked work too (advisor Note 1: new work IS untracked — a fresh
    module crash-lost while `git stash create` reported 'clean'). Uses a TEMP
    index (GIT_INDEX_FILE) so the real index and working tree are never touched:
    stage everything into the temp index → write-tree → commit-tree. .gitignore
    is respected, so ignored files (venv/, .maw/.runtime) are excluded.

    No-op (created=False) when the tree is clean or git is unavailable. Never
    raises — a checkpoint backstop must not itself break a stand-down.
    """
    ref = f"refs/maw-wip/{_safe_seat(seat)}"
    try:
        st = runner(["git", "status", "--porcelain"], cwd)
    except (OSError, subprocess.SubprocessError) as e:
        return CheckpointResult(False, "", ref, f"git unavailable: {e}")
    if st.returncode != 0:
        return CheckpointResult(False, "", ref, "not a git repo (or git error)")
    if not st.stdout.strip():
        return CheckpointResult(False, "", ref, "clean tree (tracked + untracked) — nothing to snapshot")

    # HEAD as the snapshot's parent (absent on an unborn branch → parentless).
    head_cp = runner(["git", "rev-parse", "HEAD"], cwd)
    head = head_cp.stdout.strip() if head_cp.returncode == 0 else ""

    fd, idx = tempfile.mkstemp(prefix="maw-wip-idx-")
    os.close(fd)
    os.unlink(idx)  # GIT_INDEX_FILE must not pre-exist as an empty (invalid) index
    env = {"GIT_INDEX_FILE": idx}
    try:
        if head:
            runner(["git", "read-tree", head], cwd, env)  # seed temp index from HEAD
        add = runner(["git", "add", "-A"], cwd, env)       # stage tracked + untracked
        if add.returncode != 0:
            return CheckpointResult(False, "", ref, f"add failed: {add.stderr.strip()}")
        wt = runner(["git", "write-tree"], cwd, env)
        if wt.returncode != 0 or not wt.stdout.strip():
            return CheckpointResult(False, "", ref, f"write-tree failed: {wt.stderr.strip()}")
        tree = wt.stdout.strip()
        msg = f"maw-wip checkpoint ({_safe_seat(seat)})"
        commit_cmd = ["git", "commit-tree", tree, "-m", msg]
        if head:
            commit_cmd[2:2] = ["-p", head]
        ct = runner(commit_cmd, cwd)
        if ct.returncode != 0 or not ct.stdout.strip():
            return CheckpointResult(False, "", ref, f"commit-tree failed: {ct.stderr.strip()}")
        sha = ct.stdout.strip()
        up = runner(["git", "update-ref", ref, sha], cwd)
        if up.returncode != 0:
            return CheckpointResult(False, sha, ref, f"update-ref failed: {up.stderr.strip()}")
        return CheckpointResult(True, sha, ref, "snapshotted tracked + untracked")
    except (OSError, subprocess.SubprocessError) as e:
        return CheckpointResult(False, "", ref, f"git error: {e}")
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


def cmd_checkpoint(args: argparse.Namespace) -> int:
    res = checkpoint(args.seat)
    if res.created:
        print(f"checkpoint: {res.ref} -> {res.sha[:12]} ({res.detail}); "
              f"recover with `git checkout {res.sha[:12]} -- .` (or git stash apply)")
    else:
        print(f"checkpoint: no snapshot ({res.detail})")
    return 0  # always 0 — a backstop never fails the caller


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="succession.checkpoint", description=__doc__)
    p.add_argument("--seat", default=None, help="advisor|executor (else 'session')")
    p.set_defaults(fn=cmd_checkpoint)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
