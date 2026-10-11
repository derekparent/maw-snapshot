"""init.cli — entry point for `/maw:init`.

The slash-command markdown at maw/commands/init.md shells out to:
    "${CLAUDE_PLUGIN_ROOT}/bin/maw-run" init.cli $ARGUMENTS

argparse surface per contract §1; behavior per §§2-8. Exit codes per §6:
    0 = success
    1 = refuse (not-a-git-repo, target-is-maw, profile-unknown, etc.)
    2 = parse / path / placeholder error
    3 = permission denied
    4 = disk full / IO failure

Stdlib-only; no third-party YAML.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from init import pyfloor

# Before any import that needs the floor (stack_detection imports tomllib): an
# old interpreter gets one line naming the Python it needs, not a traceback.
pyfloor.require()

from init.probe import capabilities_line as render_capabilities_line  # noqa: E402
from init.profile import (
    Profile,
    ProfileError,
    list_available_profiles,
    load_profile,
    read_profile_name_from_config,
    read_repo_id_from_config,
    read_repo_slug_from_config,
    render_config_yaml,
)
from init.stack_detection import (
    IsGitSubmoduleError,
    NotAGitRepoError,
    detect as detect_stack,
)
from init.state import (
    StateError,
    StateFile,
    SymlinkRefusal,
    read_state,
    state_path,
    write_repo_text,
    write_state,
)
from init.install import (
    ACTION_INSTALL,
    ACTION_OVERWRITE_WITH_BACKUP,
    ACTION_PLANNED,
    ACTION_PLANNED_OVERWRITE,
    ACTION_PLANNED_RECOMMENDED,
    ACTION_SKIP_CURRENT,
    ACTION_SKIP_PRESERVING_DRIFT,
    ACTION_WROTE_RECOMMENDED,
    InstallPlan,
    InstallReport,
    plan_and_apply,
    through_symlink,
)
from init.substitution import UnresolvedPlaceholderError

EXIT_OK = 0
EXIT_REFUSE = 1
EXIT_PARSE = 2
EXIT_PERMISSION = 3
EXIT_IO = 4

# Where the gitignore fragment template lives (shipped alongside other
# templates, but not declared in any profile YAML — it's a substrate
# universal that every target repo needs to keep state.json + .runtime/ +
# .attic/ out of the index).
GITIGNORE_FRAGMENT_PATH = (
    Path(__file__).resolve().parent / "templates" / ".gitignore-fragment"
)
GITIGNORE_BEGIN = "# --- maw substrate (added by /maw:init) ---"
GITIGNORE_END = "# --- end maw substrate ---"
# The marker earlier versions wrote. A repo that carries it is found by it and
# its block is rewritten under the current marker, so a rename of the marker
# never leaves two blocks behind.
GITIGNORE_BEGIN_FORMER = ("# --- maw substrate (added by /maw init) ---",)


class InitRefusal(Exception):
    """Refuse-with-message error path. Carries the exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_REFUSE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maw init",
        description="Install the maw substrate into a target repository.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Override auto-detected stack profile (e.g., 'generic').",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite drifted target files, backing up to .claude/maw/.attic/.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan; no filesystem writes.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Explicit re-run with verbose per-file rationale.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Target repo root (defaults to cwd). Primarily for testing.",
    )
    return parser


# ---------------------------------------------------------------------------
# Prerequisite checks (contract §1, §6)
# ---------------------------------------------------------------------------

def _is_maw_repo(repo_root: Path) -> bool:
    """Detect when /maw:init is being run from inside the maw substrate.

    The umbrella `marketplace.json` at the repo root is the load-bearing
    signal — only maw publishes one, and it's gitignore-stable. A previous
    revision also checked the operator's own absolute checkout path
    as belt-and-suspenders; reviewer flagged that as operator-specific (LOW
    finding L2) and the marketplace.json check is sufficient on its own.
    """
    return (repo_root / ".claude-plugin" / "marketplace.json").is_file()


def _is_git_repo(repo_root: Path) -> bool:
    git_path = repo_root / ".git"
    return git_path.is_dir() or git_path.is_file()


