"""Session registry — who is alive, where, and how to reach them.

Each live session registers as one JSON file in the registry dir
(default ~/.claude/bridge/switchboard/registry/<name>.json, override via
MAW_NET_DIR). Liveness is probed at read time against the recorded pid,
so a crashed session disappears on the next `net who` without any
manual cleanup — the registry never requires a clean shutdown.

Transport capability is recorded per session: a session outside tmux is
push-deaf (tmux send-keys cannot reach a plain terminal tab), so peers
must know whether the wake line works before they try it.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from bridge.format import _acquire_lock, _release_lock

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STALE_AFTER_S = 6 * 3600  # last_seen older than this is flagged (not pruned)

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class NetRegistryError(Exception):
    pass


def _run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def net_dir() -> Path:
    override = os.environ.get("MAW_NET_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "bridge" / "switchboard"


def registry_dir() -> Path:
    return net_dir() / "registry"


def utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Entry:
    name: str
    role: str
    repo_root: str
    cwd: str
    pid: int
    tty: str | None
    tmux_pane: str | None
    session_id: str
    started_at: str
    last_seen: str
    transport: dict[str, bool] = field(default_factory=dict)
    # The shared .git path — the LOGICAL-repo identity. Defaulted so entries
    # written before this field (cached-code registrations) still load. Two
    # seats in two per-seat worktrees of one repo (backlog item 1) share this
    # even though their repo_root/cwd differ; consumers group by it.
    repo_common_dir: str = ""
    # Which multiplexer hosts this seat: "tmux" (default, the wake line is
    # tmux send-keys) or "herdr" (the wake line is the herdr socket). Defaulted
    # so pre-herdr entries still load; peers pick the transport off this field.
    mux: str = "tmux"
    # The herdr pane id (HERDR_PANE_ID) when mux == "herdr", else None. This is
    # the launch-env provenance signal a paste cannot forge — the herdr analog
    # of the tmux %N tell (see seatcheck).
    herdr_pane: str | None = None
    # The model the launcher PINNED for this seat (MAW_SEAT_MODEL), e.g.
    # "opus" for both seats by default since 2026-09-22 (a run may pin
    # "fable" via MAW_ADVISOR_MODEL / MAW_EXECUTOR_MODEL).
    # This is the launcher's INTENT, not the live model — pulse compares it to
    # the transcript's live model to catch a fallback/flip (B3). "" when the
    # launcher didn't pin one (a bare/legacy launch). Defaulted for forward-compat.
    model: str = ""
    # The seat's OWN Claude transcript (the SessionStart hook's `transcript_path`).
    # Pinned here because a project dir holds a transcript per SESSION, not per
    # seat — headless children and rotated seats share the seat's cwd — so
    # "newest file in the dir" lets a sibling vouch for a wedged seat. Defaulted
    # so entries written before this field (and hand-registered seats) still load;
    # net.liveness treats an empty value as the weaker dir-max basis and says so.
    transcript_path: str = ""

    @property
    def tmux_push(self) -> bool:
        return bool(self.transport.get("tmux_push"))

    @property
    def herdr_push(self) -> bool:
        return bool(self.transport.get("herdr_push"))

    @property
    def pushable(self) -> bool:
        """Reachable on the wake line by any transport."""
        return self.tmux_push or self.herdr_push


def _proc_row(pid: int, runner: Runner = _run) -> tuple[int, str, str] | None:
    """Return (ppid, tty, command) for pid, or None if it's gone."""
    try:
        cp = runner(["ps", "-o", "ppid=,tty=,command=", "-p", str(pid)])
    except (OSError, subprocess.SubprocessError):
        return None
    line = cp.stdout.strip()
    if cp.returncode != 0 or not line:
        return None
    parts = line.split(None, 2)
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), parts[1], parts[2]
    except ValueError:
        return None


def find_claude_ancestor(
    start_pid: int | None = None, runner: Runner = _run
) -> tuple[int, str | None]:
    """Walk the process tree upward to the owning claude process.

    Registration runs as a Bash-tool child of the claude CLI, so the
    nearest ancestor whose command names `claude` is the session itself.
    Falls back to the immediate parent if no claude ancestor is found
    (e.g. invoked from a bare shell for testing).
    """
    pid = start_pid if start_pid is not None else os.getpid()
    fallback = (os.getppid(), None)
    for _ in range(20):
        row = _proc_row(pid, runner)
        if row is None:
            break
        ppid, tty, command = row
        exe = command.split()[0] if command.split() else ""
        if "claude" in os.path.basename(exe) or " claude" in command[:80]:
            return pid, (None if tty in ("??", "-") else f"/dev/{tty}")
        if ppid <= 1:
            break
        pid = ppid
    return fallback


