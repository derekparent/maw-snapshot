"""net.watch — the dark-session / parked-wire alarm.

2026-06-10 exposed the gap three times in one evening: the registry
flagged the advisor stale at 16:59 and nobody noticed for five hours; a
parked human draft silently swallowed four pushes; a Remote Control
overlay occupied a pane while its session reasoned from a stale world.
In every case the system *recorded* the failure and told no one — a
heartbeat nobody watches is a log, not a monitor.

This module is the "says so out loud" leg. Three detections, all pure
reads over state the switchboard already keeps:

  dark sessions — a registered session (pid alive, so not prunable)
      whose last_seen is older than the threshold. It looks reachable
      on `net who`; peers push to it; nothing comes back.

  parked wire turns — unconsumed wire turns older than the threshold.
      The v1.1 precheck deliberately parks instead of clobbering a
      human draft; parking is safe ONLY if someone eventually notices.

  refused heartbeats — a stream of heartbeat_lineage refusals in
      registry-refusals.jsonl: a live session is claiming a name it
      does not own, so its OWN liveness never lands and its real entry
      reads dark while it actively builds (the 2026-06-12 mis-seated
      executor case). Thresholded — the refusal fires on every prompt
      of a misconfigured session, so one record is noise and a stream
      is the alarm. register_name_collision is deliberately NOT
      surfaced: benign, the self-qualifier handles it.

Designed for cron/launchd/loop: each invocation is one sweep — prints
findings and exits 1 when there are any (0 when all clear). Notify is
pluggable. The operator's decided channel (2026-06-11) is PushNotification, which
is a harness tool — so that leg is a session-owned /loop ("run the
sweep; findings → PushNotification") and the watcher stays a plain
detector. The included iMessage sender (osascript, buddy-AppleScript
verified on macOS 26) is the no-session daemon fallback, armed only via
MAW_WATCH_IMESSAGE_TO.
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bridge.livemodel import (
    live_model_for_cwd,
    live_models_for_cwd,
    tail_last_main_models,
)
from net.registry import (
    Entry,
    net_dir,
    pid_alive,
    registry_dir,
    registry_refusals_file,
    unregister,
    who,
)
from net.transport import Runner, _run, herdr_agent_status, pane_exists
from net.wire import deliveries, unconsumed
from net import bridgelink, waitstate

DEFAULT_DARK_MIN = 15
DEFAULT_PARKED_MIN = 10
# Both seats of a pair sitting on an unpulled inbox this long reads as the
# wait-state deadlock (#149): neither is draining the other. Below the dark
# threshold on purpose — the incident sat BELOW every existing alarm.
DEFAULT_DEADLOCK_MIN = 12
# A live seat that CLAIMS herdr status=working but has made no progress (stale
# heartbeat AND no recent commit) this long is stuck-while-working (#149/F10 —
# the 2026-07-21 8h stall). The knob's default is the incident-derived floor;
# alarm-fatigue tuning is a stated non-goal (the drill proves signal; tuning
# follows usage). Fires a WAKE, not a page — the cost of a false fire is one
# cheap pane/worktree read by the supervisor, which is the correct doctrine.
DEFAULT_STUCK_WORKING_MIN = 15
DEFAULT_REFUSAL_WINDOW_MIN = 30
DEFAULT_REFUSAL_MIN_COUNT = 3
# An input box that has held an unsent draft across repeated wakes for this long
# is almost certainly orphaned (a typed-but-never-submitted /rc prompt), not a
# human mid-keystroke. Order-of-magnitude the dark threshold; the >=2-park
# "repeated" guard is what actually separates a stuck draft from live typing.
DEFAULT_ORPHANED_DRAFT_MIN = 15
# Coarse genuinely-dark detection (item 9), OPT-IN: 0 disables it. The naive
# per-cycle dark timer (DEFAULT_DARK_MIN) is net-harmful as a pager — it flags a
# 17-minute legitimate refactor and trains alarm fatigue (the rev-1 lesson, the
# advisor declined to arm it). The only viable form is COARSE (hours) AND
# git-progress-aware: silent for hours WITH no repo commits in the window =
# likely abandoned. It is explicitly NOT a fine-grained liveness pager (it can't
# see a seat hung mid-build with no commits — that's the acknowledged hole); it
# catches the ~2-day-dead-seat case.
DEFAULT_GENUINELY_DARK_HOURS = 0

# Roles for which commit cadence is NOT a liveness signal (MAW-PULSE-COS).
# genuinely-dark reads "silent AND not committing" as "abandoned". That
# inference holds for a BUILDER — a builder that is working commits. It is
# simply false for a WATCHER: the CoS seat spends hours in advisory
# conversation with the operator without committing, and its loop sleeps up to 3h
# between wakes BY DESIGN, so it trips the heuristic on every quiet interval
# (it false-paged the operator every 15 min from 2026-07-13). The heuristic is wrong for
# the ROLE, not badly tuned: raising the threshold still false-pages on a
# genuinely quiet 4h sitting, just less often.
#
# This is not a liveness hole. genuinely-dark only ever fires on a pid-ALIVE
# seat — who(prune=True) unregisters a dead pid before we see it — so a watcher
# whose process is gone was never caught by this path for ANY role. Exempting
# the role forfeits nothing that was being detected.
#
# Exact-match on the registry role, and ONLY the role: an absent, unknown, or
# misspelled role is not a watcher, so it keeps the full heuristic and fails
# toward paging. Naming a seat "cos" does not make it one.
WATCHER_ROLES = ("cos",)


@dataclass(frozen=True)
class Finding:
    # "dark-session" | "parked-wire" | "refused-heartbeat" | "genuinely-dark"
    # | "orphaned-draft" (input box stuck with an unsent draft across wakes — #110)
    # | "blocked-seat" (herdr says blocked while silent — A3)
    # | "model-flip" (live model != launcher-pinned model — B3)
    # | "deadlock-both-parked" (both seats of a pair sitting on an unpulled
    #   durable inbox — the wait-state deadlock, #149)
    # | "stuck-while-working" (a live seat claims herdr status=working but shows
    #   no progress — stale heartbeat AND no recent commit — the 2026-07-21 8h
    #   stall class, #149/F10; the hole genuinely-dark explicitly leaves open)
    # | "watcher-quiet" (a WATCHER_ROLES seat exempted from genuinely-dark —
    #   a record of the exemption, never a page)
    # | "waiting-on-counterpart" (a quiet seat whose next move is its live
    #   peer's or the operator's, by an artifact — T2; a record, never a wake)
    # | "self-quiet" (the sweeping seat's OWN dark-session while a live peer
    #   watches it — #345; a record, never a wake; net.deadman check pages on
    #   the raw dark-session)
    kind: str
    subject: str
    age_min: int
    detail: str
    # The subject could not be read on this sweep: its pane status was
    # unreadable or `unknown`, or its last_seen did not parse. A finding that
    # needs that read (blocked-seat) is then missing because nobody could
    # look, not because it cleared: net.deadman holds an until-clear ack
    # across such a tick, and does not take the absence as the end of a stall.
    unread: bool = False

    def line(self) -> str:
        return f"[{self.kind}] {self.subject}: {self.detail} ({self.age_min} min)"


# Records, not alarms: rendered in the full sweep (so the suppression is
# legible) but left out of --ids-only, the exit code and the watchdog's wake set.
# A suppression that wakes the supervisor is not a suppression.
QUIET_KINDS = frozenset({"waiting-on-counterpart", "self-quiet"})


def _age_min(ts: str, now_s: float) -> int | None:
    # timegm, NOT mktime: the timestamps are UTC, and mktime(strptime(...))
    # interprets the struct as local with a DST *guess* (isdst=-1) — in a
    # DST zone that silently skews every age by an hour.
    try:
        seen = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
    return max(0, int((now_s - seen) / 60))


def _now_s() -> float:
    return time.time()


def dark_sessions(
    threshold_min: int = DEFAULT_DARK_MIN,
    now_s: float | None = None,
    runner: Runner = _run,
) -> list[Finding]:
    """Registered, pid-alive sessions silent past the threshold.

    who(prune=True) already removes dead pids, so anything returned here
    is a session that *looks* reachable to peers — the dangerous kind of
    dark. An unparseable last_seen counts as dark (fail-loud).
    """
    now = now_s if now_s is not None else _now_s()
    findings: list[Finding] = []
    entries = who(prune=True)
    for entry in entries:
        status: str | None = None
        age = _age_min(entry.last_seen, now)
        if age is None:
            findings.append(
                Finding(
                    "dark-session",
                    entry.name,
                    0,
                    f"last_seen unparseable ({entry.last_seen!r})",
                    unread=True,
                )
            )
            continue
        if age <= threshold_min:
            continue
        # Herdr liveness fusion (A3): for a herdr seat, the live agent status
        # REFINES this silence-triggered finding — it never triggers one on
        # its own (herdr detection is heuristic, #765). 'working' suppresses
        # (heads-down, not dark); 'blocked' reclassifies to a waiting-prompt
        # finding (the prompt may be the operator's, so a peer pages the operator and never
        # answers it: the 2026-09-22 row-12 lesson); anything else (idle/done/unknown/
        # unreadable/herdr-absent) falls through to the normal dark finding —
        # i.e. herdr-absent behavior is byte-for-byte the pre-A3 behavior.
        if entry.mux == "herdr" and entry.herdr_pane:
            status = herdr_agent_status(entry.herdr_pane, runner)
            if status == "working":
                continue
            if status == "blocked":
                findings.append(
                    Finding(
                        "blocked-seat",
                        entry.name,
                        age,
                        f"herdr reports BLOCKED and silent {age} min — a prompt "
                        f"is waiting in herdr pane {entry.herdr_pane}. It may be "
                        f"the operator's own (an arm consent or a question): only the operator answers "
                        f"it, in that pane. Another seat never answers it or presses "
                        f"an option; read the pane and page the operator",
                    )
                )
                continue
            pane_note = f"herdr pane {entry.herdr_pane} status {status or 'unreadable'}"
            reachable = status in ("idle", "done")
        elif entry.tmux_pane and pane_exists(entry.tmux_pane, runner):
            pane_note = f"pane {entry.tmux_pane} still exists"
            reachable = True
        elif entry.tmux_pane:
            pane_note = f"pane {entry.tmux_pane} is GONE"
            reachable = False
        else:
            pane_note = "push-deaf (no pane)"
            reachable = False
        # T2 (Lane D P2): idle by design is not dark. A seat whose next move is
        # its live peer's or the operator's, by an ARTIFACT it left (the pair
        # wire's turn order, an open net.question), is reclassified to a quiet
        # record. The counterpart holds the ball; if it goes quiet, ITS finding
        # fires. No artifact, or an unreadable one, falls through to dark, and
        # so does a seat with no reachable pane: it cannot receive the reply.
        wait = waitstate.wait_reason(entry, entries, now) if reachable else None
        if wait:
            findings.append(
                Finding(
                    "waiting-on-counterpart",
                    entry.name,
                    age,
                    f"quiet {age} min, {wait}; {pane_note}. Not dark: the "
                    f"counterpart holds the next move (never wakes or pages)",
                )
            )
            continue
        findings.append(
            Finding(
                "dark-session",
                entry.name,
                age,
                f"registered + pid alive but silent; {pane_note}",
                # `unknown` is herdr saying it could not classify the pane:
                # no more a look at the seat than no answer at all (#379).
                unread=bool(entry.mux == "herdr" and entry.herdr_pane
                            and status in (None, "unknown")),
            )
        )
    return findings


def parked_wires(
    threshold_min: int = DEFAULT_PARKED_MIN,
    now_s: float | None = None,
) -> list[Finding]:
    """Unconsumed wire turns older than the threshold, grouped per recipient."""
    now = now_s if now_s is not None else _now_s()
    base = net_dir() / "wire"
    if not base.is_dir():
        return []
    findings: list[Finding] = []
    for recipient_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        turns = unconsumed(recipient_dir.name)
        old = []
        for _file, turn in turns:
            age = _age_min(turn.ts, now)
            if age is None or age > threshold_min:
                old.append((turn, age))
        if not old:
            continue
        oldest = max((age or 0) for _t, age in old)
        senders = sorted({t.role for t, _a in old})
        findings.append(
            Finding(
                "parked-wire",
                recipient_dir.name,
                oldest,
                f"{len(old)} unconsumed turn(s) from {', '.join(senders)} — "
                f"peer should run recv",
            )
        )
    return findings


def deadlock_pairs(
    threshold_min: int = DEFAULT_DEADLOCK_MIN,
    now_s: float | None = None,
) -> list[Finding]:
    """The wait-state deadlock (#149): BOTH seats of a pair are sitting on an
    unpulled durable inbox older than the threshold — each waiting for the other
    to act while a turn already waits on its own wire.

    This reads the DURABLE bridge (not just the wire): the motivating incident
    had a successfully-pushed merge echo that was never PULLED — a consumed wire
    turn but an unconsumed bridge turn — which a wire-only check misses. Pairs
    live seats by shared bridge slug; a pair is flagged only when the executor
    has an unconsumed inbound from the advisor AND the advisor has one from the
    executor. A one-sided unpulled inbox is a normal parked-wire finding, not
    this — the mutual wait is the deadlock tell the operator named.
    """
    now = now_s if now_s is not None else _now_s()
    by_slug: dict[str, dict[str, Entry]] = {}
    for e in who(prune=False):
        if e.role not in bridgelink.BRIDGE_ROLES:
            continue
        # Only LIVE seats can deadlock — a dead-pid seat with an unpulled inbox is
        # a dark/reaped case (its inbox will never be pulled), not a mutual wait.
        if not pid_alive(e.pid):
            continue
        try:
            slug = bridgelink._shared_slug(e)
        except Exception:  # noqa: BLE001 — a bad entry must not sink the sweep
            continue
        by_slug.setdefault(slug, {})[e.role] = e

    findings: list[Finding] = []
    for slug, roles in sorted(by_slug.items()):
        adv, exe = roles.get("advisor"), roles.get("executor")
        if adv is None or exe is None:
            continue
        exe_in = bridgelink.next_inbound(exe)   # unconsumed advisor -> executor
        adv_in = bridgelink.next_inbound(adv)   # unconsumed executor -> advisor
        if exe_in is None or adv_in is None:
            continue
        exe_age = _age_min(exe_in[1].ts, now)
        adv_age = _age_min(adv_in[1].ts, now)
        # An unparseable ts counts as old (fail-loud, matching dark_sessions).
        exe_old = exe_age is None or exe_age > threshold_min
        adv_old = adv_age is None or adv_age > threshold_min
        if not (exe_old and adv_old):
            continue
        ages = [a for a in (exe_age, adv_age) if a is not None]
        oldest = max(ages) if ages else threshold_min + 1
        findings.append(
            Finding(
                "deadlock-both-parked",
                slug,
                oldest,
                f"BOTH seats have an unpulled inbox — executor has turn "
                f"{exe_in[1].turn_id} from advisor, advisor has turn "
                f"{adv_in[1].turn_id} from executor; likely a wait-state deadlock, "
                f"each should run recv (#149)",
            )
        )
    return findings


# A parked delivery whose push header carries one of these markers means the
# wake line refused to type because the target's input box held an UNSENT DRAFT.
# Two wordings because two transports write it: herdr's precheck says "box
# non-empty"; the tmux dispatcher says "input box is not empty". Other parks
# (peer busy mid-turn, pane gone) are NOT draft signals and must not match.
_DRAFT_PARK_MARKERS = ("box non-empty", "input box is not empty")


def _push_header(body: str) -> str:
    """The `push: ...` line from a wire turn's audit body ('' if absent)."""
    for line in body.splitlines():
        if line.startswith("push:"):
            return line
    return ""


def _is_draft_park(body: str) -> bool:
    header = _push_header(body)
    return header.startswith("push: none") and any(
        m in header for m in _DRAFT_PARK_MARKERS
    )


def _is_success_push(body: str) -> bool:
    """True when this delivery actually landed (box was empty, text submitted).

    A successful push proves the box was empty at that instant — so any earlier
    parked draft was gone by then. It is the signal that RESETS the stuck-draft
    streak.
    """
    return _push_header(body).startswith("push: pasted+enter")


def orphaned_drafts(
    threshold_min: int = DEFAULT_ORPHANED_DRAFT_MIN,
    now_s: float | None = None,
) -> list[Finding]:
    """Recipients whose input box is stuck holding an unsent draft.

    The operator lost five rulings in three days to /rc prompts typed but never submitted;
    an orphaned draft also parks every subsequent wake to that pane. maw already
    KNOWS — its wake-line precheck refuses to type over a non-empty box and
    records that refusal in the wire ledger. This reads maw's OWN signal: per
    recipient, the delivery history after the last successful push (the "open
    region" — nothing has landed since). If that region holds >= 2 box-non-empty
    parks, the most recent delivery is still such a park (i.e. still stuck), and
    the oldest park is >= threshold old, the box is surfaced.

    SURFACE ONLY. This never submits the draft: pressing Enter would forge the operator's
    Enter (trust ladder, non-negotiable). Pure over the wire ledger — a dead
    session's stuck draft is separately a dark-session finding.

    Known, deliberate false-positive bound: a draft submitted by hand (not via a
    maw push) leaves the parks in the ledger untouched, so it RE-SURFACES on
    every sweep until the next successful push to that recipient clears the open
    region (minutes in an active pair, potentially hours for a quiet pane).
    Over-surfacing is safe — the sweeper only ever reports; a human confirms.
    """
    now = now_s if now_s is not None else _now_s()
    base = net_dir() / "wire"
    if not base.is_dir():
        return []
    findings: list[Finding] = []
    for recipient_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        turns = deliveries(recipient_dir.name)
        if not turns or not _is_draft_park(turns[-1].body):
            continue
        # Open region: everything after the last delivery that actually landed.
        start = 0
        for i, turn in enumerate(turns):
            if _is_success_push(turn.body):
                start = i + 1
        parks = [t for t in turns[start:] if _is_draft_park(t.body)]
        if len(parks) < 2:
            continue
        oldest_age = _age_min(parks[0].ts, now)
        if oldest_age is None or oldest_age < threshold_min:
            continue
        senders = sorted({t.role for t in parks})
        findings.append(
            Finding(
                "orphaned-draft",
                recipient_dir.name,
                oldest_age,
                f"{len(parks)} box-non-empty park(s) from {', '.join(senders)}; "
                f"input box has held an unsent draft for {oldest_age} min — likely "
                f"an orphaned /rc submission. SURFACE ONLY: check the pane and "
                f"submit or clear it by hand; the sweeper will not press Enter.",
            )
        )
    return findings


def refused_heartbeats(
    window_min: int = DEFAULT_REFUSAL_WINDOW_MIN,
    min_count: int = DEFAULT_REFUSAL_MIN_COUNT,
    now_s: float | None = None,
) -> list[Finding]:
    """Names with >= min_count heartbeat_lineage refusals inside the window.

    The lineage refusal fires on every UserPromptSubmit of a mis-seated
    session, so the threshold separates a stray record from the alarm
    condition: a session that is alive and working while its registry
    entry reads dark. Other refusal kinds (register_name_collision) are
    excluded — benign, and alarming every launch is noise. Malformed
    lines are skipped: this is a detector over an append-only artifact
    the registry owns, not a validator of it.
    """
    now = now_s if now_s is not None else _now_s()
    file = registry_refusals_file()
    if not file.is_file():
        return []
    recent_ages: dict[str, list[int]] = {}
    for line in file.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") != "heartbeat_lineage":
            continue
        age = _age_min(str(rec.get("ts", "")), now)
        if age is None or age > window_min:
            continue
        recent_ages.setdefault(str(rec.get("name")), []).append(age)
    findings: list[Finding] = []
    for name, ages in sorted(recent_ages.items()):
        if len(ages) < min_count:
            continue
        findings.append(
            Finding(
                "refused-heartbeat",
                name,
                min(ages),
                f"{len(ages)} lineage-refused heartbeat(s) in {window_min} min "
                f"— a live session claims this name but is not the owner; its "
                f"own liveness never lands (env/seat mismatch — relaunch or "
                f"reseat; see registry-refusals.jsonl)",
            )
        )
    return findings


def reap_dead() -> list[str]:
    """Explicitly remove registry entries whose pid is gone; return their names.

    who() already prunes dead pids as a side effect of dark_sessions, but making
    it a FIRST-CLASS, reported step means reaping no longer depends on a manual
    `who` (the gap item #8 names) and is VISIBLE — its one real use this arc was
    reaping a multi-day zombie. This is CONFIRMED-DEAD (pid gone), deliberately
    distinct from a dark session (pid alive but silent — item #9): a reaped seat
    is gone, a dark seat needs attention.

    Reads the registry dir DIRECTLY rather than via who(): who() filters dead
    entries out of its result (it only unlinks them when prune=True), so it can't
    tell us WHICH names were reaped. Torn/foreign files are skipped, not reaped.
    """
    rdir = registry_dir()
    if not rdir.exists():
        return []
    reaped: list[str] = []
    for path in sorted(rdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid, name = int(data["pid"]), str(data["name"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
            continue  # torn/foreign file — leave for manual inspection
        if not pid_alive(pid):
            unregister(name)
            reaped.append(name)
    return reaped


def repo_has_recent_commits(repo_root: str, since_hours: int, runner: Runner = _run) -> bool:
    """True if `repo_root` has any commit within the last `since_hours`.

    The progress signal that suppresses a coarse dark alarm during legitimate
    long work. Repo-wide (not per-seat) — by design: the coarse detector targets
    'the whole thing went quiet', not 'this seat hung'. A git error → False (we
    couldn't confirm progress; an hours-silent seat we also can't vouch for is
    left to fail toward flagging).

    `--branches` is load-bearing once seats run in PER-SEAT WORKTREES (backlog
    item 1): each seat's `repo_root` is its own linked worktree whose checked-out
    HEAD is its own `seat/<role>` branch. A bare `git log -1` would see ONLY
    that branch — so an idle advisor whose peer executor is committing actively
    on `seat/executor` would resolve 'no recent commits' and be FALSE-flagged
    genuinely-dark, even though the logical repo is plainly progressing.
    `--branches` walks every LOCAL branch through the shared object store (a
    linked worktree's `git` reaches all branches of the common repo), making the
    signal genuinely repo-wide across seats. It deliberately excludes
    remote-tracking refs and tags (which `--all` would include) — a fetched but
    locally-idle/abandoned seat must NOT be kept 'alive' by remote commit dates;
    the progress signal is about LOCAL work across the pair's seats. No behavior
    change in the single-branch shared-tree world (one branch = whole repo);
    correct in the per-seat-worktree world.
    """
    try:
        cp = runner(
            ["git", "-C", repo_root, "log", "-1", "--branches",
             "--since", f"{since_hours} hours ago", "--format=%H"]
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0 and bool(cp.stdout.strip())


def genuinely_dark_sessions(
    threshold_hours: int = DEFAULT_GENUINELY_DARK_HOURS,
    now_s: float | None = None,
    runner: Runner = _run,
) -> list[Finding]:
    """Coarse, git-progress-aware darkness (item 9). 0 hours = disabled.

    A registered + pid-alive session silent for >= threshold_hours AND whose repo
    has no commits in that window — likely abandoned, not mid-task. Suppressed by
    recent commits (progress = not dark). NOT a fine-grained pager: a seat hung
    mid-build with no commits is the acknowledged hole.

    A WATCHER_ROLES seat is reclassified to a non-paging watcher-quiet finding:
    commit cadence is not a liveness signal for a role that is quiet by design.
    """
    if threshold_hours <= 0:
        return []
    now = now_s if now_s is not None else _now_s()
    threshold_min = threshold_hours * 60
    findings: list[Finding] = []
    for entry in who(prune=True):
        age = _age_min(entry.last_seen, now)
        if age is None or age < threshold_min:
            continue
        if repo_has_recent_commits(entry.repo_root, threshold_hours, runner):
            continue  # progress in the repo → not genuinely dark (coarse)
        if entry.role in WATCHER_ROLES:
            # Reclassify rather than drop: a suppression that leaves no trace
            # cannot be proven to have worked. watcher-quiet is surfaced by
            # render() but is absent from pulse.DARK_KINDS, so it records the
            # exemption without paging. (Same shape as the blocked-seat
            # reclassification in dark_sessions.)
            findings.append(
                Finding(
                    "watcher-quiet",
                    entry.name,
                    age,
                    f"silent {age // 60}h with no commits in {entry.repo_root}, but "
                    f"role={entry.role!r} is a watcher role — quiet by design between "
                    f"wakes, so commit cadence cannot mean abandoned. Not paging "
                    f"(process is alive; a dead one would be unregistered)",
                )
            )
            continue
        findings.append(
            Finding(
                "genuinely-dark",
                entry.name,
                age,
                f"silent {age // 60}h + no commits in {entry.repo_root} over "
                f"{threshold_hours}h — likely abandoned (coarse; not a mid-build "
                f"hang detector)",
            )
        )
    return findings


# MODEL-FLIP NOISE CONTROL (#188). Three live runs of false positives, zero
# pages (model-flip is not in ESCALATABLE_KINDS and must never be) — but each
# fire costs a wake plus a pane read, which is what teaches a seat to ignore the
# watch output.
#
# PERSISTENCE: one record is not a state. A single transient fallback turn stays
# "the last" until a fresh turn overwrites it, so the flip fired on a seat whose
# pane read 4.8. A flip now needs the most recent MODEL_FLIP_CONSECUTIVE records
# to AGREE on the fallback.
#
# COOLDOWN: a condition that CYCLES re-fires as a fresh edge every time the id
# set goes empty->present, and a DELIBERATE mismatch (the operator running opus-5 against
# an opus-4-8 pin) can never clear at all — that one re-fired all night. A fired
# id is quiet for MODEL_FLIP_COOLDOWN_MIN; it is a notice, so a delayed repeat
# costs nothing.
MODEL_FLIP_CONSECUTIVE = 3
DEFAULT_MODEL_FLIP_COOLDOWN_MIN = 120
MODEL_FLIP_TAIL = 5
FLAP_LEDGER = "watch-flap-cooldown.jsonl"


def live_seats(prune: bool = True) -> list[Entry]:
    """Registered sessions with DUPLICATE registrations of one session collapsed.

    The name-collision handler re-registers a seat under a qualified name
    (`executor` -> `executor-<slug>`) and the original file stays behind, so one
    live session appears twice and DOUBLES every finding about it (skills
    2026-07-25). Two entries that share a pid AND a pane are one session; the
    freshest `last_seen` is the registration the seat is actually heartbeating.

    Delivery still reads `who()` — a peer sending to the stale name must still
    resolve it. This is the SWEEP's view, where a double is pure noise.
    """
    best: dict[tuple, Entry] = {}
    for entry in who(prune=prune):
        key = (entry.pid, entry.mux, entry.herdr_pane or entry.tmux_pane or "")
        current = best.get(key)
        if current is None or (_seen_epoch(entry) or 0) > (_seen_epoch(current) or 0):
            best[key] = entry
    return sorted(best.values(), key=lambda e: e.name)


def _seen_epoch(entry: Entry) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(entry.last_seen, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def _flap_fired_recently(fid: str, now_s: float, window_min: int) -> bool:
    """True if `fid` fired within the cooldown window. An unreadable or torn
    ledger reads as NOT recent — a notice that fires twice is better than a
    notice a corrupt file silences."""
    path = net_dir() / FLAP_LEDGER
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("finding_id") != fid:
            continue
        epoch = rec.get("epoch")
        if isinstance(epoch, (int, float)) and epoch >= now_s - window_min * 60:
            return True
    return False


def _flap_stamp(fid: str, now_s: float) -> None:
    path = net_dir() / FLAP_LEDGER
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"finding_id": fid, "epoch": now_s}) + "\n")
    except OSError:
        pass  # a sweep must never fail on its own noise-control bookkeeping


def live_models(entry: Entry, projects_root: Path | None = None) -> list[str]:
    """The seat's last few main-loop models, newest first.

    Reads the transcript the registry PINNED for this seat when it has one; a
    project dir also holds headless children and rotated seats, so the newest
    file in it is not necessarily this seat's (#210).
    """
    pinned = (entry.transcript_path or "").strip()
    if pinned:
        return tail_last_main_models(Path(pinned), MODEL_FLIP_TAIL)
    return live_models_for_cwd(entry.cwd, MODEL_FLIP_TAIL, projects_root)


def live_model(entry: Entry, projects_root: Path | None = None) -> str | None:
    """The model actually running a seat (its freshest transcript's last
    MAIN-LOOP assistant model).

    Delegates to the shared bridge.livemodel reader so B2 (turn stamping) and
    B3 (this) agree on one definition — including the isSidechain skip (a
    subagent's model is not the seat's) and fail-open on every miss.
    """
    return live_model_for_cwd(entry.cwd, projects_root)


def _is_flip(pinned: str, live: str) -> bool:
    """A flip = the live model is not the pinned family.

    Bidirectional substring so a version/suffix decoration ('claude-opus-4-8'
    vs 'claude-opus-4-8[1m]') is NOT a flip, while a genuine family change
    ('claude-fable-5' vs 'claude-opus-4-8') is.
    """
    p, m = pinned.lower(), live.lower()
    return p not in m and m not in p


def model_flip_sessions(
    now_s: float | None = None,
    live_models_fn=live_models,
    *,
    cooldown_min: int = DEFAULT_MODEL_FLIP_COOLDOWN_MIN,
) -> list[Finding]:
    """Seats whose LIVE model has PERSISTENTLY diverged from the pinned one (B3).

    A surfaced finding (fallbackModel fired / the Fable window closed), never a
    page. Requires the most recent MODEL_FLIP_CONSECUTIVE main-loop records to
    agree on the fallback — one stale record is a transient, not a state — and
    stays quiet for `cooldown_min` after firing, so a cycling or deliberate
    mismatch costs one wake instead of one per sweep (#188). Too few records is
    fail-open (no finding). The detail names BOTH values.
    """
    now = now_s if now_s is not None else _now_s()
    findings: list[Finding] = []
    for entry in live_seats(prune=True):
        if not entry.model:
            continue
        # Filter empties BEFORE the length gate. Counting them toward the gate
        # while skipping them in the agreement check let ["opus-4-7", "", ""]
        # fire on ONE real record — the single-record false positive this gate
        # exists to remove (#213 review). MODEL_FLIP_TAIL reads more than
        # MODEL_FLIP_CONSECUTIVE precisely to leave slack for blanks.
        recent = [m for m in (live_models_fn(entry) or []) if m][:MODEL_FLIP_CONSECUTIVE]
        if len(recent) < MODEL_FLIP_CONSECUTIVE:
            continue
        if not all(_is_flip(entry.model, m) for m in recent):
            continue
        live = recent[0]
        fid = f"model-flip:{entry.name}"
        if _flap_fired_recently(fid, now, cooldown_min):
            continue
        _flap_stamp(fid, now)
        findings.append(
            Finding(
                "model-flip",
                entry.name,
                0,
                f"pinned {entry.model}, live {live} — the launcher asked for "
                f"{entry.model} but the seat's last {MODEL_FLIP_CONSECUTIVE} turns "
                f"ran {live} (fallback fired or the model window closed). Quiet for "
                f"{cooldown_min} min after this notice.",
            )
        )
    return findings


def _committed_since(repo_root: str, since_iso: str, runner: Runner = _run) -> bool:
    """True if `repo_root` has any commit across LOCAL branches since the ABSOLUTE
    timestamp `since_iso`. The positive work artifact that DISPROVES stuck-while-
    working. The cutoff is an absolute timestamp derived from the finding's
    injected clock — NOT git's wall-clock `--since "N minutes ago"`, which would
    evaluate the window against real-now even when the finding runs on a
    fast-forwarded now_s (the #181 review catch; the mocked-runner tests could
    not see it). `--branches` for the per-seat-worktree reason (a seat's git
    reaches every local branch of the shared object store, so a committing peer
    counts as pair progress). A git error → False (couldn't confirm progress; a
    claims-working seat we can't vouch for fails toward flagging, matching
    repo_has_recent_commits)."""
    try:
        cp = runner(
            ["git", "-C", repo_root, "log", "-1", "--branches",
             f"--since={since_iso}", "--format=%H"]
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0 and bool(cp.stdout.strip())


def _progressed_without_commit(entry: Entry, now: float, threshold_min: int) -> bool:
    """Progress artifacts for work that never commits (T2, Lane D P1). A commit
    is the executor's progress artifact; the ADVISOR's work (reviewing,
    verdicts, merges, paging) never commits, so 'no commit' fired
    stuck-while-working on every advisor turn longer than the threshold.

    - Either seat: a durable bridge turn it SENT within the window (speaking on
      the pair wire is visible work, and it is an artifact, not a self-report).
    - Advisor only: its OWN pinned transcript (basis 'session-transcript', never
      the dir-max fallback a sibling session can satisfy) written within the
      window. A seat hung on a model or tool call writes nothing, so that is
      still caught. The executor keeps the commit rule: the 2026-07-21 stall was
      an executor, and its liveness-without-commits is what this kind exists for.
    Any failed read is no evidence (the finding stands)."""
    cutoff = now - threshold_min * 60
    try:
        for t in bridgelink.sent_durable(entry):
            sent = waitstate._epoch(t.ts)
            if sent is not None and sent >= cutoff:
                return True
    except Exception:  # noqa: BLE001 — unreadable bridge: no evidence
        pass
    if entry.role != "advisor":
        return False
    try:
        from net.liveness import evidence_for
        ev = evidence_for(entry.name, now_s=now, entries=[entry])
    except Exception:  # noqa: BLE001
        return False
    return (ev.basis == "session-transcript" and ev.age_min is not None
            and ev.age_min <= threshold_min)


def stuck_while_working(
    threshold_min: int = DEFAULT_STUCK_WORKING_MIN,
    now_s: float | None = None,
    runner: Runner = _run,
) -> list[Finding]:
    """A live seat that CLAIMS to be working but shows no positive work artifact
    past the threshold — the 2026-07-21 stall class (#149/F10). This is
    the 'seat hung mid-build with no commits' hole genuinely_dark_sessions
    explicitly leaves open: there, herdr status=working SUPPRESSES the dark
    finding (A3), so the very signal that says 'an agent is present' hides that
    it stopped progressing.

    Fires when a pid-alive, herdr-mux seat has agent_status=working AND its
    heartbeat (last_seen) is stale past `threshold_min` AND no commit landed in
    its worktree within the window. Any ONE positive artifact clears it: a fresh
    heartbeat (the hook is firing), a recent commit (visible work), a bridge turn
    the seat sent, or for an advisor its own fresh transcript (see
    _progressed_without_commit: the advisor never commits). herdr-mux
    only — agent_status is the 'claims working' signal, and a tmux seat silent
    this long is already a dark-session. Scoped per pair (the finding names the
    stuck seat and its bridge slug) so the supervising peer is the one woken.

    Deliberately errs toward flagging: a false fire costs the supervisor one
    cheap pane/worktree read (the artifact-grounded-supervision doctrine), never
    a page — the dead-man escalation to the operator (lane 3) is separately gated on an
    UNACKED finding, not on this firing.
    """
    now = now_s if now_s is not None else _now_s()
    findings: list[Finding] = []
    for entry in who(prune=False):
        if entry.role not in bridgelink.BRIDGE_ROLES:
            continue
        if not pid_alive(entry.pid):
            continue
        if entry.mux != "herdr" or not entry.herdr_pane:
            continue
        age = _age_min(entry.last_seen, now)
        # A fresh heartbeat is progress; only a STALE (or unparseable, fail-loud)
        # heartbeat is a stuck candidate.
        if age is not None and age <= threshold_min:
            continue
        # The 'claims working' signal. Only 'working' qualifies: idle/blocked/
        # unreadable are dark_sessions' territory (their own findings), not this.
        if herdr_agent_status(entry.herdr_pane, runner) != "working":
            continue
        # A recent commit in the seat's worktree DISPROVES stuck (visible work).
        # The cutoff is absolute, derived from the SAME injected clock the finding
        # runs on — a wall-clock "N minutes ago" would read the wrong window under
        # a fast-forwarded now_s (#181 review catch).
        cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - threshold_min * 60))
        if _committed_since(entry.repo_root, cutoff_iso, runner):
            continue
        if _progressed_without_commit(entry, now, threshold_min):
            continue
        try:
            slug = bridgelink._shared_slug(entry)
        except Exception:  # noqa: BLE001 — a bad entry must not sink the sweep
            slug = entry.repo_root
        shown = age if age is not None else threshold_min + 1
        findings.append(
            Finding(
                "stuck-while-working",
                entry.name,
                shown,
                f"seat '{entry.name}' (pair {slug}) reports herdr status=working "
                f"but shows NO progress for {shown} min — heartbeat stale and no "
                f"commit in its worktree within {threshold_min} min; likely stuck "
                f"mid-turn while the working status hides it. Read its pane/worktree "
                f"before trusting its self-report (#149/F10)",
            )
        )
    return findings


def pair_subjects(seat_name: str) -> set[str]:
    """The finding-subjects that belong to `seat_name`'s pair: the seat itself,
    its bridge peer(s) sharing the same slug, and the slug itself (deadlock
    findings key on the slug, seat findings on the name). A per-pair watchdog
    filters the sweep to these so it wakes ONLY on its own pair's findings, never
    the whole fleet's (the lane-2 arming reads this). An unknown seat resolves to
    just its own name — fail-narrow, never widen to the fleet."""
    subjects: set[str] = {seat_name}
    me = next((e for e in who(prune=False) if e.name == seat_name), None)
    if me is None:
        return subjects
    try:
        slug = bridgelink._shared_slug(me)
    except Exception:  # noqa: BLE001 — a bad entry must not widen the scope
        return subjects
    subjects.add(slug)
    for e in who(prune=False):
        if e.role not in bridgelink.BRIDGE_ROLES:
            continue
        try:
            if bridgelink._shared_slug(e) == slug:
                subjects.add(e.name)
        except Exception:  # noqa: BLE001
            continue
    return subjects


def _live_peer(seat_name: str) -> Entry | None:
    """`seat_name`'s live bridge peer (same slug, pid alive), or None."""
    entries = who(prune=False)
    me = next((e for e in entries if e.name == seat_name), None)
    if me is None:
        return None
    try:
        return waitstate._peer(me, entries)
    except Exception:  # noqa: BLE001 — no peer read, no reclassification
        return None


def findings_for_pair(findings: list[Finding], seat_name: str, *,
                      quiet_self: bool = False) -> list[Finding]:
    """Keep only findings whose subject belongs to `seat_name`'s pair.

    With `quiet_self` (the WAKE paths only: net.watch's --wake, --ids-only and
    exit code), the sweeping seat's OWN dark-session is reclassified to a
    quiet `self-quiet` record while a live peer is registered (#345). The
    dead-man's `check` never passes it: a page about your own silence still
    goes out when the peer's monitor is down, and the per-switchboard
    escalation ledger keeps it to one page per episode. A wake about
    your own silence is noise by construction: if you can read it you are not
    dark, and if you are dark you never read it. Its page belonged to the
    peer all along, whose sweep keeps the finding as a dark-session (subject =
    the other seat). With no live peer the seat's own loop is the last pager,
    so the finding stays. Only dark-session: blocked-seat on yourself is a
    prompt in your pane that may be the operator's, and it still pages."""
    subjects = pair_subjects(seat_name)
    scoped = [f for f in findings if f.subject in subjects]
    if not quiet_self or not any(f.kind == "dark-session" and f.subject == seat_name for f in scoped):
        return scoped
    peer = _live_peer(seat_name)
    if peer is None:
        return scoped
    return [
        Finding("self-quiet", f.subject, f.age_min,
                f"your own seat reads dark to the sweep ({f.detail}); {peer.name}'s "
                f"watchdog owns this finding (never wakes you)")
        if f.kind == "dark-session" and f.subject == seat_name else f
        for f in scoped
    ]


# The --wake loop's memory (#346): the alarm ids a seat has already been woken
# for, on disk so a Monitor re-arm (a fresh shell) does not re-fire them.
SEEN_DIR = "watch-seen"


def _seen_path(seat: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in seat)
    return net_dir() / SEEN_DIR / f"{safe}.json"


def _seat_started(seat: str) -> str:
    me = next((e for e in who(prune=False) if e.name == seat), None)
    return me.started_at if me is not None else ""


def _load_seen(seat: str, started_at: str) -> set[str]:
    """Ids already woken for during THIS registration. An unreadable file, or
    one a predecessor seat of the same name wrote, is empty: fail toward the
    wake, never toward silence."""
    try:
        data = json.loads(_seen_path(seat).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict) or data.get("started_at") != started_at:
        return set()
    ids = data.get("ids")
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def _save_seen(seat: str, started_at: str, ids: set[str]) -> None:
    path = _seen_path(seat)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"started_at": started_at, "ids": sorted(ids)}),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # an unsaved set re-wakes once; it never silences