def _claude_dir_is_file(repo_root: Path) -> bool:
    p = repo_root / ".claude"
    return p.exists() and not p.is_dir()


# ---------------------------------------------------------------------------
# Variables resolution
# ---------------------------------------------------------------------------

def _plugin_version() -> str:
    """Read version from the umbrella plugin manifest. Conservative fallback."""
    manifest = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "0.6.0-unknown"
    return str(data.get("version", "0.6.0-unknown"))


def _plugin_commit_vars() -> dict[str, str]:
    """The plugin commit the transplant comes from — the stamp that can go stale
    (#201). Fail-open to empty strings: an unstamped substrate is flagged by
    init.substrate as un-provable, which is the honest state, not a crash here."""
    from init.substrate import plugin_commit

    sha, committed_at = plugin_commit()
    return {"plugin_sha": sha, "plugin_committed_at": committed_at}


def _candidate_repo_slug(repo_root: Path) -> str:
    """Deterministic slug: <dirname>-<sha1[:8] of absolute path>.

    Same shape as the bridge's per-repo slug (e.g.,
    `my-app-a3f8c102`). Stable across re-runs as long as the
    repo isn't moved.
    """
    abs_path = str(repo_root.resolve())
    name = repo_root.resolve().name.lower()
    # Sanitize: lowercase alnum + dashes, collapse runs.
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in name).strip("-")
    if not safe:
        safe = "repo"
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def _resolve_bridge_slug(repo_root: Path, repo_id: str) -> tuple[str, str | None]:
    """Bridge-slug collision handling per §9.6.

    Returns (slug, warning_message). Probes ~/.claude/bridge/<slug>/.maw-repo-id;
    on mismatch, suffixes -2, -3, ... until a free slot is found or the existing
    one matches our repo_id. Lossless preserve.
    """
    base = _candidate_repo_slug(repo_root)
    home_bridge = Path(os.environ.get("MAW_BRIDGE_HOME") or os.path.expanduser("~/.claude/bridge"))
    if not home_bridge.exists():
        return base, None

    def _claim_match(slug: str) -> bool | None:
        slug_dir = home_bridge / slug
        if not slug_dir.exists():
            return None  # free
        marker = slug_dir / ".maw-repo-id"
        if not marker.is_file():
            # Pre-existing directory without our marker: treat as collision.
            return False
        existing = marker.read_text(encoding="utf-8").strip()
        return existing == repo_id

    initial = _claim_match(base)
    if initial is None or initial is True:
        return base, None

    # Need a suffix.
    n = 2
    while True:
        candidate = f"{base}-{n}"
        result = _claim_match(candidate)
        if result is None or result is True:
            warning = (
                f"WARNING: bridge slug {base!r} collides with another repo; "
                f"using {candidate!r} instead. Original slug preserved for "
                f"the other repo (lossless)."
            )
            return candidate, warning
        n += 1
        if n > 99:
            # Pathological case; refuse rather than loop forever.
            raise InitRefusal(
                f"bridge slug collision unresolvable: {base}-2..{base}-99 all taken",
                exit_code=EXIT_REFUSE,
            )


def _runtime_vars(repo_root: Path, *, repo_id: str | None = None, repo_slug: str | None = None) -> dict[str, str]:
    """Variables /maw:init derives at install time (not from any template).

    repo_id / repo_slug are reused from an existing config.yaml if present;
    otherwise generated. plugin_version + deploy_url are always derived fresh.
    """
    if repo_id is None:
        repo_id = str(uuid.uuid4())
    if repo_slug is None:
        repo_slug, _ = _resolve_bridge_slug(repo_root, repo_id)
    return {
        "plugin_version": _plugin_version(),
        **_plugin_commit_vars(),
        "repo_id": repo_id,
        "repo_slug": repo_slug,
        # deploy_url is profile-shipped placeholder; we default to empty so
        # operators can edit `.maw/config.yaml` after init. Tests confirm this.
        "deploy_url": "",
    }


def _merged_vars(profile: Profile, runtime: dict[str, str]) -> dict[str, str]:
    """profile.template_vars overlaid on top of the runtime-derived vars.

    Runtime values win for the shared keys (e.g., plugin_version), which
    matches the profile YAML's `{{plugin_version}}` expectation — the profile
    file declares the variable; runtime supplies the value.
    """
    merged = dict(profile.template_vars)
    merged.update(runtime)
    return merged


