"""Launch one seat of a maw pair, or both, from any repo.

    maw-seat advisor  [--repo PATH] [--model M] [--tmux] [--human] [--dry-run] [-- CLAUDE ARGS]
    maw-seat executor ...
    maw-seat pair     [--repo PATH] [--dry-run]

A seat is an interactive `claude` session started in the seat's own git worktree
(`net.worktree ensure`) with the environment the plugin reads:

    MAW_BRIDGE_ROLE   advisor | executor   (what seatcheck validates)
    MAW_NET_NAME      set only for a generational name (advisor2, executor3)
    MAW_NET_DIR       the repo's switchboard, ~/.claude/bridge/sw-<slug>
    MAW_SEAT_MODEL    the model the launcher asked for (the registry records it)

With no CLAUDE ARGS the session's first prompt is `/maw:<role>`, which brings the
seat online. Anything after `--` replaces that and is passed to `claude` as is
(for example your own permission or remote-control flags).

`--human` also sets MAW_HUMAN=1. That variable is the marker a repo's pre-push
hook trusts to mean "a human launched this session by hand"; it is off by
default so a script, cron job or agent that runs this launcher does not claim
to be you.

`pair` opens a tmux session with the two seats side by side, or re-attaches to
it if it is already running. Without tmux, run `maw-seat advisor` and
`maw-seat executor` in two terminals.

Both seats get the same MAW_NET_DIR, so they share one switchboard; anything
you start from a seat's session (its hooks, a watcher) inherits it. A watcher
you start by hand elsewhere must set the same MAW_NET_DIR to see the pair.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from net import worktree

# The plugin root this launcher runs from (the dir holding net/, bridge/, ...).
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "opus"


@dataclass
class SeatLaunch:
    role: str
    name: str
    cwd: Path
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


def role_of(name: str) -> str:
    for role in worktree.SEATS:
        if name == role or (name.startswith(role) and name[len(role):].isdigit()):
            return role
    raise ValueError(f"unknown seat {name!r}: expected advisor or executor "
                     f"(optionally with a generation number, e.g. advisor2)")


def net_dir(slug: str) -> Path:
    return Path.home() / ".claude" / "bridge" / f"sw-{slug}"


def plan(name: str, worktree_path: Path, slug: str, *, model: "str | None" = DEFAULT_MODEL,
         human: bool = False, claude_args: "list[str] | None" = None) -> SeatLaunch:
    """The exact command and environment for one seat. Pure: runs nothing."""
    role = role_of(name)
    env = {
        "MAW_BRIDGE_ROLE": role,
        "MAW_NET_DIR": str(net_dir(slug)),
    }
    if name != role:
        env["MAW_NET_NAME"] = name
    if model:
        env["MAW_SEAT_MODEL"] = model
    if human:
        env["MAW_HUMAN"] = "1"
    argv = ["claude"]
    if model:
        argv += ["--model", model]
    argv += claude_args if claude_args else [f"/maw:{role}"]
    return SeatLaunch(role=role, name=name, cwd=worktree_path, argv=argv, env=env)


# Seat-identity variables a parent shell may carry from ANOTHER seat (e.g. an
# executor launched from an advisor2 pane). They are removed unless this
# launch sets them, so an inherited name or human marker never leaks in.
SCRUBBED = ("MAW_NET_NAME", "MAW_HUMAN")


def child_env(launch: SeatLaunch, base: "dict[str, str]") -> "dict[str, str]":
    env = {k: v for k, v in base.items() if k not in SCRUBBED}
    env.update(launch.env)
    return env


def shell_line(launch: SeatLaunch) -> str:
    """One shell command that reproduces the launch (for tmux or a printout)."""
    words = ["env"]
    words += [w for k in SCRUBBED if k not in launch.env for w in ("-u", k)]
    words += [f"{k}={v}" for k, v in sorted(launch.env.items())]
    words += launch.argv
    return f"cd {shlex.quote(str(launch.cwd))} && " + " ".join(shlex.quote(w) for w in words)


LAUNCH_GOAL = "grounding: seat launched, goal not yet confirmed"


_CLAIMABLE = ("wrapped", "idle")


def launch_claim(role: str, main_root: str, *, claimer=None, reader=None) -> bool:
    """Claim the repo's board slot at LAUNCH (#192), advisor seat only.

    The advisor is the principal that owns the slot; the executor never writes
    it. The goal reads "grounding" until the advisor re-claims at goal-confirm
    (advisor.md 2f), which refreshes the goal and armed window. Fire-and-forget:
    any board miss returns False and never blocks the launch.

    A RELAUNCH mid-run must not wipe the live goal/shipped list: the slot is
    claimed only when it is missing or wrapped/idle; a live fragment is left
    untouched (returns False)."""
    if role != "advisor":
        return False
    try:
        from net import board
        if claimer is None:
            claimer = board.claim
        if reader is None:
            reader = lambda root: board._existing(board._repo_slug(root))  # noqa: E731
        prev = reader(main_root)
        if prev and prev.get("status") not in _CLAIMABLE:
            return False
        return bool(claimer(goal_plain=LAUNCH_GOAL, repo_root=main_root))
    except Exception:  # noqa: BLE001 - a board miss must never block a launch
        return False


def boot_banner(role: str, slug: str, env: "dict[str, str]",
                peers: "list | None" = None) -> "list[str]":
    """Boot lines naming this seat's herdr pane and its peer's (#192).

    Empty outside herdr. The peer is looked up in the repo's switchboard; a
    peer that has not registered yet is said so, never guessed."""
    from net import registry
    mine = registry.detect_herdr_pane(env)
    if mine is None:
        return []
    if peers is None:
        old = os.environ.get("MAW_NET_DIR")
        os.environ["MAW_NET_DIR"] = env.get("MAW_NET_DIR", str(net_dir(slug)))
        try:
            peers = registry.who(prune=False)
        except Exception:  # noqa: BLE001
            peers = []
        finally:
            if old is None:
                os.environ.pop("MAW_NET_DIR", None)
            else:
                os.environ["MAW_NET_DIR"] = old
    other = "executor" if role == "advisor" else "advisor"
    found = [e for e in peers if e.role == other and getattr(e, "herdr_pane", None)]
    peer = found[0].herdr_pane if len(found) == 1 else None
    return [
        f"maw-seat: {role} in herdr pane {mine}",
        f"maw-seat: peer {other} pane: {peer or 'not registered yet (net.cli who)'}; "
        f"pane-peek (herdr pane read) is a first-class move when a peer goes quiet",
    ]


def _ensure(role: str, repo: "str | None") -> "worktree.EnsureResult":
    return worktree.ensure(role, cwd=repo)


def _self_cmd(name: str, repo: str, extra: "list[str]",
              claude_args: "list[str] | None" = None) -> str:
    launcher = PLUGIN_ROOT / "bin" / "maw-seat"
    tail = ["--", *claude_args] if claude_args else []
    return " ".join(shlex.quote(a) for a in [str(launcher), name, "--repo", repo, *extra, *tail])


def pair_commands(repo: str, slug: str, extra: "list[str]",
                  exists: bool = False,
                  claude_args: "list[str] | None" = None) -> "list[list[str]]":
    """tmux commands for a side-by-side pair (advisor left, executor right).
    When the pair's session already exists, only attach to it."""
    session = f"{slug}-pair"
    if exists:
        return [["tmux", "attach-session", "-t", session]]
    return [
        ["tmux", "new-session", "-d", "-s", session,
         _self_cmd("advisor", repo, extra, claude_args)],
        ["tmux", "split-window", "-h", "-t", session,
         _self_cmd("executor", repo, extra, claude_args)],
        ["tmux", "attach-session", "-t", session],
    ]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maw-seat", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("seat", help="advisor, executor (or advisor2, ...), or pair")
    p.add_argument("--repo", default=None, help="repo to seat in (default: cwd)")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"model for claude --model (default: {DEFAULT_MODEL}); "
                        f"'' to let claude choose")
    p.add_argument("--tmux", action="store_true",
                   help="run the seat in its own tmux session (re-attaches if it exists)")
    p.add_argument("--human", action="store_true",
                   help="also set MAW_HUMAN=1, the marker a pre-push hook trusts to "
                        "mean a human launched this by hand. Off by default so an "
                        "automated caller never claims to be you")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would run; create nothing, start nothing")
    return p


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    claude_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, claude_args = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    try:
        repo = str(Path(args.repo).resolve()) if args.repo else os.getcwd()
    except FileNotFoundError:
        print("maw-seat: the current directory no longer exists; "
              "cd somewhere real or pass --repo <path>", file=sys.stderr)
        return 2
    if not os.path.isdir(repo):
        print(f"maw-seat: --repo {args.repo}: no such directory", file=sys.stderr)
        return 2
    try:
        main_root = worktree.main_worktree_root(repo)
    except (worktree.WorktreeError, OSError) as e:
        print(f"maw-seat: {e}", file=sys.stderr)
        return 2
    slug = main_root.name

    if args.seat == "pair":
        if args.tmux:
            print("maw-seat: pair always runs in tmux; drop --tmux", file=sys.stderr)
            return 2
        extra = [a for a, on in (("--human", args.human),) if on]
        if args.model != DEFAULT_MODEL:
            extra += ["--model", args.model]
        import subprocess
        exists = bool(shutil.which("tmux")) and subprocess.run(
            ["tmux", "has-session", "-t", f"{slug}-pair"],
            capture_output=True).returncode == 0
        cmds = pair_commands(str(main_root), slug, extra, exists=exists,
                             claude_args=claude_args)
        if exists and (extra or claude_args):
            print(f"maw-seat: {slug}-pair is already running; attaching. The options "
                  f"given now (--human, --model, args after --) are not applied to it.",
                  file=sys.stderr)
        if args.dry_run or not shutil.which("tmux"):
            if not args.dry_run:
                print("maw-seat: tmux not found; run these two in separate terminals:",
                      file=sys.stderr)
                print(_self_cmd("advisor", str(main_root), extra, claude_args))
                print(_self_cmd("executor", str(main_root), extra, claude_args))
                return 1
            for c in cmds:
                print(" ".join(shlex.quote(a) for a in c))
            return 0
        for c in cmds[:-1]:
            if subprocess.run(c).returncode != 0:
                return 1
        os.execvp(cmds[-1][0], cmds[-1])

    try:
        role = role_of(args.seat)
    except ValueError as e:
        print(f"maw-seat: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        wt = worktree.seat_worktree_path(role, str(main_root))
    else:
        try:
            wt = _ensure(role, str(main_root)).path
        except worktree.WorktreeError as e:
            print(f"maw-seat: {e}", file=sys.stderr)
            return 1
    launch = plan(args.seat, wt, slug, model=args.model or None, human=args.human,
                  claude_args=claude_args)
    if not args.dry_run:
        launch_claim(role, str(main_root))
        # NOTE: with --tmux this banner prints to the launching terminal before
        # tmux takes it over; it is not injected into the seat's prompt. The
        # same facts are available in-seat via `net.cli who`.
        for line in boot_banner(role, slug, child_env(launch, dict(os.environ))):
            print(line, file=sys.stderr)
    if args.tmux:
        session = f"{slug}-{args.seat}"
        cmd = ["tmux", "new-session", "-As", session, shell_line(launch)]
        if args.dry_run:
            print(" ".join(shlex.quote(a) for a in cmd))
            return 0
        os.execvp("tmux", cmd)
    if args.dry_run:
        print(shell_line(launch))
        return 0
    os.chdir(launch.cwd)
    os.execvpe(launch.argv[0], launch.argv, child_env(launch, dict(os.environ)))
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
