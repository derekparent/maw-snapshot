"""Substrate freshness — is this repo's transplant older than current doctrine?

WHY (#201, the 2026-07-23 incident: the operator found an iOS repo's pair a
week behind current doctrine). A repo's substrate is transplanted
once and then sits. When doctrine moves in the plugin, the transplanted repo does
not: that pair ground on June-era doctrine for a week, and the stale text made a
retired ritual read as business-as-usual instead of an anomaly worth escalating.
Nothing in the system said "you are behind".

WHY THE EXISTING STAMP COULD NOT SAY IT. `state.json` already recorded
`substrate_version`, but it is the plugin MANIFEST version — a label that
read `0.6.0-pre-implementation` from before any of this doctrine existed until
the first snapshot, and that still moves only when someone edits it by hand.
Every install between two such edits stamps the same string, so a comparison
against it cannot tell an old transplant from a new one.
That iOS repo proves it: stamped `0.6.0-pre-implementation`, installed
2026-06-12, six weeks behind, and the stamp says exactly what a fresh install
says.

WHAT MOVES INSTEAD: the plugin's own git commit. `/maw:init` records the SHA and
the COMMIT DATE of the plugin it transplanted from, and bring-up compares that
date against DOCTRINE_EPOCH — the date of the last change that made older
substrate text WRONG rather than merely older. A stamp older than the epoch, or
no stamp at all, is a loud warning at SessionStart, not a silent drift.

FAIL-OPEN EVERYWHERE. This is a warning path: an unreadable state file, a plugin
that is not a git checkout, an unparseable date — all produce NO warning rather
than a crash or a false alarm. It must never block a session start.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from init.state import StateError, read_state

# THE LAST BREAKING DOCTRINE CHANGE. Substrate transplanted before this date can
# carry text that is WRONG, not merely old — the reason a warning is worth a
# human's attention at all.
#
# 2026-07-24 (`1f08cda`): the strict-echo DEFAULT was retired, so every
# repo is no-echo. Substrate older than this can still instruct a seat to demand
# a per-batch echo that no longer exists — the exact nag loop that made the
# iOS repo's pair comply with a retired ritual for a week.
#
# BUMP THIS ONLY WITH THE DOCTRINE CHANGE ITSELF, in the same PR. Bumping it for
# an ordinary release turns the warning into noise, and a warning everyone learns
# to ignore is worse than no warning.
DOCTRINE_EPOCH = "2026-07-24"
DOCTRINE_EPOCH_REASON = (
    "the strict-echo default was retired on 2026-07-24; substrate older than this can "
    "still demand a per-batch merge echo that no longer exists"
)


def plugin_root() -> Path:
    """The plugin this process is running from.

    CLAUDE_PLUGIN_ROOT when a seat command set it (it resolves to the pinned
    worktree); otherwise this package's own parent, which is what a
    `python3 -m init.substrate` run against the tree means.
    """
    env = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


# The sentinel `repin.check sync` writes into the cache copy, naming the commit
# that copy was made from. Duplicated as a literal rather than imported from
# repin.check: init must not grow a dependency on the deploy tooling to answer a
# freshness question.
SENTINEL_NAME = ".maw-pinned-sha"
PINNED_WORKTREE = Path.home() / ".maw" / "plugin-main"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# The stamp the release export writes into the plugin dir: the source commit
# and its date. A marketplace install is a plain copy of maw/ with no git and
# no repin sentinel, so this file is the only thing that can date it.
RELEASE_STAMP_NAME = ".maw-release-stamp"
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
# Exactly what the export writes. A stamp is the one source nothing else vouches
# for, so anything looser (a bare day, an offset, a trailing newline) is no stamp.
_STAMP_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_STAMP_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _is_this_checkout(root: Path) -> bool:
    """True when `root` is a git checkout's top level, or the plugin dir right
    under one. A plugin copy that merely sits INSIDE some other repository (a
    home directory kept in git, with the cache under it) is not a checkout of
    maw, and that repository's HEAD says nothing about the copy."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if cp.returncode != 0 or not cp.stdout.strip():
        return False
    try:
        top = Path(cp.stdout.strip()).resolve()
        here = root.resolve()
    except OSError:
        return False
    return top in (here, here.parent)


