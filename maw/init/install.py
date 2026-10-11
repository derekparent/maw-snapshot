"""Template-walking and per-file install for /maw:init.

The cli dispatches to install_target() with a resolved Profile + vars dict;
install_target() walks the template list, renders each, and either writes
it, preserves drift as a `.maw-recommended` sibling, overwrites with backup,
or skips (no-op when content matches). Contract §3 behavior matrix.

This module is the orchestration heart: it computes per-file actions
("install" / "skip-current" / "skip-preserving-drift" / "overwrite-with-backup")
and surfaces them up to cli.py for banner rendering.

Per-file atomicity (§7) goes through state.write_repo_bytes() (repo-local
targets, refusing a symlink at the write itself) or state.write_atomic_text()
(git hooks, which may live outside the repo). state.json itself is the LAST
write — done by cli.py after this module reports success.
"""
from __future__ import annotations

import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from init.profile import Profile
from init.state import (
    FileEntry,
    StateFile,
    SymlinkRefusal,
    read_repo_bytes_nofollow,
    sha256_file,
    sha256_text,
    utcnow_iso,
    write_atomic_text,
    write_repo_bytes,
)
from init.substitution import substitute_file

# Map a template-rel-path's leading segment to the target-repo-rel-path
# layout. Anything outside these three families is rejected at profile
# load time (profile YAMLs only declare hooks/git_hooks/schemas).
TARGET_PREFIX = {
    "hooks": ".claude/hooks",
    "git-hooks": ".git/hooks",
    "schemas": ".claude/schemas",
}

# Action vocabulary used in the banner output. Keep these stable — the
# banner test surface and downstream telemetry parse them.
ACTION_INSTALL = "install"
ACTION_SKIP_CURRENT = "skip-current"
ACTION_SKIP_PRESERVING_DRIFT = "skip-preserving-drift"
ACTION_OVERWRITE_WITH_BACKUP = "overwrite-with-backup"
ACTION_WROTE_RECOMMENDED = "wrote-recommended-sibling"
ACTION_PLANNED = "would-install"           # dry-run only
ACTION_PLANNED_RECOMMENDED = "would-write-recommended"
ACTION_PLANNED_OVERWRITE = "would-overwrite-with-backup"
ACTION_DRY_RUN_NO_CHANGE = "would-skip-current"
ACTION_REFUSED_SYMLINK = "refused-symlink"   # target reached through a symlink


@dataclass
class FileAction:
    template: str
    target_rel: str
    action: str
    backup_path: str | None = None  # set when an overwrite generates a backup


def template_to_target_relpath(template_rel: str) -> str:
    """Map e.g. 'hooks/budget-guard.py' → '.claude/hooks/budget-guard.py'."""
    if "/" not in template_rel:
        raise ValueError(
            f"template path {template_rel!r} lacks a category prefix; "
            f"expected one of: {sorted(TARGET_PREFIX)}"
        )
    head, _, rest = template_rel.partition("/")
    if head not in TARGET_PREFIX:
        raise ValueError(
            f"template path {template_rel!r} uses unknown category "
            f"{head!r}; expected one of: {sorted(TARGET_PREFIX)}"
        )
    return f"{TARGET_PREFIX[head]}/{rest}"


def git_hooks_dir(repo_root: Path) -> Path:
    """Worktree-aware hooks directory (#204).

    In a linked worktree `.git` is a gitdir-pointer FILE, so joining
    `repo_root / ".git/hooks"` ENOTDIRs on the mkdir. `git rev-parse
    --git-path hooks` resolves the real hooks dir in both shapes (shared
    common dir for linked worktrees) and respects core.hooksPath. Falls
    back to the plain join when git is absent or the root isn't a real
    repo (the test fixtures' bare `.git` directory).
    """
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return repo_root / ".git" / "hooks"
    if cp.returncode != 0 or not cp.stdout.strip():
        return repo_root / ".git" / "hooks"
    p = Path(cp.stdout.strip())
    return p if p.is_absolute() else repo_root / p