def wake_step(findings: list[Finding], seat: str) -> str:
    """One edge-triggered loop step (#346): the text to print, '' for no wake.

    The old loop dedup'd on `--ids-only` set INEQUALITY, then made a second
    call for the detail. Three defects followed: acking one of two findings
    changed the set and re-echoed the other; every Monitor re-arm started with
    an empty set and re-fired everything; and a finding that cleared between
    the two calls left a header with no detail. Here the step wakes only on an
    id NOT already seen, the seen-set lives on disk, and the header and the
    detail come from ONE sweep.

    Seen-set rule: seen' = (seen | fresh) restricted to the CURRENT unacked
    alarm ids. So a cleared finding that recurs wakes again, and an acked one
    that is still present wakes once more when its ack lapses (the dead-man's
    window reopens at the same moment, see deadman.escalate). A
    `deadman ack --until-clear` keeps it quiet while the finding stays
    present; once a dead-man check has seen it gone, a return wakes again."""
    alarms = [f for f in findings if f.kind not in QUIET_KINDS]
    open_ = [f for f in alarms if not _acked_now(f)]
    ids = {f"{f.kind}:{f.subject}" for f in open_}
    started = _seat_started(seat)
    seen = _load_seen(seat, started)
    fresh = ids - seen
    _save_seen(seat, started, (seen | fresh) & ids)
    if not fresh:
        return ""
    first = [f for f in open_ if f"{f.kind}:{f.subject}" in fresh]
    rest = [f for f in open_ if f"{f.kind}:{f.subject}" not in fresh]
    lines = [f"PAIR-SWEEP new finding(s): {' '.join(sorted(fresh))} "
             f"- read the peer's pane before acting:"]
    lines += [f.line() for f in first]
    if rest:
        lines.append("still open (already woken for): "
                     + " ".join(sorted(f"{f.kind}:{f.subject}" for f in rest)))
    return "\n".join(lines)