# ---------------------------------------------------------------------------
# Gitignore fragment management
# ---------------------------------------------------------------------------

def _whole_line(text: str, needle: str, pos: int) -> int:
    """Offset of the first line at or after `pos` that is exactly `needle`, or -1.

    A marker counts only as a line of its own. A user's line that quotes one
    (a comment about the block, say) is not a block boundary.
    """
    while True:
        i = text.find(needle, pos)
        if i == -1:
            return -1
        end = i + len(needle)
        starts = i == 0 or text[i - 1] == "\n"
        ends = end == len(text) or text.startswith(("\n", "\r\n"), end)
        if starts and ends:
            return i
        pos = i + 1


def _line_end(text: str, pos: int) -> int:
    """Offset just past the line ending that starts at `pos`, if one does."""
    if text.startswith("\r\n", pos):
        return pos + 2
    if text.startswith("\n", pos):
        return pos + 1
    return pos


def _gitignore_blocks(text: str, body: "frozenset[str]") -> "list[tuple[int, int]]":
    """Every maw block in `text` as (start, end) offsets, in file order.

    A block starts at a BEGIN marker line, current or former, and ends after
    its END line. A BEGIN with no END of its own (the next marker is another
    BEGIN, or there is none) covers its own line and the lines right after it
    that are lines of the fragment (`body`): it stops at the first line that
    is not, so whatever a user wrote below a damaged block is kept.
    """
    markers = (GITIGNORE_BEGIN, *GITIGNORE_BEGIN_FORMER)

    def next_begin(pos: int) -> int:
        found = [i for i in (_whole_line(text, m, pos) for m in markers) if i != -1]
        return min(found) if found else -1

    blocks = []
    begin = next_begin(0)
    while begin != -1:
        following = next_begin(begin + 1)
        end_idx = _whole_line(text, GITIGNORE_END, begin)
        if end_idx != -1 and (following == -1 or end_idx < following):
            block_end = _line_end(text, end_idx + len(GITIGNORE_END))
        else:
            limit = following if following != -1 else len(text)
            nl = text.find("\n", begin, limit)
            block_end = limit if nl == -1 else nl + 1  # past the BEGIN line
            while block_end < limit:
                nl = text.find("\n", block_end, limit)
                stop = limit if nl == -1 else nl + 1
                if text[block_end:stop].rstrip("\r\n") not in body:
                    break
                block_end = stop
        blocks.append((begin, block_end))
        begin = next_begin(block_end)
    return blocks


def _ensure_gitignore_fragment(repo_root: Path, *, dry_run: bool) -> bool:
    """Idempotently install the substrate's gitignore fragment.

    A BEGIN marker is the idempotency signal: the first maw block in the file
    (under the current marker or a former one) is replaced where it stands with
    the current fragment, and any further maw block is removed, so the file
    ends with exactly one. Text outside the blocks is kept byte for byte.
    Append-fresh only happens when the file has no block at all.

    Returns True if .gitignore was modified; False if already current.
    """
    target = repo_root / ".gitignore"
    fragment = GITIGNORE_FRAGMENT_PATH.read_text(encoding="utf-8")
    if target.is_file():
        # Bytes, not read_text: text mode turns a CRLF line outside the block
        # into LF, and the rewrite would then change a line that is not ours.
        existing = target.read_bytes().decode("utf-8")
        body = frozenset(fragment.splitlines()) - {GITIGNORE_BEGIN, GITIGNORE_END, ""}
        blocks = _gitignore_blocks(existing, body)
        if blocks:
            parts = [existing[:blocks[0][0]], fragment]
            for (_, end), (start, _) in zip(blocks, blocks[1:]):
                parts.append(existing[end:start])
            parts.append(existing[blocks[-1][1]:])
            new = "".join(parts)
            if new == existing:
                return False
        else:
            # No BEGIN sentinel — append fresh, ensuring separation.
            sep = "" if existing.endswith("\n") else "\n"
            new = existing + sep + "\n" + fragment
    else:
        new = fragment
    if not dry_run:
        write_repo_text(repo_root, target, new)
    return True