def _backup_path(repo_root: Path, target_rel: str) -> Path:
    """`.claude/maw/.attic/<basename>.<utc-iso8601>` per contract §5."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = Path(target_rel).name
    return repo_root / ".claude" / "maw" / ".attic" / f"{name}.{ts}"


def _copy_with_attic(
    repo_root: Path,
    target_rel: str,
    target_abs: Path,
) -> Path:
    """Copy target_abs → backup under .claude/maw/.attic/. Returns backup path.
    Raises SymlinkRefusal if the attic, or the target itself, is a link."""
    backup = _backup_path(repo_root, target_rel)
    data, st = read_repo_bytes_nofollow(target_abs)
    write_repo_bytes(repo_root, backup, data, mode=stat.S_IMODE(st.st_mode),
                     times_ns=(st.st_atime_ns, st.st_mtime_ns))
    return backup


def _is_executable_template(template_abs: Path) -> bool:
    return bool(template_abs.stat().st_mode & 0o111)


def _write_file(
    target_abs: Path,
    content: str,
    *,
    executable: bool,
    repo_root: Path | None,
) -> None:
    """repo_root set: a repo-local write that raises SymlinkRefusal rather
    than go through a link. None: a git hook, written where git says hooks
    live (possibly outside the repo), as before."""
    mode = 0o600 | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH if executable else 0)
    if repo_root is not None:
        write_repo_bytes(repo_root, target_abs, content.encode("utf-8"), mode=mode)
        return
    target_abs.parent.mkdir(parents=True, exist_ok=True)
    write_atomic_text(target_abs, content)
    if executable:
        target_abs.chmod(target_abs.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class InstallPlan:
    profile: Profile
    repo_root: Path
    variables: dict[str, str]
    prior_state: StateFile | None
    dry_run: bool = False
    force: bool = False
    upgrade: bool = False  # verbose-mode only per §9.2


@dataclass
class InstallReport:
    actions: list[FileAction]
    new_state: StateFile

    @property
    def installed(self) -> list[FileAction]:
        return [a for a in self.actions if a.action == ACTION_INSTALL]

    @property
    def skipped(self) -> list[FileAction]:
        return [a for a in self.actions if a.action == ACTION_SKIP_CURRENT]

    @property
    def drifted(self) -> list[FileAction]:
        return [
            a for a in self.actions
            if a.action in (
                ACTION_WROTE_RECOMMENDED,
                ACTION_PLANNED_RECOMMENDED,
            )
        ]

    @property
    def backed_up(self) -> list[FileAction]:
        return [
            a for a in self.actions
            if a.action in (
                ACTION_OVERWRITE_WITH_BACKUP,
                ACTION_PLANNED_OVERWRITE,
            )
        ]


def _resolved_template_path(template_rel: str) -> Path:
    """Resolve a template path against this plugin's templates/ dir."""
    base = Path(__file__).resolve().parent / "templates"
    full = base / template_rel
    # Reject path-traversal: substitution dir must be a parent.
    try:
        full.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(
            f"template path escapes templates/ root: {template_rel!r}"
        ) from exc
    if not full.is_file():
        raise FileNotFoundError(f"template missing on disk: {template_rel}")
    return full


def through_symlink(repo_root: Path, target_abs: Path) -> bool:
    """True if a repo-local target is itself a symlink, or any directory
    between the repo root and it is one. Writing there could land outside the
    repo (an adopted .claude/ may hold links), so such a target is refused."""
    root = repo_root.absolute()
    try:
        rel = target_abs.absolute().relative_to(root)
    except ValueError:
        return True
    probe = root
    for part in rel.parts:
        probe = probe / part
        if probe.is_symlink():
            return True
    return False


def _refuse(target_rel: str, why: str) -> None:
    print(f"maw init: NOT writing {target_rel}: {why}", file=sys.stderr)


