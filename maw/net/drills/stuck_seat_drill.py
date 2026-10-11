"""net.drills.stuck_seat_drill — the reliability-run RED DRILL (lane 0).

Reproduces the 2026-07-21 stall class: a seat that is registry-alive
and stalled + quiet — no new commit, no heartbeat advance, no waiting wire or
durable-bridge turn — for well past every existing alarm threshold, while both
supervision layers missed it. The incident had TWO failure halves, and the drill
models BOTH (select with --status):

  DETECTION gap (--status working, the stalled seat's tail state): the sweep is
    SILENT even when run. dark_sessions is REFINED by herdr status (A3):
    status=working reclassifies the silence as "heads-down, not dark" and
    suppresses the finding; deadlock_pairs needs BOTH seats parked on an unpulled
    durable inbox; parked_wires needs an unconsumed wire turn; genuinely_dark is
    OPT-IN and OFF by default (it false-pages). So "working + no progress +
    nothing waiting" trips nothing. Lane 1's stuck-while-working finding closes
    this — the drill is its red->green gauge.

  CONSUMPTION gap (--status blocked, the status the stalled seat actually
    reported): the sweep FIRES a blocked-seat finding today — detection is
    present — but NOTHING consumes or escalates it. No watchdog wakes the peer
    (lane 2) and no dead-man page reaches the operator (lane 3). The signal existed and no
    one was watching. Lanes 2/3 close this; the machine-assertable escalation
    red-first lands in lane 3.

Runnable, fully hermetic:
  python3 -m net.drills.stuck_seat_drill [--silent-min N] [--status working|blocked]
It seeds its own temp MAW_NET_DIR + HOME roots, runs the real sweep at a
fast-forwarded clock, and reports the baseline. working-mode: exit 0 = detection
gap reproduced (sweep silent), exit 1 = detection gap closed (lane 1 landed).
blocked-mode: exit 0 = consumption-gap baseline (blocked-seat fires, unconsumed).

NOTHING real is wedged. The "stuck seat" is a fake registry entry plus a quiet
temp worktree; no real seat, registry, bridge, or herdr pane is touched.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from net import watch
from net.registry import Entry, pid_alive, registry_dir

# A deterministic "now" (noon UTC, as a true epoch via timegm — the same UTC
# math the code under test uses). last_seen is set `silent_min` before this, so
# the sweep runs against a clock fast-forwarded past every threshold.
FIXED_NOW_S = float(
    calendar.timegm(time.strptime("2026-06-11T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
)


def _ago(now_s: float, minutes: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s - minutes * 60))


class WorkingHerdr:
    """A herdr `pane get` runner that reports agent_status=working for every
    pane — the very signal that (correctly) says "an agent is present" but (in
    the stall) hides that no PROGRESS is being made. Any non-herdr command
    returns empty success, so pane_exists and other probes stay benign."""

    def __init__(self, status: str = "working") -> None:
        self.status = status
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], stdin_text: str | None = None):
        self.calls.append(cmd)
        if cmd[:3] == ["herdr", "pane", "get"]:
            body = json.dumps({"result": {"pane": {"agent_status": self.status}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _write_entry(name: str, role: str, *, last_seen: str, herdr_pane: str,
                 pid: int) -> None:
    """Seed one hermetic, herdr-hosted registry entry stamped with `pid`."""
    entry = Entry(
        name=name,
        role=role,
        repo_root="/repo",
        cwd="/repo",
        pid=pid,  # must be ALIVE, or who(prune=True) reaps it before the sweep
        tty=None,
        tmux_pane=None,
        session_id=f"{name}-drill",
        started_at=last_seen,
        last_seen=last_seen,
        transport={"herdr_push": True},
        mux="herdr",
        herdr_pane=herdr_pane,
    )
    rdir = registry_dir()
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{name}.json").write_text(json.dumps(entry.__dict__), encoding="utf-8")


def seed_stuck_pair(
    now_s: float,
    *,
    silent_min: int = 31,
    peer_fresh_min: int = 2,
    stuck_role: str = "executor",
    status: str = "working",
    pid: int | None = None,
) -> WorkingHerdr:
    """Seed a hermetic advisor+executor pair (shared bridge slug 'repo') where
    `stuck_role` is stalled + quiet: registry-alive, silent `silent_min` minutes,
    herdr agent_status=`status`, no wire turn, no durable inbox. The peer is fresh
    (`peer_fresh_min` ago) so it is under threshold and never itself a candidate —
    the stuck seat is the only thing the sweep could flag.

    `status` selects which HALF of F10 the fixture models (the 2026-07-21 incident
    exhibited BOTH):
      - "working": the DETECTION gap — the sweep is SILENT even when run (herdr
        working suppresses the dark finding via A3; lane 1's target).
      - "blocked": the CONSUMPTION gap — the sweep FIRES a blocked-seat finding
        today, but nothing consumes or escalates it (lanes 2/3's target).

    Requires MAW_NET_DIR (registry root) and HOME (durable-bridge root) to point
    at temp dirs; `main()` sets both, the test's setUp sets both. Returns the
    WorkingHerdr runner to pass to watch.sweep().
    """
    the_pid = os.getpid() if pid is None else pid
    supervisor = "advisor" if stuck_role == "executor" else "executor"
    _write_entry(supervisor, supervisor, last_seen=_ago(now_s, peer_fresh_min),
                 herdr_pane="1-1", pid=the_pid)
    _write_entry(stuck_role, stuck_role, last_seen=_ago(now_s, silent_min),
                 herdr_pane="2-3", pid=the_pid)
    return WorkingHerdr(status)


def baseline_findings(now_s: float, runner: WorkingHerdr) -> list[watch.Finding]:
    """The REAL sweep, deployed defaults, at the fast-forwarded clock."""
    return watch.sweep(now_s=now_s, runner=runner)


DRILL_SESSION_SUFFIX = "-drill"
MIN_HOLD_SECONDS = 120


def _live_switchboards() -> list[Path]:
    """Every directory that is, or plausibly is, a LIVE switchboard.

    Guarding one hardcoded path guards the one directory nobody uses: the live
    switchboard is PER-REPO (`MAW_NET_DIR=~/.claude/bridge/sw-<repo>`), and this
    machine has ten of them (#211 review). So the set is discovered, never named:

      - the AMBIENT MAW_NET_DIR — the one this process inherited from the seat
        env, read BEFORE the drill overrides it;
      - the default switchboard;
      - every directory under ~/.claude/bridge that has a `registry/` child —
        that is what a switchboard IS, and it catches the other pairs' dirs, not
        just this session's. No "sw-*" name matching: the operator can point
        MAW_NET_DIR anywhere.
    """
    out: list[Path] = []
    ambient = (os.environ.get("MAW_NET_DIR") or "").strip()
    if ambient:
        out.append(Path(ambient))
    bridge = Path.home() / ".claude" / "bridge"
    out.append(bridge / "switchboard")
    try:
        out.extend(d for d in bridge.iterdir() if (d / "registry").is_dir())
    except OSError:
        pass
    return out


def _holds_a_live_foreign_seat(net_dir: Path) -> bool:
    """True if this dir's registry holds a pid-alive entry the DRILL did not write.

    The structural rule, independent of knowing where the real switchboards are: a
    scratch dir is empty, a live switchboard is not. Drill-seeded entries are
    exempt (their session_id carries DRILL_SESSION_SUFFIX) so re-seeding a scratch
    dir the harness owns still works. Anything unreadable counts as live —
    "cannot prove it is empty" refuses, the same shape as the OSError path.
    """
    reg = net_dir / "registry"
    if not reg.is_dir():
        return False
    try:
        candidates = list(reg.glob("*.json"))
    except OSError:
        return True
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True  # an unreadable entry is not proof of an empty dir
        if not isinstance(data, dict):
            return True
        if str(data.get("session_id", "")).endswith(DRILL_SESSION_SUFFIX):
            continue  # our own fixture
        pid = data.get("pid")
        if isinstance(pid, int) and pid_alive(pid):
            return True
    return False


def _refuse_reason(path: Path) -> str | None:
    """Why seeding into `path` is refused, or None if it is safe.

    A drill that can seed fake stalls into a live registry is a hazard, not a
    harness: real seats sweep that dir every 45s and the dead-man pages a human.
    """
    try:
        target = path.expanduser().resolve()
    except OSError:
        return "the path cannot be resolved, so it cannot be proven safe"
    for live in _live_switchboards():
        try:
            resolved = live.expanduser().resolve()
        except OSError:
            continue
        if target == resolved:
            return f"it IS a live switchboard ({resolved})"
        if resolved.is_relative_to(target):
            return f"it CONTAINS a live switchboard ({resolved})"
    if _holds_a_live_foreign_seat(target):
        return ("its registry already holds a pid-alive seat this drill did not "
                "write — a scratch dir is empty, a live switchboard is not")
    return None


def _seed_only(args) -> int:
    """Seed a scratch registry and exit, printing the follow-up commands.

    This is the seam #191 asks for. Seeding used to stamp `os.getpid()`, so a
    `python3 -c ...` seed left a DEAD pid behind: `who(prune=True)` reaped the
    seeded seat and the sweep reported nothing — a drill that looks like a passing
    system. Here the pid is explicit (or held by a spawned holder) and verified
    alive before anything is written.
    """
    net_dir = Path(args.net_dir)
    refusal = _refuse_reason(net_dir)
    if refusal is not None:
        print(f"REFUSING to seed into {net_dir}: {refusal}. Seed a scratch dir "
              f"(mktemp -d) — a seeded stall in a live registry is swept by real "
              f"seats and pages a real human.")
        return 2

    holder = None
    if args.hold_seconds is not None:
        holder = subprocess.Popen(["sleep", str(args.hold_seconds)])
        pid = holder.pid
    else:
        pid = os.getpid() if args.pid is None else args.pid

    if not pid_alive(pid):
        print(f"REFUSING: pid {pid} is not alive. who(prune=True) reaps a dead pid "
              f"before the sweep runs, so the drill would report 'no finding' and "
              f"look like a passing system — the exact #191 footgun. Hold a pid "
              f"(`sleep 600 & echo $!`) or use --hold-seconds.")
        return 2

    # Point the registry at the scratch dir for the write, then put the ambient
    # value BACK: leaving the override behind would make a second seed read its
    # own scratch dir as "the ambient live switchboard" and refuse itself.
    previous = os.environ.get("MAW_NET_DIR")
    try:
        os.environ["MAW_NET_DIR"] = str(net_dir)
        seed_stuck_pair(FIXED_NOW_S, silent_min=args.silent_min, status=args.status,
                        pid=pid)
    finally:
        if previous is None:
            os.environ.pop("MAW_NET_DIR", None)
        else:
            os.environ["MAW_NET_DIR"] = previous
    print(f"seeded a stalled + quiet executor (status={args.status}, silent "
          f"{args.silent_min} min) into {net_dir}, pid {pid}"
          + (f" (holder exits in {args.hold_seconds}s)" if holder else ""))
    print("\nrun the sweep and the dead-man as SEPARATE invocations, with HOME "
          "pointed at a scratch dir so the durable-bridge read stays hermetic:")
    print(f"  MAW_NET_DIR={net_dir} HOME=$(mktemp -d) \\")
    print(f"    python3 -m net.watch --for executor --no-header")
    print(f"  MAW_NET_DIR={net_dir} HOME=$(mktemp -d) \\")
    print(f"    python3 -m net.deadman check --for executor --escalate-min 1")
    print("\nNOTE: cross-process, herdr status is read LIVE from the pane, and the "
          "seeded pane (2-3) does not exist — so the sweep reads 'unreadable' and "
          "reports dark-session. That is why --status is refused here: the "
          "blocked-seat half only exists in hermetic mode, where the fake herdr "
          "runner is in play.")
    print("\nNOTE: the seeded entries carry a REAL now-relative clock only in the "
          "sweep's eyes — last_seen is stamped relative to the drill's fixed epoch, "
          "so a live sweep reads the seat as silent for years, well past every "
          "threshold. That is intended: the drill measures the escalation path, "
          "not the threshold arithmetic.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reliability-run red drill: seeded stalled + quiet seat.")
    ap.add_argument(
        "--silent-min",
        type=int,
        default=31,
        help="minutes the stuck seat has been silent (default 31, past the 30-min bar).",
    )
    ap.add_argument(
        "--status",
        choices=["working", "blocked"],
        default=None,
        help="the herdr status the stalled seat reports. 'working' models the "
        "DETECTION gap (sweep silent even when run — lane 1's target); 'blocked' "
        "models the CONSUMPTION gap (a blocked-seat finding fires today but "
        "nothing consumes/escalates it — lanes 2/3's target). The stalled seat "
        "reported BLOCKED, so both halves are real.",
    )
    ap.add_argument(
        "--seed-only",
        action="store_true",
        help="seed the registry into --net-dir and EXIT, without sweeping — so the "
        "sweep and the dead-man run as separate CLI invocations, the way a seat "
        "actually runs them (#191).",
    )
    ap.add_argument("--net-dir", default=None,
                    help="the scratch MAW_NET_DIR to seed into (required with "
                         "--seed-only; refuses the real switchboard).")
    ap.add_argument("--pid", type=int, default=None,
                    help="stamp this pid on the seeded seats. It must be ALIVE for "
                         "the whole drill: who(prune=True) reaps a dead pid, and the "
                         "sweep then reports nothing — the #191 footgun.")
    ap.add_argument("--hold-seconds", type=int, default=None,
                    help="spawn a `sleep N` holder process and stamp ITS pid, so the "
                         "seeded seat outlives this process without you managing one.")
    args = ap.parse_args(argv)

    if args.pid is not None and args.hold_seconds is not None:
        ap.error("--pid and --hold-seconds both choose the seeded pid; pass one")
    if args.hold_seconds is not None and args.hold_seconds < MIN_HOLD_SECONDS:
        ap.error(f"--hold-seconds must be at least {MIN_HOLD_SECONDS}: a holder that "
                 f"dies mid-drill leaves the seeded seat reaped and the sweep silent "
                 f"— the same false-green this mode exists to remove, just later")
    if args.seed_only:
        if not args.net_dir:
            ap.error("--seed-only needs --net-dir (a scratch dir, never the real one)")
        if args.status is not None:
            ap.error("--status does not apply to --seed-only: cross-process the herdr "
                     "status is read LIVE from a pane that does not exist, so the "
                     "sweep reports dark-session whatever you pass. Use hermetic mode "
                     "for the blocked-seat half.")
        args.status = "working"
        return _seed_only(args)
    args.status = args.status or "working"
    if args.net_dir or args.pid is not None or args.hold_seconds is not None:
        ap.error("--net-dir/--pid/--hold-seconds apply to --seed-only; the hermetic "
                 "mode makes its own temp dirs and uses this process's pid")

    with tempfile.TemporaryDirectory() as net_dir, tempfile.TemporaryDirectory() as home:
        os.environ["MAW_NET_DIR"] = net_dir
        os.environ["HOME"] = home
        now_s = FIXED_NOW_S
        runner = seed_stuck_pair(now_s, silent_min=args.silent_min, status=args.status)
        findings = baseline_findings(now_s, runner)

        print(
            f"drill: seeded a stalled + quiet executor "
            f"(registry-alive, herdr status={args.status}, silent {args.silent_min} min, "
            f"no commits / no wire turn / no durable inbox)."
        )
        if args.status == "working":
            # The DETECTION gap. Pre-lane-1 the sweep is silent; post-lane-1 the
            # stuck-while-working finding fires (the drill is the red->green gauge).
            if not findings:
                print(
                    f"DETECTION-GAP BASELINE REPRODUCED: the net.watch sweep is SILENT "
                    f"past {args.silent_min} min even when run — the stall is invisible. "
                    f"This is the hole lane 1's stuck-while-working finding closes."
                )
                return 0
            print(f"DETECTION GAP CLOSED: the sweep now fires {len(findings)} finding(s):")
            for f in findings:
                print("  " + f.line())
            return 1
        # args.status == "blocked" — the CONSUMPTION gap. The finding EXISTS today;
        # the failure is that nothing consumes or escalates it. The machine-
        # assertable escalation red-first lives in lane 3 (dead-man page); here we
        # demonstrate the finding fires and state the consumption gap plainly.
        kinds = {f.kind for f in findings}
        if "blocked-seat" in kinds:
            print(
                f"CONSUMPTION-GAP BASELINE: blocked+quiet already FIRES a blocked-seat "
                f"finding — detection is present today:"
            )
            for f in findings:
                print("  " + f.line())
            print(
                "  ...but NOTHING consumes or escalates it: no watchdog wakes the peer "
                "(lane 2) and no dead-man page reaches the operator (lane 3). That is the "
                "2026-07-21 failure — the signal existed and no one was watching. The "
                "machine-assertable escalation red-first lands in lane 3."
            )
            return 0
        print("UNEXPECTED: blocked+quiet did not surface a blocked-seat finding:")
        for f in findings:
            print("  " + f.line())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