# ---------------------------------------------------------------------------
# Config.yaml management
# ---------------------------------------------------------------------------

CONFIG_REL = ".maw/config.yaml"


def _config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_REL


def _write_config(repo_root: Path, content: str) -> None:
    """First-install only. Re-runs MUST NOT overwrite config.yaml (§9.4)."""
    write_repo_text(repo_root, _config_path(repo_root), content)


# ---------------------------------------------------------------------------
# Banner / observability (contract §8)
# ---------------------------------------------------------------------------

def _hooks_path_note(repo_root: Path, *, dry_run: bool) -> str:
    """A NOTE when core.hooksPath redirects hooks away from .git/hooks, else "".

    git reads hooks from core.hooksPath and ignores .git/hooks, so the per-file
    lines above (which name .git/hooks/...) are not where the hooks land. Say
    where they do, and that a shared path runs them for every repo using it.
    Best-effort: any git failure prints nothing.
    """
    import subprocess
    import init.install as inst
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    configured = cp.stdout.strip() if cp.returncode == 0 else ""
    if not configured:
        return ""
    where = inst.git_hooks_dir(repo_root)
    verb = "would be" if dry_run else "were"
    return (
        f"    NOTE: core.hooksPath is set ({configured}); git ignores .git/hooks "
        f"here. maw's hooks {verb} installed in {where}, not .git/hooks. Any "
        f"other repo that shares that path runs them too."
    )


def _print_banner(
    *,
    profile_name: str,
    profile_origin: str,
    capabilities_line: str,
    report: InstallReport,
    bridge_slug: str,
    slug_warning: str | None,
    runtime_vars: dict[str, str],
    dry_run: bool,
    upgrade: bool,
    out: object = None,
    repo_root: Path | None = None,
) -> None:
    """Emit the contract §8 banner. Six required fields.

    `out` is typed loosely to ease test injection (any file-like .write target).
    Defaults to the live sys.stdout (resolved at call time, not import time,
    so redirect_stdout() in unit tests captures the banner correctly).
    """
    sink = out if out is not None else sys.stdout

    def write(line: str = "") -> None:
        sink.write(line + "\n")

    title = "/maw:init — dry-run plan" if dry_run else "/maw:init"
    write("")
    write(f"=== {title} ===")
    write(f"[1] Profile: {profile_name} ({profile_origin})")
    write(f"[2] Capabilities: {capabilities_line}")
    write("[3] Per-file actions:")
    if not report.actions:
        write("    (none)")
    for a in report.actions:
        suffix = f"  backup={a.backup_path}" if a.backup_path else ""
        write(f"    [{a.action}] {a.target_rel} <- {a.template}{suffix}")
    write(f"[4] Bridge slug: {bridge_slug}")
    if slug_warning:
        write(f"    {slug_warning}")
    installed = len([a for a in report.actions if a.action in (ACTION_INSTALL, ACTION_PLANNED)])
    skipped = len([a for a in report.actions if a.action == ACTION_SKIP_CURRENT])
    drifted = len([
        a for a in report.actions
        if a.action in (ACTION_WROTE_RECOMMENDED, ACTION_PLANNED_RECOMMENDED)
    ])
    kept_drift = len([a for a in report.actions if a.action == ACTION_SKIP_PRESERVING_DRIFT])
    backed_up = len([
        a for a in report.actions
        if a.action in (ACTION_OVERWRITE_WITH_BACKUP, ACTION_PLANNED_OVERWRITE)
    ])
    write(
        f"[5] Summary: installed={installed} skipped={skipped} "
        f"drifted={drifted} kept-drift={kept_drift} backed-up={backed_up}"
    )
    alias_suggestion = runtime_vars.get("alias_suggestion", "mw")
    if _push_gate_state(report) == "kept-yours":
        verb = "will stay" if dry_run else "is"
        write(
            f"[6] Next: Run /maw:status to verify hooks are active.\n"
            f"    NOTE: the MAW_HUMAN push gate {verb} NOT active in this repo. "
            f"{PRE_PUSH_REL} is yours (kept), not maw's current hook, so "
            f"MAW_HUMAN=1 may do nothing here. To turn the gate on, merge "
            f"{PRE_PUSH_REL}.maw-recommended into your hook (or replace yours "
            f"with it), then add MAW_HUMAN=1 to the alias you launch Claude "
            f"with (suggested: alias {alias_suggestion}=\"MAW_HUMAN=1 claude\")."
        )
    else:
        write(
            f"[6] Next: Run /maw:status to verify hooks are active; "
            f"add MAW_HUMAN=1 to your shell alias (suggested: alias {alias_suggestion}=\"MAW_HUMAN=1 claude\")."
        )
    note = _hooks_path_note(repo_root, dry_run=dry_run) if repo_root else ""
    if note:
        write(note)
    if upgrade:
        write("    (upgrade-verbose mode requested)")
    write("")


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    try:
        return _cmd_init_inner(args, repo_root)
    except InitRefusal as exc:
        sys.stderr.write(f"/maw:init: {exc}\n")
        return exc.exit_code
    except SymlinkRefusal as exc:
        # The up-front check passed, then a link appeared before the write.
        sys.stderr.write(
            f"/maw:init: refusing: {exc}, so maw's write could land outside "
            f"this repo or replace your link. Make it a plain file/directory, "
            f"then re-run.\n"
        )
        return EXIT_REFUSE
    except UnresolvedPlaceholderError as exc:
        sys.stderr.write(f"/maw:init: missing placeholder values — {exc}\n")
        return EXIT_PARSE
    except ProfileError as exc:
        sys.stderr.write(f"/maw:init: {exc}\n")
        return EXIT_REFUSE
    except StateError as exc:
        sys.stderr.write(
            f"/maw:init: .maw/state.json malformed: {exc}\n"
            f"  Suggest: delete `.maw/state.json` and re-run `/maw:init` "
            f"(it will rebuild state from disk via SHA-recompute).\n"
        )
        return EXIT_PARSE
    except PermissionError as exc:
        sys.stderr.write(f"/maw:init: permission denied: {exc}\n")
        return EXIT_PERMISSION
    except OSError as exc:
        # Disk full or other low-level IO. errno 28 == ENOSPC.
        if getattr(exc, "errno", None) == 28:
            sys.stderr.write(f"/maw:init: disk full mid-write: {exc}\n")
            return EXIT_IO
        sys.stderr.write(f"/maw:init: IO error: {exc}\n")
        return EXIT_IO