def detect_tmux_pane(tty: str | None, runner: Runner = _run) -> str | None:
    """Resolve the tmux pane housing this session, if any.

    $TMUX_PANE is inherited from a tmux-launched claude; otherwise match
    the claude process tty against the pane list (covers env-stripped
    subprocesses).
    """
    env_pane = os.environ.get("TMUX_PANE")
    if env_pane:
        return env_pane
    if not tty:
        return None
    try:
        cp = runner(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_tty}"])
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == tty:
            return parts[0]
    return None


def detect_herdr_pane(env: dict[str, str] | None = None) -> str | None:
    """The herdr pane id for this session, or None if not a herdr seat.

    A herdr-managed pane carries HERDR_ENV=1 plus HERDR_PANE_ID (and friends).
    Both are set by herdr before the session has any agency, so — like the
    tmux launch env — they are provenance a paste cannot forge. Returns the
    pane id only when both are present; anything else is a tmux/plain seat.
    """
    e = os.environ if env is None else env
    if e.get("HERDR_ENV", "").strip() != "1":
        return None
    pane = (e.get("HERDR_PANE_ID") or "").strip()
    return pane or None


def herdr_pane_consistent(entry: Entry, env: dict[str, str] | None = None) -> bool | None:
    """Whether a herdr seat's registered pane still matches its live env.

    The %5-vector analog for herdr: a seat's HERDR_PANE_ID is trusted launch
    provenance, so a registered herdr_pane that disagrees with the live env is
    a tell (spoofed seating / stale registration). Returns None when the check
    does not apply (not a herdr entry, or no live herdr env to compare).
    """
    if entry.mux != "herdr":
        return None
    live = detect_herdr_pane(env)
    if live is None:
        return None
    return entry.herdr_pane == live


def git_common_dir(runner: Runner = _run) -> str:
    """Absolute shared .git path for cwd's repo, or '' if not in a repo.

    From inside a linked worktree `--git-common-dir` resolves to the MAIN
    repo's .git, so it is identical across every per-seat worktree of one
    logical repo — the stable key the switchboard needs once seats stop
    sharing a working dir (backlog item 1). '' when git is absent or cwd is
    not a repo; recording it is best-effort, never fatal to registration.
    """
    try:
        cp = runner(["git", "rev-parse", "--git-common-dir"])
    except (OSError, subprocess.SubprocessError):
        return ""
    out = cp.stdout.strip()
    if cp.returncode != 0 or not out:
        return ""
    p = Path(out)
    if not p.is_absolute():
        p = Path.cwd() / p
    # resolve() regardless of git's relative/absolute form so the key matches
    # worktree.repo_common_dir for the same repo even behind a symlink (Note 1).
    return str(p.resolve())


def same_repo(a: Entry, b: Entry) -> bool:
    """True if two entries are seats of the same LOGICAL repo.

    Prefer the shared .git path (worktree-aware); fall back to repo_root for
    entries written before repo_common_dir existed.
    """
    if a.repo_common_dir and b.repo_common_dir:
        return a.repo_common_dir == b.repo_common_dir
    return a.repo_root == b.repo_root


