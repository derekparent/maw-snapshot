"""`maw repin` — the pinned-worktree plugin model (foundation fix, copy variant).

ROOT CAUSE this replaces: maw is installed from a *directory* marketplace
source, which COPIES `./maw` into
`~/.claude/plugins/cache/maw/maw/<version>/`. Slash commands + hooks resolve
`CLAUDE_PLUGIN_ROOT` to that copy. `claude plugin install` keys its re-copy on
the plugin.json *version label* — and that label
("0.6.0-pre-implementation" at the time) almost never moves. So a reinstall NO-OPs and the cache
silently goes stale, or (verified live 2026-06-17) gets emptied while the
registry still claims the plugin is installed.

THE MODEL (live-validated 2026-06-18 against the real environment):
  - `~/.maw/plugin-main` is a git worktree of the maw repo pinned to `main`
    (a pure mirror of origin/main; never edited by hand). UNCHANGED — this is
    the half that fixes the contended-editing-tree fault, and it works.
  - The cache version dir `~/.claude/plugins/cache/maw/maw/<version>` is a REAL
    DIRECTORY COPY of `~/.maw/plugin-main/maw`. NOT a symlink.
  - A sentinel file `<version_dir>/.maw-pinned-sha` records the pinned
    worktree's HEAD sha at the moment of the copy. The cache is IN_SYNC iff that
    sentinel matches the pinned HEAD.
  - "Refresh after a merge" = (1) fast-forward the pinned worktree, (2) rm the
    cache version dir contents + `cp -R` the pinned `maw/` back in, (3) rewrite
    the sentinel. NEVER `claude plugin install` (it no-ops on the unchanged
    label, or empties the cache — that was the #36 fault).

WHY A REAL COPY, NOT A SYMLINK (the empirical correction, validated 2026-06-18):
  - A SYMLINK at the cache version dir is REMOVED by Claude Code on the next
    full session start: it "repairs" the non-directory plugin entry, and the
    no-op `claude plugin install` then leaves nothing. Confirmed twice.
  - A REAL COPY at the SAME (unchanged) version label SURVIVES session starts —
    confirmed across 3 fresh `claude -p` sessions, including one launched from
    the maw repo dir. `/maw:status` resolved and ran each time.
  - `known_marketplaces.json` `maw.autoUpdate` reverts to `true` on its own
    (claude rewrites it); it is NOT a lever and is not relied on. The copy
    survives regardless; setup repoints the marketplace source to the pinned
    worktree so any claude-initiated auto re-sync copies from clean main
    (defense in depth), but that is benign either way.

IMPORTANT: the plugin.json `version` label is IRRELEVANT. We bypass
`claude plugin install` entirely — the copy IS the cache content, written by
this module's `refresh_cache`. The label only names the directory.

A SYMLINK at the cache version dir classifies NEEDS_RECOPY and `check` FAILS on
it: claude will silently remove a symlink on the next session start, so a
symlinked cache is a latent empty cache. It must be a real copy.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------
# Constants — the pinned-worktree model
# --------------------------------------------------------------------------

# The pinned worktree of the maw repo (pure mirror of origin/main, never edited).
PINNED_WORKTREE = Path("~/.maw/plugin-main")
# The plugin subdir inside the pinned worktree — the copy SOURCE.
PINNED_PLUGIN_SUBDIR = "maw"
# The cache version dir(s): one per installed version label. We copy into each.
CACHE_BASE = Path("~/.claude/plugins/cache/maw/maw")
# Sentinel file written into each cache version dir recording the pinned HEAD
# sha at the moment of the copy. IN_SYNC iff its content == pinned HEAD.
SENTINEL_NAME = ".maw-pinned-sha"

# A glob, not a filename: a clone without the operator's deploy runbook must
# never be told to read it, so the pointer is named only where the file is.
RUNBOOK_GLOB = "docs/*deploy-runbook*.md"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class RepinError(Exception):
    pass


class CacheState(Enum):
    """Classification of the cache version dir relative to the pinned model."""

    IN_SYNC = "IN_SYNC"            # real dir + sentinel sha == pinned HEAD
    STALE = "STALE"               # real dir, sentinel missing or sha mismatch
    NEEDS_RECOPY = "NEEDS_RECOPY"  # a SYMLINK — claude will remove it; must be a copy
    BROKEN = "BROKEN"             # missing/empty dir, or no plugin manifest


# `git rev-parse --local-env-vars`. A post-merge hook fired from a merge in a
# linked worktree exports GIT_DIR/GIT_WORK_TREE; inherited, they override
# `git -C ~/.maw/plugin-main` and the ff-only merge runs against the CALLING
# worktree instead of the pin (#206).
GIT_LOCAL_ENV_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV_VARS}


def _run(cmd: list[str], cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=cwd, env=_clean_env()
    )


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def _expand(p: Path, home: Path) -> Path:
    """Resolve a ``~``-anchored constant against an injectable home."""
    s = str(p)
    if s.startswith("~/"):
        return home / s[2:]
    if s == "~":
        return home
    return p


def pinned_worktree_path(home: Path | None = None) -> Path:
    return _expand(PINNED_WORKTREE, _home(home))


def _runbook_roots(home: Path | None = None) -> tuple[Path, ...]:
    return (pinned_worktree_path(home), Path(__file__).resolve().parents[2])


def runbook_path(home: Path | None = None) -> str | None:
    """The deploy runbook, relative to the tree that holds it, or None.

    Looked for in the pinned worktree, then in the tree this module runs from.
    A tree counts only with exactly ONE match: with several, naming one would
    be a guess.
    """
    for root in _runbook_roots(home):
        try:
            found = sorted(f for f in root.glob(RUNBOOK_GLOB) if f.is_file())
        except OSError:
            continue
        if len(found) == 1:
            return found[0].relative_to(root).as_posix()
    return None


def pinned_plugin_path(home: Path | None = None) -> Path:
    return pinned_worktree_path(home) / PINNED_PLUGIN_SUBDIR


def cache_base_path(home: Path | None = None) -> Path:
    return _expand(CACHE_BASE, _home(home))


def pinned_head(runner: Runner = _run, home: Path | None = None) -> str | None:
    """Return the pinned worktree's HEAD sha, or None if it can't be determined."""
    wt = pinned_worktree_path(_home(home))
    if not wt.exists():
        return None
    try:
        cp = runner(["git", "-C", str(wt), "rev-parse", "HEAD"], str(wt))
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    sha = (cp.stdout or "").strip()
    return sha or None


# --------------------------------------------------------------------------
# Cache copy health
# --------------------------------------------------------------------------

class CacheReport:
    """Result of classifying a single cache version dir."""

    def __init__(self, state: CacheState, version_dir: Path | None,
                 sentinel_sha: str | None, detail: str) -> None:
        self.state = state
        self.version_dir = version_dir
        self.sentinel_sha = sentinel_sha  # the sha recorded in the version dir
        self.detail = detail

    @property
    def healthy(self) -> bool:
        return self.state is CacheState.IN_SYNC


def cache_status(home: Path | None = None,
                 runner: Runner = _run) -> CacheReport:
    """Classify the cache version dir against the pinned-worktree COPY model.

    The four states:
      IN_SYNC      — the version dir is a REAL directory whose
                     ``.maw-pinned-sha`` sentinel equals the pinned HEAD.
      STALE        — a real dir, but the sentinel is missing or its sha does not
                     match the pinned HEAD (a merge happened; recopy is due).
      NEEDS_RECOPY — the version dir is a SYMLINK. Claude Code removes a symlink
                     at the next session start, leaving an empty cache; it MUST
                     be replaced with a real copy. `check` fails on this.
      BROKEN       — no version dir exists, the cache base is empty, or a real
                     dir has no plugin manifest (cache emptied / corrupt).

    If multiple version dirs exist, the worst non-healthy one is reported so
    `check` fails loudly until the cache is clean.
    """
    h = _home(home)
    base = cache_base_path(h)
    want_sha = pinned_head(runner, h)

    if not base.exists():
        return CacheReport(CacheState.BROKEN, None, None,
                           f"cache base does not exist: {base}")

    entries = sorted(base.iterdir())
    if not entries:
        return CacheReport(CacheState.BROKEN, None, None,
                           f"cache base is empty (no version dir): {base}")

    reports = [_classify_version_dir(vd, want_sha) for vd in entries]

    # Prefer reporting a non-healthy state (worst-case) so check can't pass while
    # a stray/broken/symlinked sibling dir lingers.
    severity = {
        CacheState.BROKEN: 3,
        CacheState.NEEDS_RECOPY: 3,
        CacheState.STALE: 2,
        CacheState.IN_SYNC: 0,
    }
    reports.sort(key=lambda r: severity[r.state], reverse=True)
    return reports[0]


def _read_sentinel(vd: Path) -> str | None:
    sentinel = vd / SENTINEL_NAME
    if not sentinel.is_file():
        return None
    try:
        return sentinel.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _classify_version_dir(vd: Path, want_sha: str | None) -> CacheReport:
    # A symlink is the one thing this model forbids: claude removes it on the
    # next session start. Classify NEEDS_RECOPY regardless of where it points.
    if vd.is_symlink():
        return CacheReport(CacheState.NEEDS_RECOPY, vd, None,
                           f"cache version dir is a SYMLINK ({vd}); claude removes "
                           "symlinked plugin entries on session start — must be a "
                           "real copy. Run setup/sync to recopy.")
    if not vd.is_dir():
        return CacheReport(CacheState.BROKEN, vd, None,
                           f"not a dir or symlink: {vd}")

    # Real directory. Require a plugin manifest before any healthy/stale verdict —
    # an emptied dir is BROKEN, not merely stale.
    if not (vd / ".claude-plugin" / "plugin.json").exists():
        return CacheReport(CacheState.BROKEN, vd, None,
                           f"real dir but no plugin manifest "
                           f"(.claude-plugin/plugin.json): {vd}")

    sentinel_sha = _read_sentinel(vd)
    if sentinel_sha is None:
        return CacheReport(CacheState.STALE, vd, None,
                           f"real dir but sentinel {SENTINEL_NAME} missing/unreadable "
                           f"(cannot prove it matches pinned main): {vd}")
    if want_sha is None:
        # We have a sentinel but couldn't read the pinned HEAD — cannot prove
        # IN_SYNC. Treat as STALE (action: investigate the pinned worktree).
        return CacheReport(CacheState.STALE, vd, sentinel_sha,
                           f"sentinel sha={sentinel_sha[:12]} but pinned HEAD "
                           f"unreadable — cannot confirm sync: {vd}")
    if sentinel_sha == want_sha:
        return CacheReport(CacheState.IN_SYNC, vd, sentinel_sha,
                           f"real copy, sentinel matches pinned HEAD "
                           f"({sentinel_sha[:12]}): {vd}")
    return CacheReport(CacheState.STALE, vd, sentinel_sha,
                       f"sentinel sha={sentinel_sha[:12]} != pinned HEAD "
                       f"{want_sha[:12]} (recopy due): {vd}")


def refresh_cache(home: Path | None = None, runner: Runner = _run) -> Path:
    """Recopy the pinned plugin into the cache version dir + write the sentinel.

    Steps, per the live-validated model:
      1. resolve the pinned HEAD sha (the sentinel value).
      2. pick the cache version dir (the existing one; if several, the existing
         real dirs are each refreshed; if none, derive a label from plugin.json).
      3. rm the version dir contents (or the whole symlinked entry) and
         ``cp -R`` the pinned ``maw/`` into it.
      4. write ``<version_dir>/.maw-pinned-sha`` = pinned HEAD.

    NEVER calls `claude plugin install`. Returns the (last) refreshed version
    dir. Raises RepinError if the pinned plugin source is missing.
    """
    h = _home(home)
    src = pinned_plugin_path(h)
    if not (src / ".claude-plugin" / "plugin.json").exists():
        raise RepinError(
            f"pinned plugin source missing/empty: {src} — "
            "run repin/setup_pinned_plugin.sh first"
        )
    sha = pinned_head(runner, h)
    if not sha:
        raise RepinError(
            f"could not determine pinned HEAD sha at {pinned_worktree_path(h)}"
        )

    base = cache_base_path(h)
    base.mkdir(parents=True, exist_ok=True)

    targets = [e for e in sorted(base.iterdir())] if base.exists() else []
    if not targets:
        # Empty cache base — derive a label from the pinned plugin.json so
        # CLAUDE_PLUGIN_ROOT has a dir to resolve. The label is cosmetic.
        targets = [base / _derive_version_label(src)]

    last: Path | None = None
    for vd in targets:
        _recopy_one(src, vd, sha)
        last = vd
    assert last is not None
    return last


def _recopy_one(src: Path, vd: Path, sha: str) -> None:
    # Remove whatever is there (symlink, file, or old dir) atomically-ish, then
    # copy the pinned plugin tree in as a REAL directory.
    if vd.is_symlink() or vd.is_file():
        vd.unlink()
    elif vd.is_dir():
        shutil.rmtree(vd)
    shutil.copytree(src, vd)
    (vd / SENTINEL_NAME).write_text(sha + "\n", encoding="utf-8")


def _derive_version_label(src: Path) -> str:
    """Pull the version string out of plugin.json (cosmetic dir name only)."""
    manifest = src / ".claude-plugin" / "plugin.json"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0-pinned"
    import re
    m = re.search(r'"version"\s*:\s*"([^"]*)"', text)
    return (m.group(1) if m else "") or "0.0.0-pinned"


# --------------------------------------------------------------------------
# Pinned worktree health
# --------------------------------------------------------------------------

class PinnedReport:
    """Result of inspecting the pinned worktree's git state.

    ``current`` is the only health bit the pinned model cares about: the
    checked-out commit of the pinned worktree EQUALS origin/main. The branch
    NAME is irrelevant — a detached HEAD sitting exactly at origin/main is fully
    current (this is the normal state when ``main`` is already checked out in the
    primary tree, so the worktree is born detached). What is NOT current: a
    missing worktree, a failed fetch (we cannot verify against a stale remote
    ref), an unparseable/failed rev-list (zero information, not zero divergence),
    or any nonzero ahead/behind.
    """

    def __init__(self, *, exists: bool, on_main: bool, synced: bool,
                 ahead: int, behind: int, detail: str,
                 fetched: bool = True, rev_list_ok: bool = True) -> None:
        self.exists = exists
        self.on_main = on_main     # informational only; NOT part of `current`
        self.synced = synced       # in sync with origin/main (0 ahead / 0 behind)
        self.ahead = ahead
        self.behind = behind
        self.detail = detail
        self.fetched = fetched     # `git fetch origin` succeeded
        self.rev_list_ok = rev_list_ok  # ahead/behind were actually determined

    @property
    def current(self) -> bool:
        # HEAD == origin/main, VERIFIABLY: worktree exists, fetch refreshed the
        # remote ref, rev-list gave a parseable count, and that count is 0/0.
        # Branch name deliberately excluded — a detached mirror at origin/main
        # is current.
        return (self.exists and self.fetched and self.rev_list_ok
                and self.synced)


def pinned_worktree_status(runner: Runner = _run,
                           home: Path | None = None) -> PinnedReport:
    """Inspect the pinned worktree: exists? VERIFIABLY at origin/main?

    Fetches origin first, then compares HEAD to origin/main via rev-list counts.
    ahead = local commits not on origin/main; behind = origin commits not yet
    merged. The mirror is "current" iff HEAD == origin/main (0 ahead / 0 behind)
    AND we could actually verify that — a failed fetch or a failed/unparseable
    rev-list yields UNKNOWN, which is NOT current (the bug this module exists to
    prevent is reporting OK when health is unverified). A dirtied/ahead worktree
    is a real signal (someone edited the mirror) and is surfaced, not hidden.
    The branch name is recorded for diagnostics but does not gate `current`.
    """
    h = _home(home)
    wt = pinned_worktree_path(h)
    if not wt.exists():
        return PinnedReport(exists=False, on_main=False, synced=False,
                            ahead=0, behind=0, fetched=False, rev_list_ok=False,
                            detail=f"pinned worktree missing: {wt}")
    cwd = str(wt)

    br = runner(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    branch = (br.stdout or "").strip()
    on_main = br.returncode == 0 and branch == "main"

    # Fetch so the origin/main ref reflects the remote. A failed fetch means we
    # are comparing against a STALE ref — we cannot honestly call the mirror
    # current, so this forces current=False below.
    fetch = runner(["git", "-C", cwd, "fetch", "origin"], cwd)
    fetched = fetch.returncode == 0

    # rev-list is the actual divergence measurement. If it fails or returns
    # something we can't parse, we have ZERO information — treat that as UNKNOWN
    # (not as "0 ahead / 0 behind"), which forces current=False.
    ahead = behind = 0
    rl = runner(["git", "-C", cwd, "rev-list", "--left-right", "--count",
                 "refs/remotes/origin/main...HEAD"], cwd)
    rev_list_ok = False
    if rl.returncode == 0:
        parts = (rl.stdout or "").split()
        if len(parts) == 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
                rev_list_ok = True
            except ValueError:
                rev_list_ok = False
    synced = ahead == 0 and behind == 0

    if not fetched:
        detail = (f"branch={branch or '?'} "
                  "could not verify against remote (fetch failed)")
    elif not rev_list_ok:
        detail = (f"branch={branch or '?'} "
                  "could not determine ahead/behind vs origin/main")
    else:
        detail = (f"branch={branch or '?'} ahead={ahead} behind={behind} "
                  "fetched=ok")
    return PinnedReport(exists=True, on_main=on_main, synced=synced,
                        ahead=ahead, behind=behind, fetched=fetched,
                        rev_list_ok=rev_list_ok, detail=detail)


def fast_forward_pinned(runner: Runner = _run,
                        home: Path | None = None) -> bool:
    """Fast-forward the pinned worktree to origin/main.

    Runs `git -C <pinned> fetch origin` then `git -C <pinned> merge --ff-only
    origin/main`. Non-destructive: ff-only REFUSES (and we raise) if the pinned
    worktree was dirtied or diverged — that refusal is a real signal that the
    mirror was edited, not something to paper over. Returns True on a clean
    (possibly no-op) fast-forward.
    """
    h = _home(home)
    wt = pinned_worktree_path(h)
    if not wt.exists():
        raise RepinError(
            f"pinned worktree missing: {wt} — run repin/setup_pinned_plugin.sh first"
        )
    cwd = str(wt)

    for step in (["git", "-C", cwd, "fetch", "origin"],
                 ["git", "-C", cwd, "merge", "--ff-only", "refs/remotes/origin/main"]):
        try:
            cp = runner(step, cwd)
        except (OSError, subprocess.SubprocessError) as e:
            raise RepinError(f"{' '.join(step)} failed: {e}") from e
        if cp.returncode != 0:
            raise RepinError(
                f"{' '.join(step)} failed (rc={cp.returncode}): "
                f"{(cp.stderr or cp.stdout).strip()}"
            )
    return True


# --------------------------------------------------------------------------
# The installed post-merge hook
# --------------------------------------------------------------------------

HOOK_NAME = "post-merge"
HOOK_TEMPLATE_REL = Path("init") / "templates" / "git-hooks" / HOOK_NAME


def hook_template_path(home: Path | None = None) -> Path:
    """The reviewed hook: the template in the PINNED worktree (deployed main)."""
    return pinned_plugin_path(home) / HOOK_TEMPLATE_REL


def installed_hook_path(runner: Runner = _run, home: Path | None = None) -> Path | None:
    """Where git reads the source repo's post-merge hook from. Asked of git in
    the pinned worktree, which shares the source repo's hooks dir (and honours
    core.hooksPath). None when git cannot say."""
    wt = pinned_worktree_path(home)
    if not wt.exists():
        return None
    try:
        cp = runner(["git", "-C", str(wt), "rev-parse", "--git-path", "hooks"], str(wt))
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (cp.stdout or "").strip()
    if cp.returncode != 0 or not raw:
        return None
    hooks = Path(raw)
    return (hooks if hooks.is_absolute() else wt / hooks) / HOOK_NAME


def hook_status(runner: Runner = _run, home: Path | None = None) -> "tuple[str, str]":
    """(state, detail): current | stale | missing | unknown. The installed
    hook is a COPY made by hand, so a merged template change does not reach it
    until `repin.check hook --install` runs."""
    template = hook_template_path(home)
    installed = installed_hook_path(runner, home)
    if installed is None:
        return "unknown", "could not locate the hooks dir (pinned worktree missing?)"
    try:
        want = template.read_bytes()
    except OSError as e:
        return "unknown", f"cannot read the template {template}: {e}"
    try:
        have = installed.read_bytes()
    except FileNotFoundError:
        return "missing", f"{installed} is not installed"
    except OSError as e:
        return "unknown", f"cannot read {installed}: {e}"
    if have != want:
        return "stale", f"{installed} differs from {template}"
    if not os.access(installed, os.X_OK):
        return "stale", f"{installed} matches the template but is not executable"
    return "current", f"{installed} matches the pinned template"


def install_hook(runner: Runner = _run, home: Path | None = None) -> Path:
    """Copy the pinned template over the installed hook (write + rename, 0755)."""
    template = hook_template_path(home)
    installed = installed_hook_path(runner, home)
    if installed is None:
        raise RepinError("could not locate the hooks dir (pinned worktree missing?)")
    try:
        data = template.read_bytes()
        installed.parent.mkdir(parents=True, exist_ok=True)
        tmp = installed.with_name(f".{HOOK_NAME}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, installed)
    except OSError as e:
        raise RepinError(f"could not install {installed}: {e}") from e
    return installed


def cmd_hook(args: argparse.Namespace) -> int:
    """Is the installed post-merge hook the pinned template? Exit 0 only if so."""
    if args.install:
        try:
            path = install_hook()
        except RepinError as e:
            print(f"repin hook: install failed: {e}", file=sys.stderr)
            return 1
        print(f"repin hook: installed {path} from {hook_template_path()}")
    state, detail = hook_status()
    print(f"hook:   {state.upper()} — {detail}")
    if state != "current":
        print("  refresh it: $HOME/.maw/plugin-main/maw/bin/maw-run repin.check hook --install")
    return 0 if state == "current" else 2


def recipe_pointer(home: Path | None = None) -> str:
    """The repin 'recipe' is now a one-liner + a pointer to the runbook (when present)."""
    runbook = runbook_path(home)
    tail = f"\nOne-time setup + the live durability gate: {runbook}" if runbook else ""
    return (
        "repin = fast-forward the pinned worktree, then recopy it into the cache "
        "version dir (we bypass `claude plugin install` entirely — a symlink would "
        "be removed by claude on session start, a no-op install empties the cache):\n"
        f"  $HOME/.maw/plugin-main/maw/bin/maw-run repin.check sync\n"
        "  # equivalently:\n"
        "  #   git -C ~/.maw/plugin-main fetch origin && \\\n"
        "  #   git -C ~/.maw/plugin-main merge --ff-only refs/remotes/origin/main && \\\n"
        "  #   rm -rf ~/.claude/plugins/cache/maw/maw/<version>/* && \\\n"
        "  #   cp -R ~/.maw/plugin-main/maw/. ~/.claude/plugins/cache/maw/maw/<version>/ && \\\n"
        "  #   git -C ~/.maw/plugin-main rev-parse HEAD > "
        "~/.claude/plugins/cache/maw/maw/<version>/.maw-pinned-sha"
        f"{tail}"
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    """Report both statuses; exit 0 only if cache IN_SYNC and pinned current."""
    cache = cache_status()
    pinned = pinned_worktree_status()

    print(f"cache:  {cache.state.value} — {cache.detail}")
    print(f"pinned: {'CURRENT' if pinned.current else 'NOT-CURRENT'} — {pinned.detail}")

    if cache.healthy and pinned.current:
        print("repin: OK — cache is a real copy in sync with pinned main, worktree in sync.")
        return 0

    print("\nrepin: ACTION NEEDED.")
    if not cache.healthy:
        if cache.state is CacheState.BROKEN:
            print("  - cache is BROKEN (missing/empty/no manifest). Run one-time setup:")
            print("      bash maw/repin/setup_pinned_plugin.sh")
        elif cache.state is CacheState.NEEDS_RECOPY:
            print("  - cache version dir is a SYMLINK — claude removes it on the next")
            print("    session start (latent empty cache). Recopy as a real dir:")
            print("      $HOME/.maw/plugin-main/maw/bin/maw-run repin.check sync")
        elif cache.state is CacheState.STALE:
            print("  - cache is STALE (sentinel missing or != pinned HEAD). Recopy:")
            print("      $HOME/.maw/plugin-main/maw/bin/maw-run repin.check sync")
    if not pinned.current:
        if not pinned.exists:
            print("  - pinned worktree missing. Run: bash maw/repin/setup_pinned_plugin.sh")
        elif not pinned.fetched:
            print("  - could not fetch origin — cannot verify the mirror against "
                  "the remote (offline/transient?). Re-run when network is back.")
        elif not pinned.rev_list_ok:
            print("  - could not determine ahead/behind vs origin/main "
                  "(rev-list failed/unparseable) — investigate the pinned worktree.")
        elif pinned.ahead:
            print(f"  - pinned worktree is {pinned.ahead} ahead of origin/main — "
                  "the mirror was edited; ff-only sync will refuse. Investigate.")
        elif pinned.behind:
            print(f"  - pinned worktree is {pinned.behind} behind — run: $HOME/.maw/plugin-main/maw/bin/maw-run repin.check sync")
    runbook = runbook_path()
    if runbook:
        print(f"\nsee: {runbook}")
    return 2


def cmd_sync(args: argparse.Namespace) -> int:
    """Fast-forward the pinned worktree THEN recopy it into the cache.

    This is what the post-merge hook calls. ff first (so the copy source is
    current), then refresh_cache (rm + cp -R + sentinel). NEVER plugin install.
    """
    before = pinned_head()
    try:
        fast_forward_pinned()
        vd = refresh_cache()
    except RepinError as e:
        print(f"repin sync failed: {e}", file=sys.stderr)
        return 1
    after = pinned_head()
    if before and after and before == after:
        moved = f"pinned worktree already at origin/main ({after[:12]}), nothing to fast-forward"
    elif before and after:
        moved = f"pinned worktree fast-forwarded {before[:12]}..{after[:12]}"
    else:
        moved = "pinned worktree fast-forwarded to origin/main"
    print(f"repin sync: {moved}; recopied into the cache ({vd}); sentinel rewritten.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Human-readable health — never errors out (always exit 0)."""
    cache = cache_status()
    pinned = pinned_worktree_status()
    print("maw plugin foundation — pinned-worktree COPY model")
    print(f"  pinned worktree : {pinned_worktree_path()}")
    print(f"  copy source     : {pinned_plugin_path()}")
    print(f"  cache base      : {cache_base_path()}")
    print(f"  cache state     : {cache.state.value}  ({cache.detail})")
    print(f"  pinned state    : {'CURRENT' if pinned.current else 'NOT-CURRENT'}  ({pinned.detail})")
    hook_state, hook_detail = hook_status()
    print(f"  post-merge hook : {hook_state.upper()}  ({hook_detail})")
    healthy = cache.healthy and pinned.current
    runbook = runbook_path()
    attention = "NEEDS ATTENTION" + (f" — see {runbook}" if runbook else "")
    print(f"  overall         : {'HEALTHY' if healthy else attention}")
    return 0


def cmd_recipe(args: argparse.Namespace) -> int:
    print(recipe_pointer())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repin", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="report cache + pinned status; exit 2 if action needed")
    chk.set_defaults(fn=cmd_check)

    syn = sub.add_parser("sync", help="ff the pinned worktree + recopy into the cache (post-merge hook)")
    syn.set_defaults(fn=cmd_sync)

    st = sub.add_parser("status", help="human-readable foundation health (always exit 0)")
    st.set_defaults(fn=cmd_status)

    hk = sub.add_parser("hook", help="is the installed post-merge hook the pinned template? "
                                     "exit 2 if not; --install refreshes it")
    hk.add_argument("--install", action="store_true",
                    help="copy the pinned template over the installed hook")
    hk.set_defaults(fn=cmd_hook)

    rec = sub.add_parser("recipe", help="print the repin one-liner + runbook pointer")
    rec.set_defaults(fn=cmd_recipe)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
