"""init.status — entry point for `/maw:status` (v0.6 PRD §3.3, Phase 1d).

Read-only observability over everything the substrate leaves on disk:

  [1] Substrate  — .maw/state.json (version, profile, file count, drift)
  [2] Capabilities — probed snapshot via init.probe.capabilities_line
  [3] Bridge     — slug + bridge dir presence (turn detail is /maw:bridge-status)
  [4] Sensors    — optional sensor client: not installed, off, or on
  [5] Merge posture — echo vs no-echo for this repo, advisor armed or not, expiry

Always exits 0: status never refuses, it reports. A repo with no substrate
gets "not installed" lines, not an error — the command's job is to make the
current state legible, whatever that state is.
"""
from __future__ import annotations

import sys
from pathlib import Path

from init.capabilities import CapabilityError
from init.probe import capabilities_line
from init.profile import read_repo_slug_from_config
from init.state import StateError, read_state
from init.substrate import drift_warning as substrate_drift

BRIDGE_HOME = Path.home() / ".claude" / "bridge"


def _substrate_line(repo_root: Path) -> str:
    # read_state wraps malformed *content* in StateError but lets I/O-layer
    # failures (PermissionError, UnicodeDecodeError) escape raw — and
    # "status never refuses" has to survive those too.
    try:
        state = read_state(repo_root)
    except (StateError, OSError, UnicodeDecodeError) as e:
        return f"state.json unreadable ({e!s}) — re-run /maw:init"
    if state is None:
        return "not installed (run /maw:init)"

    drift = sum(
        1
        for target_rel in state.files
        if (repo_root / f"{target_rel}.maw-recommended").exists()
    )
    drift_note = f", {drift} drift sibling(s) pending review" if drift else ""
    stamp = state.substrate_committed_at[:10] or "unstamped"
    # The manifest version cannot date a transplant (it never moves), so the
    # line carries the plugin COMMIT date and says outright when it is behind.
    stale = " — STALE, run /maw:init --upgrade" if substrate_drift(repo_root) else ""
    return (
        f"installed v{state.substrate_version or '?'} "
        f"from plugin {state.substrate_sha[:7] or '?'} ({stamp}){stale} "
        f"profile={state.profile or '?'} at {state.installed_at or '?'} "
        f"({len(state.files)} files tracked{drift_note})"
    )


def _capabilities_line(repo_root: Path) -> str:
    try:
        return capabilities_line(repo_root)
    except CapabilityError as e:
        return f"unreadable ({e!s})"


def _bridge_line(repo_root: Path) -> str:
    try:
        config_text = (repo_root / ".maw" / "config.yaml").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "no .maw/config.yaml (run /maw:init)"
    slug = read_repo_slug_from_config(config_text)
    if not slug:
        return "no slug in .maw/config.yaml (run /maw:init)"
    bridge_dir = BRIDGE_HOME / slug
    presence = "exists" if bridge_dir.is_dir() else "not created yet"
    return f"slug={slug} dir={bridge_dir} ({presence}) — /maw:bridge-status for turns"


def _jev_line() -> str:
    try:
        from jev.client import status as jev_status
    except ModuleNotFoundError:
        return "not installed"
    except Exception as e:
        return f"client unavailable ({e!s})"
    enabled, why = jev_status()
    return why if enabled else f"{why}; sensors emit no signal"


def _merge_posture_line(repo_root: Path) -> str:
    """Echo posture + advisor arm state, from session_auth's own helpers.

    Read-only: it classifies (requires_echo / _repo_slug), then reads the arm
    through peer_status (the registered advisor's token) and, failing that,
    status (a token in this tree). It never arms and never writes.
    """
    try:
        from guards import session_auth as sa
        from net import registry
    except Exception as e:
        return f"unknown (session_auth unavailable: {e!s})"
    try:
        slug = sa._repo_slug(repo_root)
        posture = "echo-required" if sa.requires_echo(slug) else "no-echo"
        posture_text = f"{posture} (repo={slug})"
    except Exception as e:
        return f"unknown (posture unreadable: {e!s})"
    try:
        armed, msg = False, ""
        try:
            def _in_repo(cmd, **kw):
                return sa._run([cmd[0], "-C", str(repo_root), *cmd[1:]], **kw)

            common = registry.git_common_dir(_in_repo)
            armed, msg = sa.peer_status(common, registry.who(prune=False))
        except Exception as e:
            msg = f"peer read failed: {e!s}"
        if not armed:
            local_armed, local_msg = sa.status(repo_root)
            if local_armed:
                armed, msg = True, local_msg
            elif not msg or "no-advisor" in msg:
                msg = local_msg
        return f"{posture_text}; arm: {msg}"
    except Exception as e:
        return f"{posture_text}; arm state unknown ({e!s})"


def render_status(repo_root: Path) -> str:
    lines = [
        f"maw status — {repo_root}",
        f"[1] Substrate: {_substrate_line(repo_root)}",
        f"[2] Capabilities: {_capabilities_line(repo_root)}",
        f"[3] Bridge: {_bridge_line(repo_root)}",
        f"[4] Sensors: {_jev_line()}",
        f"[5] Merge posture: {_merge_posture_line(repo_root)}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="init.status")
    parser.add_argument("--repo-root", default=".", type=Path)
    args = parser.parse_args(argv)
    sys.stdout.write(render_status(args.repo_root.resolve()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