def plan_and_apply(plan: InstallPlan) -> InstallReport:
    """Walk the profile's template list, render each, and act per §3.

    Decision tree per file (dry-run mirrors with `would-` prefix):
      target missing                        → install
      target present, sha matches new       → skip-current
      target present, sha != new
        --force                              → overwrite-with-backup
        else                                 → write `<file>.maw-recommended`

    state.json records what maw put on disk or wanted there. Installed,
    current and overwritten targets, and a user's kept edit (the rendered SHA
    is the drift baseline), get a fresh entry. A REFUSED overwrite keeps its
    prior entry if it had one and gets none otherwise; a target reached
    through a symlink gets none.
    """
    actions: list[FileAction] = []
    new_files: dict[str, FileEntry] = {}
    prior_files = plan.prior_state.files if plan.prior_state else {}

    for template_rel in plan.profile.all_template_paths:
        template_abs = _resolved_template_path(template_rel)
        target_rel = template_to_target_relpath(template_rel)
        # git-hooks resolve through git itself: the state key stays the
        # stable ".git/hooks/<name>" rel-path, but the on-disk location in
        # a linked worktree is the shared common dir (#204).
        is_git_hook = template_rel.startswith("git-hooks/")
        if is_git_hook:
            target_abs = git_hooks_dir(plan.repo_root) / template_rel.partition("/")[2]
        else:
            target_abs = plan.repo_root / target_rel
        write_root = None if is_git_hook else plan.repo_root
        if not is_git_hook and through_symlink(plan.repo_root, target_abs):
            _refuse(target_rel, "the path goes through a symlink, so the write "
                    "could land outside this repo")
            actions.append(FileAction(template_rel, target_rel, ACTION_REFUSED_SYMLINK))
            continue
        rendered = substitute_file(template_abs, plan.variables)
        rendered_sha = sha256_text(rendered)
        executable = _is_executable_template(template_abs)
        fresh_entry = FileEntry(
            template=template_rel,
            installed_sha256=rendered_sha,
            installed_at=utcnow_iso(),
        )

        def keep_prior() -> None:
            if target_rel in prior_files:
                new_files[target_rel] = prior_files[target_rel]

        if not target_abs.exists():
            if plan.dry_run:
                actions.append(FileAction(template_rel, target_rel, ACTION_PLANNED))
                new_files[target_rel] = fresh_entry
                continue
            try:
                _write_file(target_abs, rendered, executable=executable,
                            repo_root=write_root)
            except SymlinkRefusal:
                _refuse(target_rel, "a symlink appeared on the path before the write")
                actions.append(FileAction(template_rel, target_rel, ACTION_REFUSED_SYMLINK))
                continue
            actions.append(FileAction(template_rel, target_rel, ACTION_INSTALL))
            new_files[target_rel] = fresh_entry
            continue

        current_sha = sha256_file(target_abs)
        if current_sha == rendered_sha:
            actions.append(
                FileAction(
                    template_rel,
                    target_rel,
                    ACTION_DRY_RUN_NO_CHANGE if plan.dry_run else ACTION_SKIP_CURRENT,
                )
            )
            new_files[target_rel] = fresh_entry
            continue

        # Drifted (current SHA != rendered SHA).
        if plan.force:
            if through_symlink(plan.repo_root, _backup_path(plan.repo_root, target_rel)):
                # No backup, no overwrite: a symlinked .claude/maw would carry
                # the user's file outside the repo on its way to the attic.
                print(f"maw init: NOT overwriting {target_rel}: its backup path "
                      f"under .claude/maw/.attic/ goes through a symlink",
                      file=sys.stderr)
                actions.append(FileAction(template_rel, target_rel, ACTION_REFUSED_SYMLINK))
                keep_prior()
            elif plan.dry_run:
                actions.append(
                    FileAction(
                        template_rel,
                        target_rel,
                        ACTION_PLANNED_OVERWRITE,
                        backup_path=str(_backup_path(plan.repo_root, target_rel)),
                    )
                )
                new_files[target_rel] = fresh_entry
            else:
                try:
                    backup = _copy_with_attic(plan.repo_root, target_rel, target_abs)
                except SymlinkRefusal:
                    print(f"maw init: NOT overwriting {target_rel}: a symlink "
                          f"appeared on it or its .attic backup path before the "
                          f"write", file=sys.stderr)
                    actions.append(FileAction(template_rel, target_rel, ACTION_REFUSED_SYMLINK))
                    if not target_abs.is_symlink():
                        keep_prior()
                    continue
                try:
                    _write_file(target_abs, rendered, executable=executable,
                                repo_root=write_root)
                except SymlinkRefusal:
                    _refuse(target_rel, "a symlink appeared on the path before the write")
                    actions.append(FileAction(template_rel, target_rel, ACTION_REFUSED_SYMLINK))
                    keep_prior()
                    continue
                actions.append(
                    FileAction(
                        template_rel,
                        target_rel,
                        ACTION_OVERWRITE_WITH_BACKUP,
                        backup_path=str(backup),
                    )
                )
                new_files[target_rel] = fresh_entry
            continue

        # No --force: preserve target's edit, write sibling .maw-recommended.
        # The record still tracks the rendered SHA at target_rel: that is the
        # substrate's "what we wanted" baseline the next drift check runs
        # against, and what `/maw:status` counts drift siblings from (a file
        # that predates maw's first init must still surface). The on-disk file
        # is the user's edited version; siblings are advisory only.
        new_files[target_rel] = fresh_entry
        sibling_rel = f"{target_rel}.maw-recommended"
        sibling_abs = plan.repo_root / sibling_rel
        if through_symlink(plan.repo_root, sibling_abs):
            print(f"maw init: NOT writing {sibling_rel}: the path goes through a "
                  f"symlink", file=sys.stderr)
            actions.append(FileAction(template_rel, sibling_rel, ACTION_REFUSED_SYMLINK))
        elif sibling_abs.is_file() and sha256_file(sibling_abs) == rendered_sha:
            # The user's edit is still kept and maw's version beside it is
            # already current: rewriting it every run is churn, not news.
            actions.append(
                FileAction(template_rel, sibling_rel, ACTION_SKIP_PRESERVING_DRIFT)
            )
        elif plan.dry_run:
            actions.append(
                FileAction(template_rel, sibling_rel, ACTION_PLANNED_RECOMMENDED)
            )
        else:
            try:
                _write_file(sibling_abs, rendered, executable=False,
                            repo_root=plan.repo_root)
            except SymlinkRefusal:
                _refuse(sibling_rel, "a symlink appeared on the path before the write")
                actions.append(FileAction(template_rel, sibling_rel, ACTION_REFUSED_SYMLINK))
                continue
            actions.append(
                FileAction(template_rel, sibling_rel, ACTION_WROTE_RECOMMENDED)
            )

    state = StateFile(
        installed_at=plan.prior_state.installed_at if plan.prior_state else utcnow_iso(),
        substrate_version=plan.variables.get("plugin_version", "0.0.0"),
        # The label above never moves; these do (#201).
        substrate_sha=plan.variables.get("plugin_sha", ""),
        substrate_committed_at=plan.variables.get("plugin_committed_at", ""),
        profile=plan.profile.name,
        files=new_files,
    )
    return InstallReport(actions=actions, new_state=state)


__all__ = [
    "ACTION_INSTALL",
    "ACTION_SKIP_CURRENT",
    "ACTION_OVERWRITE_WITH_BACKUP",
    "ACTION_WROTE_RECOMMENDED",
    "ACTION_PLANNED",
    "ACTION_PLANNED_OVERWRITE",
    "ACTION_PLANNED_RECOMMENDED",
    "ACTION_DRY_RUN_NO_CHANGE",
    "FileAction",
    "git_hooks_dir",
    "InstallPlan",
    "InstallReport",
    "plan_and_apply",
    "through_symlink",
    "template_to_target_relpath",
    "TARGET_PREFIX",
]