def sweep(
    dark_min: int = DEFAULT_DARK_MIN,
    parked_min: int = DEFAULT_PARKED_MIN,
    now_s: float | None = None,
    runner: Runner = _run,
    refusal_window_min: int = DEFAULT_REFUSAL_WINDOW_MIN,
    refusal_min_count: int = DEFAULT_REFUSAL_MIN_COUNT,
    genuinely_dark_hours: int = DEFAULT_GENUINELY_DARK_HOURS,
    orphaned_draft_min: int = DEFAULT_ORPHANED_DRAFT_MIN,
    deadlock_min: int = DEFAULT_DEADLOCK_MIN,
    stuck_working_min: int = DEFAULT_STUCK_WORKING_MIN,
) -> list[Finding]:
    return (
        dark_sessions(dark_min, now_s, runner)
        + parked_wires(parked_min, now_s)
        + orphaned_drafts(orphaned_draft_min, now_s)
        + refused_heartbeats(refusal_window_min, refusal_min_count, now_s)
        + genuinely_dark_sessions(genuinely_dark_hours, now_s, runner)
        + model_flip_sessions(now_s)
        + deadlock_pairs(deadlock_min, now_s)
        + stuck_while_working(stuck_working_min, now_s, runner)
    )


def render(findings: list[Finding], *, header: bool = True) -> str:
    if not findings:
        return "net.watch: all clear" if header else ""
    lines = [f.line() for f in findings]
    if not header:
        # Header-less: only the finding lines. An edge-triggered watch loop dedups
        # on this output, and the header's embedded timestamp changes every sweep —
        # so with the header a loop re-fires every poll on IDENTICAL findings (the
        # 2026-07-22 doctrine-snippet defect). --no-header keeps the dedup stable.
        return "\n".join(lines)
    head = f"net.watch: {len(findings)} finding(s) at {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    return "\n".join([head, *lines])