def proc_start_epoch(pid: int, runner: Runner = _run) -> float | None:
    """Wall-clock start of pid (epoch seconds, 1s resolution), or None.

    `ps -p <pid> -o lstart=` prints the start in local time. None on any
    failure; callers must treat None as "cannot prove continuity".
    """
    try:
        cp = runner(["ps", "-p", str(pid), "-o", "lstart="])
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(cp.stdout.split())
    if cp.returncode != 0 or not text:
        return None
    try:
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _inherited_session(existing: "Entry | None", pid: int, runner: Runner) -> "Entry | None":
    """The existing entry, if this register is the SAME process re-registering.

    #212: SessionStart re-fires on compaction and /clear inside one claude
    process, and the hook re-registers without --session-id; minting a fresh
    id there orphaned the advisor's armed token, which is bound to
    session_id+pid. Inherit only when the entry names this pid AND this
    process started no later than the entry's first registration. A recycled
    pid is a process started after the old owner died, so it fails the second
    test. An unreadable start time also fails it. A resume is a new pid and
    never inherits; its token dies with the old pid by design.
    """
    if existing is None or existing.pid != pid or not existing.session_id:
        return None
    started = proc_start_epoch(pid, runner=runner)
    if started is None:
        return None
    try:
        registered = calendar.timegm(time.strptime(existing.started_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    # +1s: lstart and started_at both truncate to the second.
    if started > registered + 1:
        return None
    return existing


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def registry_refusals_file() -> Path:
    return net_dir() / "registry-refusals.jsonl"


def _record_registry_refusal(
    *,
    kind: str,
    name: str,
    reason: str,
    caller_pid: int | None = None,
    owner_pid: int | None = None,
) -> None:
    """Audit-parity with wire.record_refusal (playbook gate #8).

    Wire refusals were audited; registry refusals only raised — so a
    refused-heartbeat liveness claim could not be verified against any
    artifact (found 2026-06-12). A refused registry claim that leaves no
    trace can only be proven by the refuser's say-so. JSONL,
    flock-protected, one event per line.
    """
    file = registry_refusals_file()
    line = json.dumps(
        {
            "ts": utcnow_iso(),
            "kind": kind,
            "name": name,
            "caller_pid": caller_pid,
            "owner_pid": owner_pid,
            "reason": reason,
        }
    )
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _acquire_lock(file.parent / ".registry-refusals.lock")
    try:
        with open(file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        os.chmod(file, 0o600)
    finally:
        _release_lock(lock)


def _entry_path(name: str) -> Path:
    return registry_dir() / f"{name}.json"


def _write_entry(entry: Entry) -> Path:
    path = _entry_path(entry.name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(entry), indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.rename(path)  # atomic on same fs — readers never see a torn entry
    return path


_ENTRY_FIELDS = frozenset(f.name for f in fields(Entry))


def _entry_from_data(data: dict) -> Entry:
    """Build an Entry, IGNORING unknown keys (Postel: liberal in what you read).

    A strict Entry(**data) raises TypeError on any key it doesn't declare, so a
    registry file written by NEWER code (an added field) was silently skipped by
    an older reader — which read 'no live sessions' and would have gone dark
    (this bit the live switchboard, 2026-06-16). Filtering unknown keys lets the
    schema evolve without breaking forward-compat. Missing keys still fall to
    field defaults; a missing REQUIRED key still raises TypeError (caught by
    callers as a torn entry), so the fail-loud-on-garbage behavior is preserved.

    NOTE: this protects NEW readers. Already-deployed (cached) readers stay
    strict until repinned — so any net/ field add still REQUIRES a prompt
    post-merge repin (the standing rule), the same as every net/ change.
    """
    return Entry(**{k: v for k, v in data.items() if k in _ENTRY_FIELDS})


def live_entries_all_nets(base: "Path | None" = None) -> "list[tuple[Path, Entry]]":
    """(switchboard dir, entry) for every LIVE entry on every switchboard: the
    current MAW_NET_DIR, the default, and each sw-<slug> under `base`
    (~/.claude/bridge). A torn or unreadable entry is skipped, never trusted."""
    base = base if base is not None else Path.home() / ".claude" / "bridge"
    dirs = [net_dir(), base / "switchboard"]
    if base.is_dir():
        dirs.extend(sorted(p for p in base.glob("sw-*") if p.is_dir()))
    out: list[tuple[Path, Entry]] = []
    seen: set[Path] = set()
    for d in dirs:
        rd = Path(d) / "registry"
        if rd in seen or not rd.is_dir():
            continue
        seen.add(rd)
        for f in sorted(rd.glob("*.json")):
            try:
                e = _entry_from_data(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if pid_alive(e.pid):
                out.append((Path(d), e))
    return out


def load_entry(name: str) -> Entry | None:
    path = _entry_path(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _entry_from_data(data)
    except (json.JSONDecodeError, TypeError):
        return None


def mint_session_id(name: str, now_iso: str) -> str:
    """A fresh session id: `<name>-<UTC second>-<6 hex>`.

    #338: the id used to be `<name>-<UTC second>` alone, and seat names repeat
    across repos (every pair has an `advisor`), so seats the grid launcher
    brought up in the same second shared an id. A batch manifest then refused
    (one session per line) and the per-session grant-request record collided.
    The random suffix makes the id unique on this machine without loosening
    any check that keys on it.
    """
    return f"{name}-{now_iso.replace(':', '').replace('-', '')}-{secrets.token_hex(3)}"


def register(
    *,
    name: str,
    role: str,
    repo_root: str | None = None,
    session_id: str | None = None,
    transcript_path: str | None = None,
    runner: Runner = _run,
) -> Entry:
    if not NAME_RE.match(name):
        raise NetRegistryError(
            f"invalid name {name!r}: lowercase alnum/dash/underscore, max 64"
        )
    pid, tty = find_claude_ancestor(runner=runner)
    pane = detect_tmux_pane(tty, runner=runner)
    herdr_pane = detect_herdr_pane()
    now = utcnow_iso()
    # NET-3: the load -> live-holder check -> write below is read-check-write;
    # without a per-name lock two concurrent registers both pass the check and
    # the loser's entry is silently clobbered (collision handler defeated).
    registry_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    reg_lock = _acquire_lock(registry_dir() / f".{name}.reglock")
    try:
        existing = load_entry(name)
        if existing and pid_alive(existing.pid) and existing.pid != pid:
            _record_registry_refusal(
                kind="register_name_collision",
                name=name,
                caller_pid=pid,
                owner_pid=existing.pid,
                reason=f"name held by live pid {existing.pid}",
            )
            raise NetRegistryError(
                f"name {name!r} is held by live pid {existing.pid}; "
                f"pick another name or unregister it from that session"
            )
        inherited = (
            None
            if session_id or os.environ.get("MAW_SESSION_ID")
            else _inherited_session(existing, pid, runner)
        )
        entry = Entry(
            name=name,
            role=role,
            repo_root=repo_root or os.getcwd(),
            cwd=os.getcwd(),
            pid=pid,
            tty=tty,
            tmux_pane=pane,
            session_id=session_id
            or os.environ.get("MAW_SESSION_ID")
            or (inherited.session_id if inherited else "")
            or mint_session_id(name, now),
            started_at=existing.started_at if existing and existing.pid == pid else now,
            last_seen=now,
            transport={
                "tmux_push": pane is not None,
                "herdr_push": herdr_pane is not None,
            },
            repo_common_dir=git_common_dir(runner=runner),
            mux="herdr" if herdr_pane is not None else "tmux",
            herdr_pane=herdr_pane,
            model=os.environ.get("MAW_SEAT_MODEL", "").strip(),
            # The SessionStart hook passes the Claude transcript path; a seat
            # registered without one keeps "" and net.liveness falls back to the
            # weaker dir-max basis rather than guessing a file.
            transcript_path=(transcript_path or "").strip()
            or (inherited.transcript_path if inherited else ""),
        )
        _write_entry(entry)
        # A re-registered name is live again: its old tombstone is stale and
        # must not keep vouching for a transcript the seat has since replaced.
        remove_tombstone(name)
    finally:
        _release_lock(reg_lock)
    return entry


def heartbeat(name: str, runner: Runner = _run) -> Entry:
    entry = load_entry(name)
    if entry is None:
        raise NetRegistryError(f"{name!r} is not registered")
    # Lineage-checked: an unauthenticated heartbeat would let a same-named
    # session in another repo keep refreshing — and thereby MASK — a
    # genuinely dark peer's entry, defeating net.watch's dark-session
    # detection (advisor, PR #7 review). Only the owning session refreshes.
    if not verify_caller_is(entry, runner=runner):
        _record_registry_refusal(
            kind="heartbeat_lineage",
            name=name,
            caller_pid=os.getpid(),
            owner_pid=entry.pid,
            reason="caller lineage does not reach the owning claude pid; "
            "a foreign heartbeat masks a dark peer",
        )
        raise NetRegistryError(
            f"{name!r} is owned by pid {entry.pid}, not this session; "
            f"heartbeat refused (a foreign heartbeat masks a dark peer)"
        )
    entry.last_seen = utcnow_iso()
    _write_entry(entry)
    return entry


def tombstone_dir() -> Path:
    return net_dir() / "tombstones"


def _tombstone_path(name: str) -> Path | None:
    """The tombstone path for `name`, or None if `name` is not a valid seat
    name or would resolve outside <net_dir>/tombstones (#352 hardening: the
    name may come from a registry file's JSON or a CLI string)."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    d = tombstone_dir()
    p = d / f"{name}.json"
    try:
        if p.resolve().parent != d.resolve():
            return None
    except OSError:
        return None
    return p


def _write_tombstone(entry: Entry, *, stem: str | None = None) -> bool:
    """Leave the seat's transcript pointer behind (#352): `net.deck audit --as
    <seat>` falls back to it, so a peer's counter-audit still lands after the
    seat stood down. Best-effort — a tombstone failure must never block unregister.

    `stem` is the registry file's own stem; the entry's JSON `name` must match
    it (and NAME_RE), or the write is refused and audited — a crafted entry
    file must not steer a write outside the tombstone dir. Returns True if
    a tombstone was written.
    """
    expected = entry.name if stem is None else stem
    path = _tombstone_path(entry.name) if entry.name == expected else None
    if path is None:
        try:
            _record_registry_refusal(
                kind="tombstone", name=str(expected)[:128],
                reason=f"entry name {str(entry.name)[:128]!r} is not a valid seat "
                f"name matching its registry file {str(expected)[:128]!r}; "
                "tombstone not written",
            )
        except OSError:
            pass
        return False
    try:
        d = path.parent
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = d / f"{entry.name}.json.tmp"
        tmp.write_text(json.dumps({
            "name": entry.name, "role": entry.role,
            "session_id": entry.session_id,
            "transcript_path": entry.transcript_path,
            "unregistered_at": utcnow_iso(),
        }, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.rename(path)
    except OSError:
        return False
    return True


def load_tombstone(name: str) -> dict | None:
    path = _tombstone_path(name)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def remove_tombstone(name: str) -> None:
    path = _tombstone_path(name)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def unregister(name: str) -> bool:
    path = _entry_path(name)
    if path.exists():
        entry = load_entry(name)
        if entry is not None:
            _write_tombstone(entry, stem=name)
        path.unlink()
        return True
    return False


def who(prune: bool = True) -> list[Entry]:
    """All registered sessions, pruning entries whose pid is dead.

    Pruning at read time is what makes the registry crash-safe: a session
    that died without unregistering is removed by the next reader.
    """
    rdir = registry_dir()
    if not rdir.exists():
        return []
    out: list[Entry] = []
    for path in sorted(rdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = _entry_from_data(data)
        except (json.JSONDecodeError, TypeError):
            continue  # torn/foreign file; leave for manual inspection
        if not pid_alive(entry.pid):
            if prune:
                _write_tombstone(entry, stem=path.stem)
                path.unlink(missing_ok=True)
            continue
        out.append(entry)
    return out


def verify_caller_is(entry: Entry, runner: Runner = _run) -> bool:
    """True if the calling process belongs to the session that registered.

    `--as <name>` would otherwise be a free string: any local process
    could claim any identity on the wake line. Walking the caller's own
    process tree to the owning claude pid and comparing it to the
    registered pid pins the claim to the session that actually made it.
    (Same-box root processes can still forge; this is session-level, not
    adversary-level, authentication.)
    """
    pid, _tty = find_claude_ancestor(runner=runner)
    return pid == entry.pid


def caller_lineage(entry: Entry, runner: Runner = _run,
                   start_pid: int | None = None) -> str:
    """How the calling process relates to the session that registered:
    'match' (its nearest claude ancestor is the registered pid), 'mismatch'
    (it has a claude ancestor and it is a different one), or 'unknown' (the
    walk could not finish: a `ps` call failed, or it reached pid 1 first, as a
    watcher's shell does once it has been re-parented).

    `verify_caller_is` folds 'unknown' into False, which is right for anything
    that consumes or sends. A read-only probe can tell the two apart: a walk
    that did not finish is not evidence of another session."""
    pid = start_pid if start_pid is not None else os.getpid()
    for _ in range(20):
        row = _proc_row(pid, runner)
        if row is None:
            return "unknown"
        ppid, _tty, command = row
        exe = command.split()[0] if command.split() else ""
        if "claude" in os.path.basename(exe) or " claude" in command[:80]:
            return "match" if pid == entry.pid else "mismatch"
        if ppid <= 1:
            return "unknown"
        pid = ppid
    return "unknown"


def is_stale(entry: Entry, now_s: float | None = None) -> bool:
    # timegm, not mktime: last_seen is UTC; mktime would interpret the
    # struct as local with a DST guess, skewing the age by an hour in
    # DST zones (latent bug found while building net.watch, 2026-06-11).
    try:
        seen = calendar.timegm(time.strptime(entry.last_seen, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return True
    now = now_s if now_s is not None else time.time()
    return (now - seen) > STALE_AFTER_S