def _cmd_init_inner(args: argparse.Namespace, repo_root: Path) -> int:
    # ---- §1 prerequisite checks ----
    if _is_maw_repo(repo_root):
        raise InitRefusal(
            "Inside maw substrate; init runs on consuming repos. "
            "Use /maw:init from your target repo.",
            exit_code=EXIT_REFUSE,
        )
    if not _is_git_repo(repo_root):
        raise InitRefusal(
            "Target is not a git repository. "
            "Run `git init` first, then re-run /maw:init.",
            exit_code=EXIT_REFUSE,
        )
    if _claude_dir_is_file(repo_root):
        raise InitRefusal(
            f"Path conflict: {repo_root / '.claude'} is a file, not a directory.",
            exit_code=EXIT_PARSE,
        )

    # ---- §2 state detection ----
    config_path = _config_path(repo_root)
    config_existed = config_path.is_file()
    prior_state = read_state(repo_root)
    claude_dir = (repo_root / ".claude")
    partial_no_config = (
        not config_existed
        and claude_dir.is_dir()
        and (any(claude_dir.iterdir()) if claude_dir.exists() else False)
    )
    # A non-empty .claude/ with no maw config is the normal first-run state of
    # any repo already using Claude Code (settings.json, commands/, skills/…).
    # It is ADOPTED, never refused: existing files are never overwritten — a
    # file maw would install that already exists and differs is kept, with a
    # `.maw-recommended` sibling beside it (the drift rule). What was added and
    # what was kept is listed on stderr after the plan runs.
    adopting = partial_no_config

    # ---- Profile resolution ----
    profile_origin = "auto-detected"
    if config_existed:
        existing_text = config_path.read_text(encoding="utf-8")
        existing_profile = read_profile_name_from_config(existing_text)
        existing_repo_id = read_repo_id_from_config(existing_text)
        existing_repo_slug = read_repo_slug_from_config(existing_text)
    else:
        existing_text = ""
        existing_profile = None
        existing_repo_id = None
        existing_repo_slug = None

    if args.profile:
        if existing_profile and existing_profile != args.profile and not args.force:
            raise InitRefusal(
                f"Stack profile change is destructive: existing config uses "
                f"{existing_profile!r}, but --profile {args.profile!r} was passed. "
                f"Re-run with --force to overwrite or `/maw migrate {args.profile}` "
                f"to migrate (v0.7).",
                exit_code=EXIT_REFUSE,
            )
        profile_name = args.profile
        profile_origin = "--profile override"
    elif existing_profile:
        profile_name = existing_profile
        profile_origin = "from .maw/config.yaml"
    else:
        try:
            detection = detect_stack(repo_root)
        except (NotAGitRepoError, IsGitSubmoduleError) as exc:
            raise InitRefusal(str(exc), exit_code=EXIT_REFUSE) from exc
        candidate = detection.profile
        if candidate not in list_available_profiles():
            # PRD §4.1.1 FLAG 1 fallback (a): silently use generic when
            # template doesn't exist for the detected profile. Surface the
            # fallback in the banner so the operator can verify.
            profile_name = "generic"
            profile_origin = f"detected={candidate!r}; fell back to generic"
        else:
            profile_name = candidate
            profile_origin = f"auto-detected ({candidate})"

    try:
        profile = load_profile(profile_name)
    except ProfileError:
        # Re-raise as InitRefusal to attach the canonical refuse message.
        raise InitRefusal(
            f"unknown profile {profile_name!r}. "
            f"Available: {', '.join(list_available_profiles())}",
            exit_code=EXIT_REFUSE,
        )

    # ---- Capabilities (Phase 1c-part-3) ----
    # Probes run in-session via the SessionStart hook (only Claude can invoke
    # deferred tools); python reads the snapshot they leave behind. The line
    # renders real probed state when a fresh schema-v2 snapshot exists, and
    # falls back to honest deferred-probe text otherwise.
    capabilities_line = render_capabilities_line(repo_root)

    # ---- Variables ----
    runtime = _runtime_vars(
        repo_root,
        repo_id=existing_repo_id,
        repo_slug=existing_repo_slug,
    )
    # Compute slug warning fresh on first install only — existing installs
    # reuse the already-locked-in slug. _runtime_vars already calls
    # _resolve_bridge_slug internally when no slug was provided; we re-resolve
    # here exactly once to capture the warning that _runtime_vars discards.
    slug_warning = None
    if not existing_repo_slug:
        runtime["repo_slug"], slug_warning = _resolve_bridge_slug(
            repo_root, runtime["repo_id"]
        )

    variables = _merged_vars(profile, runtime)

    # ---- Refuse before any write if a repo-local file maw owns sits behind a
    # symlink: .maw/ -> elsewhere would land config.yaml and state.json outside
    # the repo, and os.replace on a symlinked .gitignore would replace the
    # user's link with a plain file. Per-template targets, their
    # .maw-recommended siblings and the --force attic are checked per file in
    # plan_and_apply.
    for target in (_config_path(repo_root), state_path(repo_root),
                   repo_root / ".gitignore"):
        if through_symlink(repo_root, target):
            rel = target.relative_to(repo_root)
            raise InitRefusal(
                f"refusing: {rel} (or a directory above it) is a symlink, so "
                f"maw's write could land outside this repo or replace your link. "
                f"Make it a plain file/directory, then re-run.",
                exit_code=EXIT_REFUSE,
            )

    # ---- Plan + apply ----
    plan = InstallPlan(
        profile=profile,
        repo_root=repo_root,
        variables=variables,
        prior_state=prior_state,
        dry_run=args.dry_run,
        force=args.force,
        upgrade=args.upgrade,
    )

    report = plan_and_apply(plan)
    if adopting:
        _print_adoption(report, dry_run=args.dry_run)

    # Refuse on .maw/.runtime/ touches in --force mode per §5.
    if args.force:
        for action in report.actions:
            if action.target_rel.startswith(".maw/.runtime/"):
                raise InitRefusal(
                    f"--force refuses to touch .maw/.runtime/ "
                    f"(runtime state is out of scope for --force).",
                    exit_code=EXIT_REFUSE,
                )

    # ---- First-install side effects ----
    if not config_existed and not args.dry_run:
        config_text = render_config_yaml(profile, variables)
        _write_config(repo_root, config_text)
        # Drop the slug-claim marker so future inits in OTHER repos detect
        # the collision per §9.6.
        _write_bridge_marker(runtime["repo_slug"], runtime["repo_id"])

    if not args.dry_run:
        _ensure_gitignore_fragment(repo_root, dry_run=False)

    # ---- §7 state.json is LAST ----
    if not args.dry_run:
        write_state(repo_root, report.new_state)

    _print_banner(
        profile_name=profile.name,
        profile_origin=profile_origin,
        capabilities_line=capabilities_line,
        report=report,
        bridge_slug=runtime["repo_slug"],
        slug_warning=slug_warning,
        runtime_vars=variables,
        dry_run=args.dry_run,
        upgrade=args.upgrade,
        repo_root=repo_root,
    )
    return EXIT_OK