def notify_imessage(text: str, handle: str, runner: Runner = _run) -> bool:
    """Send via Messages buddy-AppleScript (verified on macOS 26).

    Text travels as an osascript argv argument and is consumed as a data
    parameter inside the script — never interpolated into the script
    source, so message content cannot inject AppleScript.
    """
    script = (
        'on run argv\n'
        'tell application "Messages" to send (item 1 of argv) to buddy '
        '(item 2 of argv) of (service 1 whose service type is iMessage)\n'
        'end run'
    )
    try:
        cp = runner(["osascript", "-e", script, text, handle])
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="net.watch")
    parser.add_argument("--dark-min", type=int, default=DEFAULT_DARK_MIN)
    parser.add_argument("--parked-min", type=int, default=DEFAULT_PARKED_MIN)
    parser.add_argument(
        "--refusal-window-min", type=int, default=DEFAULT_REFUSAL_WINDOW_MIN
    )
    parser.add_argument(
        "--refusal-min-count", type=int, default=DEFAULT_REFUSAL_MIN_COUNT,
        help="lineage refusals within the window before it becomes a finding",
    )
    parser.add_argument(
        "--genuinely-dark-hours", type=int, default=DEFAULT_GENUINELY_DARK_HOURS,
        help="coarse git-progress-aware darkness: flag a seat silent >= N HOURS "
        "with no repo commits in the window (0 = off; this is the only "
        "armable form — the per-minute --dark-min is too noisy to page on)",
    )
    parser.add_argument(
        "--orphaned-draft-min", type=int, default=DEFAULT_ORPHANED_DRAFT_MIN,
        help="flag a recipient whose input box has held an unsent draft across "
        ">=2 wakes for at least N minutes (surface-only; never auto-submits)",
    )
    parser.add_argument(
        "--stuck-working-min", type=int, default=DEFAULT_STUCK_WORKING_MIN,
        help="flag a live herdr seat that claims status=working but shows no "
        "progress (stale heartbeat AND no commit) for >= N minutes (#149/F10)",
    )
    parser.add_argument(
        "--for", dest="for_seat", default=None, metavar="SEAT",
        help="scope findings to SEAT's pair (the seat, its bridge peer, and the "
        "slug) — a per-pair watchdog wakes only on its own pair's findings",
    )
    parser.add_argument(
        "--no-header", action="store_true",
        help="print only the finding lines (no timestamped 'net.watch: N ...' "
        "header) — for an edge-triggered watch loop whose dedup must be stable "
        "across sweeps (the header's timestamp changes every poll)",
    )
    parser.add_argument(
        "--ids-only", action="store_true",
        help="print only sorted 'kind:subject' finding IDS (no age, no detail) — "
        "the STABLE dedup KEY for a watch loop. A finding's '(N min)' increments "
        "every minute, so a raw-line dedup wake-storms ~1/min on a persisting "
        "finding; the id set does not change while the same stall persists",
    )
    parser.add_argument(
        "--hide-acked", action="store_true",
        help="drop findings a seat has acked this episode with an unexpired "
        "ack (net.deadman ack) — so an acked stall does not re-wake the "
        "watchdog every time some OTHER finding changes the id set (T2/P6)",
    )
    parser.add_argument(
        "--wake", action="store_true",
        help="one edge-triggered watchdog loop step (requires --for): print a "
        "header plus the detail of every NEW unacked alarm in one output, and "
        "nothing for an alarm this seat was already woken for. The seen-set is "
        "kept on disk, so a Monitor re-arm or an ack never re-fires a "
        "persisting finding (#346). Exit 1 on a wake",
    )
    parser.add_argument(
        "--notify",
        choices=["stdout", "imessage"],
        default="stdout",
        help="imessage requires MAW_WATCH_IMESSAGE_TO=<handle> in the env",
    )
    parser.add_argument(
        "--quiet-when-clear",
        action="store_true",
        help="print nothing on all-clear (cron-friendly)",
    )
    args = parser.parse_args(argv)

    # Reap confirmed-dead seats FIRST (explicit + reported), so the registry is
    # clean before the dark/parked sweep and zombie cleanup never relies on a
    # manual `who` (item #8). Reaping is routine cleanup, not an alarm — it does
    # not set the exit code, but it is always announced when it happens.
    reaped = reap_dead()
    if reaped:
        print(f"net.watch: reaped {len(reaped)} dead seat(s): {', '.join(reaped)}")

    findings = sweep(
        args.dark_min,
        args.parked_min,
        refusal_window_min=args.refusal_window_min,
        refusal_min_count=args.refusal_min_count,
        genuinely_dark_hours=args.genuinely_dark_hours,
        orphaned_draft_min=args.orphaned_draft_min,
        stuck_working_min=args.stuck_working_min,
    )
    if args.for_seat:
        # net.watch is the wake surface, never the pager (net.deadman check
        # pages, on the raw findings), so the seat's own silence is quieted here.
        findings = findings_for_pair(findings, args.for_seat, quiet_self=True)
    if args.wake:
        if not args.for_seat:
            print("net.watch: --wake needs --for <your-seat>", file=sys.stderr)
            return 2
        text = wake_step(findings, args.for_seat)
        if text:
            print(text)
        return 1 if text else 0
    if args.hide_acked:
        findings = [f for f in findings if not _acked_now(f)]
    alarms = [f for f in findings if f.kind not in QUIET_KINDS]
    if args.ids_only:
        findings = alarms
        text = "\n".join(sorted({f"{f.kind}:{f.subject}" for f in findings}))
    else:
        text = render(findings, header=not args.no_header)

    if findings and args.notify == "imessage":
        handle = os.environ.get("MAW_WATCH_IMESSAGE_TO", "")
        if not handle:
            print(
                "net.watch: --notify imessage but MAW_WATCH_IMESSAGE_TO unset; "
                "falling back to stdout",
                file=sys.stderr,
            )
        elif not notify_imessage(text, handle):
            print("net.watch: iMessage notify failed; output below", file=sys.stderr)

    if alarms or not args.quiet_when_clear:
        print(text)
    return 1 if alarms else 0


def _acked_now(f: Finding) -> bool:
    """True if `f` holds an unexpired ack from THIS episode (the dead-man's own
    rule, so the watch and the dead-man cannot disagree on what is acked)."""
    from net import deadman  # lazy: deadman imports this module

    now = _now_s()
    return deadman._acked_valid(deadman.finding_id(f), now, deadman.finding_episode_start(f, now))


if __name__ == "__main__":
    raise SystemExit(main())