def _release_stamp(root: Path) -> tuple[str, str]:
    """(sha, ISO date) from the export's stamp file, or ("", "") unless both
    are well formed: half a stamp must not read as a dated one."""
    try:
        data = json.loads((root / RELEASE_STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    sha, committed_at = data.get("source_sha"), data.get("committed_at")
    if not (isinstance(sha, str) and _FULL_SHA_RE.fullmatch(sha)):
        return "", ""
    if not (isinstance(committed_at, str) and _STAMP_DATE_RE.fullmatch(committed_at)):
        return "", ""
    try:
        datetime.strptime(committed_at, _STAMP_DATE_FMT)  # a real instant, not 99-99
    except ValueError:
        return "", ""
    return sha, committed_at


def _git_commit(root: Path) -> tuple[str, str]:
    if not _is_this_checkout(root):
        return "", ""
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H %cI"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if cp.returncode != 0:
        return "", ""
    parts = cp.stdout.strip().split()
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def _date_of_commit(sha: str, where: Path) -> str:
    """The commit date of `sha` according to a git checkout that has it."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(where), "log", "-1", "--format=%cI", sha],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def plugin_commit(root: Path | None = None) -> tuple[str, str]:
    """(sha, ISO commit date) of the plugin this process runs from, or ("", "").

    Three sources, in order:
      1. git, when the root IS a checkout (the pinned worktree, or the tree);
      2. the cache copy's `.maw-pinned-sha` sentinel, dated by asking the pinned
         worktree about that commit;
      3. the `.maw-release-stamp` file the release export writes into maw/,
         which is what a marketplace install of a published snapshot carries.

    (2) exists because a cache copy is not a git dir. Seats run from the pin
    today, but if `CLAUDE_PLUGIN_ROOT` ever resolves to the cache again (the #134
    class), a git-only read would stamp every new install as unstamped and turn
    the drift warning into permanent noise — a warning that always fires is a
    warning nobody reads.

    A sentinel naming a commit the pin does not have yields (sha, "") — the
    identity is known, the date is not, and an undated stamp reads as
    un-provable rather than as current. Anything unreadable is ("", "").
    """
    the_root = root if root is not None else plugin_root()
    sha, committed_at = _git_commit(the_root)
    if sha:
        return sha, committed_at
    try:
        raw = (the_root / SENTINEL_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if _SHA_RE.match(raw):
        return raw, _date_of_commit(raw, PINNED_WORKTREE)
    return _release_stamp(the_root)


def _date_of(iso: str) -> str:
    """The YYYY-MM-DD prefix of an ISO timestamp, or "" if it is not one."""
    text = (iso or "").strip()
    if len(text) < 10:
        return ""
    date = text[:10]
    parts = date.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""
    return date


def drift_warning(repo_root: Path, epoch: str = DOCTRINE_EPOCH) -> str | None:
    """The loud line for a repo whose substrate predates current doctrine.

    None when the substrate is current, when the repo has no substrate at all
    (that is `/maw:status`'s message, not a drift warning), or when anything is
    unreadable — this path may only ADD a warning, never block or crash.
    """
    try:
        state = read_state(repo_root)
    except (StateError, OSError):
        return None
    if state is None:
        return None

    stamped = _date_of(state.substrate_committed_at)
    if not stamped:
        return (
            f"[maw] SUBSTRATE DRIFT: this repo's substrate carries no plugin-commit "
            f"stamp (installed {state.installed_at or 'unknown'}, version "
            f"{state.substrate_version or '?'}), so it CANNOT be shown to carry "
            f"current doctrine. Doctrine last changed {epoch} — "
            f"{DOCTRINE_EPOCH_REASON}. Run `/maw:init --upgrade` before trusting "
            f"any substrate text in this repo, and treat a tool demanding ceremony "
            f"your memory calls normal as an anomaly to escalate, not a ritual."
        )
    if stamped < epoch:
        return (
            f"[maw] SUBSTRATE DRIFT: transplanted from plugin commit "
            f"{state.substrate_sha[:7] or '?'} of {stamped}, which is OLDER than "
            f"the last breaking doctrine change ({epoch}) — {DOCTRINE_EPOCH_REASON}. "
            f"Run `/maw:init --upgrade`. Until then, substrate text in this repo "
            f"may instruct you to follow a retired ritual."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="init.substrate",
        description="warn when a repo's substrate predates the current doctrine (#201)",
    )
    p.add_argument("--repo-root", default=".")
    p.add_argument("--stamp", action="store_true",
                   help="print this plugin's commit stamp as JSON instead of checking")
    ns = p.parse_args(argv)

    if ns.stamp:
        sha, committed_at = plugin_commit()
        print(json.dumps({"sha": sha, "committed_at": committed_at}))
        return 0

    warning = drift_warning(Path(ns.repo_root))
    if warning:
        print(warning)
    return 0  # always: a freshness notice must never fail a session start


if __name__ == "__main__":
    raise SystemExit(main())