PRE_PUSH_REL = ".git/hooks/pre-push"


def _push_gate_state(report) -> str:
    """"maw" if maw's pre-push is (or would be) the hook in place, "kept-yours"
    if the repo keeps its own pre-push (so the MAW_HUMAN gate is off), "absent"
    if this profile ships no pre-push."""
    import init.install as inst
    ours = (inst.ACTION_INSTALL, inst.ACTION_SKIP_CURRENT, inst.ACTION_OVERWRITE_WITH_BACKUP,
            inst.ACTION_PLANNED, inst.ACTION_DRY_RUN_NO_CHANGE, inst.ACTION_PLANNED_OVERWRITE)
    state = "absent"
    for a in report.actions:
        if a.template != "git-hooks/pre-push":
            continue
        if a.target_rel == PRE_PUSH_REL and a.action in ours:
            return "maw"
        state = "kept-yours"
    return state


def _print_adoption(report, *, dry_run: bool) -> None:
    """Say exactly what adopting an existing .claude/ did (or would do)."""
    import init.install as inst
    added = [a.target_rel for a in report.actions
             if a.action in (inst.ACTION_INSTALL, inst.ACTION_PLANNED)]
    kept = [a.target_rel for a in report.actions
            if a.action in (inst.ACTION_WROTE_RECOMMENDED, inst.ACTION_PLANNED_RECOMMENDED,
                            inst.ACTION_SKIP_PRESERVING_DRIFT)]
    verb = "would add" if dry_run else "added"
    print("maw init: adopting an existing .claude/ that has no maw config.",
          file=sys.stderr)
    print(f"  {verb}: {', '.join(added) if added else 'nothing'}", file=sys.stderr)
    print(f"  kept yours (maw's version beside it as .maw-recommended): "
          f"{', '.join(kept) if kept else 'nothing'}", file=sys.stderr)
    refused = [a.target_rel for a in report.actions
               if a.action == inst.ACTION_REFUSED_SYMLINK]
    if refused:
        print(f"  refused (the path goes through a symlink): {', '.join(refused)}",
              file=sys.stderr)
    print("  every other existing file is untouched.", file=sys.stderr)


def _write_bridge_marker(slug: str, repo_id: str) -> None:
    """Drop ~/.claude/bridge/<slug>/.maw-repo-id so other inits detect us.

    Per contract §9.6: bridge-slug-collision detection needs a marker file.
    No-op if HOME isn't writable (e.g., tests with MAW_BRIDGE_HOME unset).
    """
    home_bridge = Path(os.environ.get("MAW_BRIDGE_HOME") or os.path.expanduser("~/.claude/bridge"))
    slug_dir = home_bridge / slug
    try:
        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / ".maw-repo-id").write_text(repo_id + "\n", encoding="utf-8")
    except OSError:
        # Best-effort; not having the marker degrades only the next /maw
        # init's collision detection, not this install's correctness.
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return cmd_init(args)


if __name__ == "__main__":
    sys.exit(main())
