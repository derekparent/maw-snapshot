"""net.deadman — the dead-man escalation (#149/F10, lane 3).

A pair-scoped STALL finding that stays UNACKED past a threshold escalates to a
page. This is the last line of the F10 defense: lane 2's watchdog WAKES a
supervising seat on a stall; the auto-ack economics mean a seat that reads the
peer's pane and finds real work ACKS the finding (a cheap explicit act — a
receipt, `deadman ack <id>`, NOT "the alarm stopped"). Only a finding nobody
acked — because the peer really is wedged, OR because the supervising seat
itself stalled — rides this clock to the operator.

WHAT ESCALATES. Stall-class findings ONLY (a seat is not progressing and a human
should intervene): stuck-while-working, blocked-seat, dark-session,
deadlock-both-parked. NOT model-flip (a tier notice — the 2026-07-22 live drill
proved it false-positives and it must never reach the operator's phone), watcher-quiet,
orphaned-draft, parked-wire, refused-heartbeat.

EVIDENCE-GATED ON SILENCE (#190). `dark-session` fires on a stale `last_seen`,
which ticks only on UserPromptSubmit — so a seat working through a long
tool-heavy turn reads dark while alive, and on 2026-07-22 the dead-man paged for
BOTH seats of a demonstrably working pair. Before paging on silence alone, the
dead-man now asks `net.liveness` for proof of life that needs no prompt submit
(the seat's own transcript mtime). Fresh evidence suppresses the page and records
a suppression; NO evidence still pages (fail-open toward the page). The gate is
scoped to `dark-session` only — the other stall kinds already carry positive
evidence of a wedge, and a wedged seat can still be writing a transcript.

THE PAGE IS LAYERED. The autonomous channel (runs from the watchdog loop, needs
no agent) is a LOUD stderr line plus a best-effort `net send` to the CoS; the
probe-gated PushNotification to the operator is the supervising AGENT's job on the
escalation wake (a Monitor loop cannot call the PushNotification tool — the seat
command documents that half). The pager is INJECTED so tests assert the
escalation RECORD + the pager call, never a real page: exactly one real push
happens, in the live drill, the operator present.

THE CLOCK STARTS WHEN THE FINDING SURFACES (T2). `escalate_min` is "unacked
for N minutes since a sweep FIRST SAW this finding", not "the stall is N minutes
old". A finding surfaces only once its stall is already past the watch threshold
(15 min), so a stall-age clock was always past 10 min on first sight: the
watchdog woke the supervisor and the dead-man paged in the SAME poll, before
any ack could land. On 2026-10-01 both escalations that night fired seconds
before the supervisor's ack. The first sighting is recorded per episode in
FIRST_SEEN_LEDGER; the supervisor gets the full window to look and ack. Cost:
a real wedge pages `escalate_min` later than before. A ledger that cannot be read
or written falls back to the stall age (the old rule): fail toward the page.
One exception (#379): a finding that has ONLY ever been seen on checks that
could not read its subject (`Finding.unread`: a dark-session reported because
the status read failed) is closed when a check that could read the subject
does not see it (`note_gone`), and its next sighting starts the window again.
Such a sighting is nobody having looked, not a stall. A finding seen on any
readable check keeps its first-seen stamp for the episode: a real stall whose
finding flickers (a hung seat reading `working` drops `stuck-while-working`
for a tick on every local-branch commit) must not restart the window.

CLOCK STEPS (#379). An ack dated up to ACK_CLOCK_SKEW_S after the check still
counts (the clock stepped back just after it). One dated further ahead is
voided the first time a check meets it (`void_future_acks`), so it can never
become valid when its date arrives.

EPISODE-SCOPED. An ack or a prior escalation counts only if it happened DURING
the current stall episode (ts >= now - age_min): a finding recurs every sweep
with the same id, so an ack suppresses re-escalation for THIS episode, and a new
stall after a gap escalates afresh. Escalations are recorded (gate 8) so a
close-out can prove the dead-man fired without re-deriving it.

AN UNTIL-CLEAR ACK DIES WHEN THE FINDING CLEARS. `age_min` is the subject's
`last_seen` age, and `last_seen` moves only on a prompt submit. A blocked-seat
that clears (the operator answers the prompt in the pane) and comes back later
is still inside the same age-derived episode, so an until-clear ack would cover
the new stall too and the dead-man would never page it. The check therefore
binds the ack to the finding's CONTINUOUS, OBSERVED presence: a check that
finds an until-clear-acked id gone records a `cleared` marker, and that marker
is the episode's new start. Acks, first sightings and escalations from before
it no longer count.

"Gone" is one check that could read the subject's status and did not see the
finding: a real clear can be shorter than a tick (the operator answers, the
seat works for a minute, the next prompt appears), so it is taken at once. A
tick on which the status could NOT be read (`Finding.unread`) drops the
finding from that sweep without anyone having looked, and must not kill a
valid ack: it takes two such ticks at least ABSENT_CONFIRM_S apart, with no
sighting between them. "Observed" means some
check saw the finding within PRESENCE_GAP_S: a clear can only be noticed by a
check that runs, and both seats' loops can be down at once (a Monitor expired
and not yet re-armed). If the finding cleared and came back while nothing
watched, the ack would cover a new stall forever. So when no check has seen an
until-clear-acked finding for longer than the gap, the ack is ended the same
way, present or not: the supervisor is woken once more and looks again. The
last sighting of each such finding is kept in PRESENCE_STATE.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from net.registry import net_dir
from net.transport import Runner, _run
from net.watch import Finding, findings_for_pair, pair_subjects, sweep

if TYPE_CHECKING:  # a RUNTIME import would defeat the lazy import in
    from net.liveness import Evidence  # _default_evidence (see its docstring)

DEFAULT_ESCALATE_MIN = 10
# An ack is "I looked NOW", not "ignore this forever". It expires: past the ttl a
# persisting stall re-escalates (advisor ruling — the min-12-ack, min-20-wedge
# case, where the peer wedges AFTER the supervisor's look within the same episode).
DEFAULT_ACK_TTL_MIN = 30

# Stall-class only — see the module docstring. model-flip/watcher-quiet/etc. are
# informational and must NEVER reach the operator's phone (the live-drill FP proved it).
ESCALATABLE_KINDS = frozenset({
    "stuck-while-working",
    "blocked-seat",
    "dark-session",
    "deadlock-both-parked",
})

# THE LIVENESS-EVIDENCE GATE (#190). `dark-session` fires on SILENCE, and the
# heartbeat it reads (`last_seen`) ticks only on UserPromptSubmit — so a seat
# working through a long tool-heavy turn reads dark while alive. The 2026-07-22
# drill escalated exactly that, for both seats at once. Before paging on silence,
# the dead-man asks for proof of life that does NOT need a prompt submit (see
# net.liveness: the seat's own transcript mtime). Evidence this fresh means alive.
#
# Deliberately NOT applied to the other stall kinds: stuck-while-working and
# blocked-seat already carry POSITIVE evidence of a wedge (agent_status plus a
# stale heartbeat plus no commit), and a wedged seat can still be writing to its
# transcript. Gating those would be the "evidence satisfied by a wedged seat"
# hole. Silencing a routine long turn stays the ACK's job, not this gate's.
DEFAULT_EVIDENCE_FRESH_MIN = 5
EVIDENCE_GATED_KINDS = frozenset({"dark-session"})

# Given a finding's subject seat, its liveness Evidence (age in minutes + the
# basis that age rests on). An age of None means NO EVIDENCE, which escalates.
EvidenceFn = Callable[[str], "Evidence"]

ACK_LEDGER = "watch-acks.jsonl"
ESCALATION_LEDGER = "watch-escalations.jsonl"
# Suppressions live in their OWN ledger. Putting them in ESCALATION_LEDGER would
# make _ids_in_episode treat a suppression as "already escalated this episode"
# and swallow the real page that follows when the evidence goes stale.
SUPPRESSION_LEDGER = "watch-suppressions.jsonl"
# When a sweep first saw each finding this episode: the dead-man's clock start.
FIRST_SEEN_LEDGER = "watch-first-seen.jsonl"
# {finding id: {"seen": epoch, "absent": epoch}} for findings under a live
# until-clear ack: the last check that saw each one, and the first check of the
# current run of checks that did not.
PRESENCE_STATE = "watch-presence.json"
# An until-clear ack ends when no check has seen its finding for this long
# (several missed 45 s ticks: both loops were down, so a clear could have gone
# unseen), or when it has been absent, its subject's status unreadable, across
# two checks this far apart. (Absent with a readable status ends it at once.)
PRESENCE_GAP_S = 5 * 60
ABSENT_CONFIRM_S = 40
# An ack stamped up to this far AFTER the check still counts (#379). The
# machine clock can step back (an NTP correction) right after an ack: the
# fresh ack then reads as future-dated, and ignoring it paged the operator
# for a stall the supervisor had just looked at. An ack dated further ahead
# than this is not a clock step: it is voided at first sight (`void_future_acks`),
# so it cannot come alive when its date arrives.
ACK_CLOCK_SKEW_S = 15 * 60

# A pager: given a stalled finding + the acting seat, deliver the autonomous
# escalation and report (delivered, method).
Pager = Callable[[Finding, str], "tuple[bool, str]"]


def finding_id(f: Finding) -> str:
    """A stable id for a finding across sweeps — the same stall keeps the same id,
    so an ack for it suppresses re-escalation until it clears."""
    return f"{f.kind}:{f.subject}"


def _ledger_path(name: str) -> Path:
    return net_dir() / name


def _read_ledger(name: str) -> list[dict]:
    path = _ledger_path(name)
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue  # a torn line is not a record
            if isinstance(rec, dict):
                out.append(rec)
    except OSError:
        return []
    return out


def _append_ledger(name: str, rec: dict) -> None:
    path = _ledger_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _now_s() -> float:
    return time.time()


def _ts(now_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s))


def ack(fid: str, seat: str, now_s: float | None = None,
        ttl_min: int = DEFAULT_ACK_TTL_MIN, *, until_clear: bool = False) -> str:
    """Record an ACK for a finding id — the explicit cheap act that says 'I looked
    NOW and it is fine (or I am handling it)'. The ack EXPIRES after `ttl_min`, so
    a stall the peer enters AFTER the look still escalates. Returns the line.

    `until_clear` (#345) holds for as long as the finding stays present
    instead: for a wait whose end is not the peer's to make (the operator's
    prompt sitting in the peer's pane, an outstanding arm). It dies once
    dead-man checks see the finding gone, or once no check has watched it for
    PRESENCE_GAP_S (`note_cleared`), so a stall that returns pages afresh, and
    `revoke` cancels it."""
    now = now_s if now_s is not None else _now_s()
    rec = {"ts": _ts(now), "epoch": now, "finding_id": fid, "seat": seat,
           "ttl_min": ttl_min}
    if until_clear:
        rec["until_clear"] = True
    _append_ledger(ACK_LEDGER, rec)
    valid = "until the finding clears" if until_clear else f"{ttl_min}min"
    return f"acked {fid} by {seat} at {_ts(now)} (valid {valid})"


def revoke(fid: str, seat: str, now_s: float | None = None) -> str:
    """Cancel every ack recorded for `fid` so far (#345: an ack given before the
    pane was read had no un-ack). The dead-man treats the finding as unacked
    from now on; its ack window does not reopen, so a stall already past it
    pages on the next check."""
    now = now_s if now_s is not None else _now_s()
    _append_ledger(ACK_LEDGER, {"ts": _ts(now), "epoch": now, "finding_id": fid,
                                "seat": seat, "revoke": True})
    return f"revoked acks for {fid} by {seat} at {_ts(now)}"


def _load_presence() -> "dict[str, dict]":
    """PRESENCE_STATE, or {} if it is missing or unreadable (then each ack's own
    time is its last sighting: an unreadable state fails toward ending the ack)."""
    try:
        data = json.loads(_ledger_path(PRESENCE_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _save_presence(state: "dict[str, dict]") -> None:
    path = _ledger_path(PRESENCE_STATE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # unsaved: the last sighting does not advance, so the ack ends early


def void_future_acks(seat: str, now: float,
                     subjects: "set[str] | None" = None) -> list[str]:
    """Void every ack dated more than ACK_CLOCK_SKEW_S after `now` by
    appending a `void` record naming it. Returns the ids voided.

    Ignoring such an ack at check time is not enough: it stays in the ledger,
    and on a finding that never clears it would become a valid ack on the day
    its date arrives, with nobody having looked (#379). A void is permanent
    and names one ack by its epoch; other acks for the id are untouched.
    Scope as `note_cleared`: ids of this pair's subjects, or, without
    `subjects`, acks this seat recorded."""
    pending: dict[tuple[str, float], dict] = {}
    for rec in _read_ledger(ACK_LEDGER):
        fid, epoch = rec.get("finding_id"), rec.get("epoch")
        if not isinstance(fid, str) or isinstance(epoch, bool) \
                or not isinstance(epoch, (int, float)):
            continue
        if rec.get("void"):
            voids = rec.get("voids")
            if isinstance(voids, (int, float)):
                pending.pop((fid, float(voids)), None)
            continue
        if rec.get("revoke") or rec.get("cleared"):
            continue
        if epoch > now + ACK_CLOCK_SKEW_S:
            pending[(fid, float(epoch))] = rec
    voided: list[str] = []
    for (fid, epoch), rec in pending.items():
        subject = fid.split(":", 1)[1] if ":" in fid else ""
        in_scope = subject in subjects if subjects is not None else rec.get("seat") == seat
        if not in_scope:
            continue
        try:
            _append_ledger(ACK_LEDGER, {"ts": _ts(now), "epoch": now, "finding_id": fid,
                                        "seat": seat, "void": True, "voids": epoch})
        except OSError:
            continue  # unwritten: still ignored this check, and the next one tries again
        voided.append(fid)
    return voided


def _voided(rec: dict, live_epoch: object) -> bool:
    """True if `rec` is a void record naming the ack stamped `live_epoch`."""
    return rec.get("void") is True and rec.get("voids") == live_epoch


def _stamp(value: object, lo: float, hi: float) -> "float | None":
    """`value` as an epoch within [lo, hi], else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if lo <= value <= hi else None


def note_cleared(present: "set[str]", seat: str, now: float,
                 subjects: "set[str] | None" = None,
                 unreadable: "set[str] | frozenset[str]" = frozenset()) -> list[str]:
    """End every live until-clear ack whose finding is gone, or went unwatched,
    by recording a `cleared` marker for it. Returns the ids marked. The marker
    ends the episode (see `finding_episode_start`), so the ack cannot cover the
    finding when it comes back.

    Gone: absent from this check. If the check could not read the subject's
    status (`unreadable`, the subjects of `Finding.unread` findings), that is
    not yet a clear: it takes a second such check at least ABSENT_CONFIRM_S
    after the first, with no sighting between. Unwatched: no check has seen it
    for more than PRESENCE_GAP_S,
    counted from the ack if none has seen it since; it may have cleared and
    returned with no loop running, so the ack ends even if it is present now.

    Scope: only ids this check could have seen. `subjects` is the checking
    seat's pair (`watch.pair_subjects`); without it, only acks this seat
    recorded. Another pair's ack is never cleared from here. One marker per
    ack: once written, the ack is no longer live."""
    live: dict[str, dict] = {}
    for rec in _read_ledger(ACK_LEDGER):
        fid = rec.get("finding_id")
        if not isinstance(fid, str) or not isinstance(rec.get("epoch"), (int, float)):
            continue
        if rec.get("void"):
            if fid in live and _voided(rec, live[fid].get("epoch")):
                del live[fid]
        elif rec.get("revoke") or rec.get("cleared"):
            live.pop(fid, None)
        elif rec.get("until_clear") is True:
            live[fid] = rec
    state = _load_presence()
    before = json.dumps(state, sort_keys=True)
    for fid in [k for k in state if k not in live]:
        del state[fid]
    marked: list[str] = []
    for fid, rec in live.items():
        subject = fid.split(":", 1)[1] if ":" in fid else ""
        in_scope = subject in subjects if subjects is not None else rec.get("seat") == seat
        if not in_scope:
            continue
        # An ack stamped later than now (the clock stepped back; one dated
        # further ahead than ACK_CLOCK_SKEW_S was voided before this ran) has
        # no usable ack time: the first check that meets it is its first
        # sighting, and it is ended like any other.
        acked = float(rec["epoch"])
        future = acked > now
        st = state.get(fid, {})
        last = _stamp(st.get("seen"), 0.0 if future else acked, now) or (now if future else acked)
        absent = _stamp(st.get("absent"), last, now)
        watched = now - last <= PRESENCE_GAP_S
        if watched and fid in present:
            state[fid] = {"seen": now}
            continue
        if watched and subject in unreadable and (
                absent is None or now - absent < ABSENT_CONFIRM_S):
            state[fid] = {"seen": last, "absent": now if absent is None else absent}
            continue
        try:
            _append_ledger(ACK_LEDGER, {"ts": _ts(now), "epoch": now, "finding_id": fid,
                                        "seat": seat, "cleared": True})
        except OSError:
            continue  # unwritten: the next check tries again
        state.pop(fid, None)
        marked.append(fid)
    if json.dumps(state, sort_keys=True) != before:
        _save_presence(state)
    return marked


def _last_cleared(fid: str, now: float) -> float | None:
    """Epoch of the newest `cleared` marker for `fid` not in the future."""
    best: float | None = None
    for rec in _read_ledger(ACK_LEDGER):
        if rec.get("finding_id") != fid or rec.get("cleared") is not True:
            continue
        epoch = rec.get("epoch")
        if isinstance(epoch, (int, float)) and epoch <= now:
            best = float(epoch) if best is None else max(best, float(epoch))
    return best


def finding_episode_start(f: Finding, now: float) -> float:
    """When the current stall episode of `f` began: the subject's `last_seen`
    (now - age_min), or the newest `cleared` marker if that is later. net.watch
    uses the same function, so the wake and the page agree on what is acked."""
    start = now - f.age_min * 60
    cleared = _last_cleared(finding_id(f), now)
    return start if cleared is None else max(start, cleared)


def _ids_in_episode(ledger: str, fid: str, episode_start: float) -> bool:
    """True if `ledger` has a record for `fid` whose epoch is within the current
    episode (>= episode_start) — the record belongs to THIS stall, not a stale one
    from a prior episode with the same id. Used for the ESCALATION dedup (once per
    episode). Acks use _acked_valid, which ALSO enforces the ttl."""
    for rec in _read_ledger(ledger):
        if rec.get("finding_id") != fid:
            continue
        epoch = rec.get("epoch")
        if isinstance(epoch, (int, float)) and epoch >= episode_start:
            return True
    return False


def _episode_acks(fid: str, episode_start: float, now: float | None = None) -> list[dict]:
    """Ack records for `fid` in the current episode that no later `revoke`
    cancelled, oldest first. With `now`, an ack stamped later than it is not
    an ack: it would sit inside every future episode, past any `cleared`
    marker, and never lapse."""
    acks: list[dict] = []
    for rec in _read_ledger(ACK_LEDGER):
        if rec.get("finding_id") != fid:
            continue
        epoch = rec.get("epoch")
        if not isinstance(epoch, (int, float)):
            continue
        if rec.get("void"):
            acks = [a for a in acks if not _voided(rec, a.get("epoch"))]
            continue
        if rec.get("revoke"):
            acks = []  # a revoke cancels every ack before it
            continue
        if rec.get("cleared"):
            # A marker, not an ack. It ends every ack written before it, by
            # ledger order as a revoke does: by time alone, an ack stamped
            # later than the marker would outlive it.
            acks = []
            continue
        # Up to ACK_CLOCK_SKEW_S ahead still counts: a clock that stepped
        # back just after the ack. Further ahead is never an ack.
        if epoch >= episode_start and (now is None or epoch <= now + ACK_CLOCK_SKEW_S):
            acks.append(rec)
    return acks


def _ack_expiry(rec: dict) -> float | None:
    """Epoch an ack lapses at; None = holds until the finding clears."""
    if rec.get("until_clear") is True:
        return None
    ttl_min = rec.get("ttl_min", DEFAULT_ACK_TTL_MIN)
    if not isinstance(ttl_min, (int, float)) or isinstance(ttl_min, bool):
        ttl_min = DEFAULT_ACK_TTL_MIN
    return float(rec["epoch"]) + ttl_min * 60


def _acked_valid(fid: str, now: float, episode_start: float) -> bool:
    """True if an UNEXPIRED ack for `fid` exists within the current episode. An ack
    counts only if its epoch is within the episode AND within its own ttl window
    (epoch >= now - ttl) — 'I looked NOW', not 'ignore forever' (advisor ruling).
    An `until_clear` ack holds for the episode; a `revoke` cancels earlier acks."""
    for rec in _episode_acks(fid, episode_start, now):
        expiry = _ack_expiry(rec)
        if expiry is None or now <= expiry:
            return True
    return False


def _last_lapse(fid: str, now: float, episode_start: float) -> float | None:
    """When the newest LAPSED ack of this episode expired, or None. A lapse
    reopens the supervisor's ack window (#345: a peer-repo executor 10-02, the
    ack ran out while the operator's prompt still sat in the pane and the same
    poll paged the operator a second time). The watch loop re-wakes the supervisor at the
    lapse, so it gets the full window to look again before the page."""
    lapses = [e for e in (_ack_expiry(r) for r in _episode_acks(fid, episode_start, now))
              if e is not None and e < now]
    return max(lapses) if lapses else None


def _first_seen_records() -> "list[dict] | None":
    """FIRST_SEEN_LEDGER's records in order, or None if it cannot be read."""
    path = _ledger_path(FIRST_SEEN_LEDGER)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except (OSError, ValueError):  # ValueError: a non-UTF-8 byte
        return None
    out: list[dict] = []
    for raw in lines:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):  # valid JSON that is not a record (e.g. `5`) is skipped
            out.append(rec)
    return out


def _unread_only_runs(records: "list[dict]") -> "dict[str, bool]":
    """{finding id: True if its open run of sightings is marked unread and
    not marked read} for each id with an open run, by ledger order. A `gone`
    record ends a run. A run with no mark at all (written before the marks
    existed, or whose mark could not be written) is a real one: never closed."""
    runs: dict[str, bool] = {}
    read: set[str] = set()
    for rec in records:
        fid = rec.get("finding_id")
        if not isinstance(fid, str):
            continue
        if rec.get("gone") is True:
            runs.pop(fid, None)
            read.discard(fid)
        elif rec.get("mark") == "read":
            read.add(fid)
            if fid in runs:
                runs[fid] = False
        elif rec.get("mark") == "unread":
            if fid in runs and fid not in read:
                runs[fid] = True
        else:
            runs.setdefault(fid, False)
    return runs


def _note_sighting(fid: str, now: float, unread: bool) -> None:
    """Mark the open run of `fid` by how this check saw it: `unread` (the
    subject could not be read) or `read`. One `read` mark is final for the
    run. Called after `_first_seen`, which opens the run. An unwritable or
    unreadable ledger leaves the run unmarked, which is never closed."""
    records = _first_seen_records()
    if records is None:
        return
    runs = _unread_only_runs(records)
    if fid not in runs:
        return
    marked_read = any(r.get("finding_id") == fid and r.get("mark") == "read"
                      for r in records[_last_gone_index(records, fid):])
    if (unread and (runs[fid] or marked_read)) or (not unread and marked_read):
        return  # already says so
    try:
        _append_ledger(FIRST_SEEN_LEDGER, {"ts": _ts(now), "epoch": now, "finding_id": fid,
                                           "mark": "unread" if unread else "read"})
    except OSError:
        pass


def _last_gone_index(records: "list[dict]", fid: str) -> int:
    """Index just past the newest `gone` record for `fid` (0 if none)."""
    last = 0
    for i, rec in enumerate(records):
        if rec.get("finding_id") == fid and rec.get("gone") is True:
            last = i + 1
    return last


def note_gone(present: "set[str]", now: float, subjects: "set[str] | None",
              unreadable: "set[str] | frozenset[str]" = frozenset()) -> list[str]:
    """Close the sighting of every finding that was only ever seen on checks
    that could not read its subject, once a check that could read it does not
    see it, by appending a `gone` record. Returns the ids closed.

    Without this such a finding kept its first-seen stamp for the whole
    age-derived episode: a dark-session that exists only on a tick where the
    status read failed, absent on every readable tick, paged the operator the
    second time it appeared, hours later (#379: about one false page per 12
    hours at 5 percent unreadable reads).

    Never closed: a finding seen on ANY readable check (it keeps its stamp for
    the episode, as before: a real stall whose finding flickers must not
    restart the window); an id whose subject could not be read on this check
    (`unreadable`; nobody looked, so it is not known to be gone); and, with no
    `subjects`, anything (the caller cannot say which ids it could have seen)."""
    if subjects is None:
        return []
    records = _first_seen_records()
    if records is None:
        return []
    closed: list[str] = []
    for fid, unread_only in _unread_only_runs(records).items():
        subject = fid.split(":", 1)[1] if ":" in fid else ""
        if not unread_only or fid in present or subject not in subjects \
                or subject in unreadable:
            continue
        try:
            _append_ledger(FIRST_SEEN_LEDGER, {"ts": _ts(now), "epoch": now,
                                               "finding_id": fid, "gone": True})
        except OSError:
            continue  # unwritten: the sighting stays open, toward the page
        closed.append(fid)
    return closed


def _first_seen(fid: str, now: float, episode_start: float) -> float:
    """Epoch a sweep first saw `fid` in this episode's current run of
    sightings, recording `now` if this is the first. A `gone` record
    (`note_gone`) ends a run: sightings before it no longer count. If the
    ledger cannot be read or written, fall back to
    the episode start (the old stall-age rule): a broken ledger must fail
    toward the page, never restart the clock every sweep."""
    records = _first_seen_records()
    if records is None:
        return episode_start
    seen: float | None = None
    for rec in records:
        if rec.get("finding_id") != fid:
            continue
        if rec.get("gone") is True:
            seen = None  # by ledger order, as a revoke: a clock step cannot undo it
            continue
        if "mark" in rec:
            continue  # a mark on the run (`_note_sighting`), not a sighting
        epoch = rec.get("epoch")
        if (seen is None and isinstance(epoch, (int, float))
                and episode_start <= epoch <= now):
            seen = float(epoch)
    if seen is not None:
        return seen
    try:
        _append_ledger(FIRST_SEEN_LEDGER, {"ts": _ts(now), "epoch": now, "finding_id": fid})
    except OSError:
        return episode_start
    return now


def _default_pager(finding: Finding, seat: str, runner: Runner = _run) -> "tuple[bool, str]":
    """The autonomous escalation channel from the watchdog loop: a LOUD stderr
    line + a best-effort `net send` to the CoS. The real the operator page (probe-gated
    PushNotification) is the supervising agent's job on the escalation wake — a
    Monitor loop cannot call that tool. Never raises."""
    line = (f"DEAD-MAN ESCALATION: {finding.kind} for {finding.subject} unacked "
            f"{finding.age_min} min — {finding.detail}")
    print(f"[deadman] {line} — PAGE THE OPERATOR NOW (probe-gated PushNotification)", file=sys.stderr)
    method = "stderr"
    # Best-effort CoS relay. Message via a temp file, NEVER --message (a code span
    # in --message is shell-executed — the confirmed net-send footgun).
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
            tf.write("DEAD-MAN (maw pair): " + line)
            tmp = tf.name
        # maw's own launcher, never `python3 -m` from the Monitor's cwd (#324).
        maw_run = str(Path(__file__).resolve().parents[1] / "bin" / "maw-run")
        cp = runner([maw_run, "net.cli", "send", "cos", "--as", seat, "--from-file", tmp])
        os.unlink(tmp)
        if getattr(cp, "returncode", 1) == 0:
            method = "stderr+net-send-cos"
    except Exception:  # noqa: BLE001 — a relay failure must not sink the escalation
        pass
    return True, method


def _default_evidence(subject: str) -> "Evidence":
    """The real evidence source, imported lazily so `net.deadman` keeps working if
    a stale transplant lacks `net.liveness` (no evidence -> the page still fires)."""
    from net.liveness import evidence_for

    return evidence_for(subject)


def _evidence(subject: str, evidence_fn: EvidenceFn) -> "tuple[float | None, str]":
    """(age, basis) for the subject, or (None, "unavailable") if it cannot be read.
    A broken evidence source is NO EVIDENCE — it must never swallow the page it was
    asked to justify."""
    try:
        ev = evidence_fn(subject)
    except Exception:  # noqa: BLE001 — see above; fail toward the page
        return None, "unavailable"
    age = getattr(ev, "age_min", None)
    basis = getattr(ev, "basis", "unknown")
    return (age if isinstance(age, (int, float)) else None), basis


def _record_suppression(fid: str, f: Finding, seat: str, now: float,
                        age_min: float, basis: str, episode_start: float) -> None:
    """Record a page NOT sent, once per episode. A suppressed alarm that leaves no
    trace is indistinguishable from a dead alarm — gate 8 (audit the refusals)."""
    if _ids_in_episode(SUPPRESSION_LEDGER, fid, episode_start):
        return
    _append_ledger(SUPPRESSION_LEDGER, {
        "ts": _ts(now), "epoch": now, "finding_id": fid, "kind": f.kind,
        "subject": f.subject, "age_min": f.age_min, "detail": f.detail,
        "escalated": False, "suppressed_by": "liveness-evidence",
        "evidence_age_min": age_min, "evidence": basis, "seat": seat,
    })


def escalate(
    findings: list[Finding],
    seat: str,
    now_s: float | None = None,
    *,
    escalate_min: int = DEFAULT_ESCALATE_MIN,
    pager: Pager | None = None,
    evidence_fn: EvidenceFn | None = None,
    evidence_fresh_min: int = DEFAULT_EVIDENCE_FRESH_MIN,
    subjects: "set[str] | None" = None,
) -> list[dict]:
    """For each STALL-class finding unacked for `escalate_min` since a sweep first
    saw it, fire the pager
    ONCE per episode and record the escalation. Returns the escalation records
    produced this call (empty if nothing escalated). Deliberately errs toward the
    page: the auto-ack path is how a routine long turn is silenced, so anything
    that reaches here without an ack IS the dead-man's job."""
    now = now_s if now_s is not None else _now_s()
    the_pager = pager if pager is not None else _default_pager
    the_evidence_fn = evidence_fn if evidence_fn is not None else _default_evidence
    produced: list[dict] = []
    # `findings` must be the RAW pair findings (never net.watch's quiet_self
    # view): an id missing from them is taken as cleared.
    present = {finding_id(f) for f in findings}
    unreadable = {f.subject for f in findings if f.unread}
    void_future_acks(seat, now, subjects)
    note_cleared(present, seat, now, subjects, unreadable)
    note_gone(present, now, subjects, unreadable)
    for f in findings:
        if f.kind not in ESCALATABLE_KINDS:
            continue
        fid = finding_id(f)
        episode_start = finding_episode_start(f, now)
        first = _first_seen(fid, now, episode_start)
        _note_sighting(fid, now, f.unread)
        if now - first < escalate_min * 60:
            continue  # the supervisor's ack window is still open
        if _acked_valid(fid, now, episode_start):
            continue  # acked this episode AND the ack is unexpired — supervisor looked
        lapsed = _last_lapse(fid, now, episode_start)
        if lapsed is not None and now - lapsed < escalate_min * 60:
            continue  # an ack just lapsed: the window reopens for a fresh look
        if f.kind in EVIDENCE_GATED_KINDS:
            age, basis = _evidence(f.subject, the_evidence_fn)
            if age is not None and age <= evidence_fresh_min:
                _record_suppression(fid, f, seat, now, age, basis, episode_start)
                continue  # proof of life that needed no prompt submit — not dark
        if _ids_in_episode(ESCALATION_LEDGER, fid, episode_start):
            continue  # already escalated this episode — do not re-page every sweep
        delivered, method = the_pager(f, seat)
        rec = {
            "ts": _ts(now), "epoch": now, "finding_id": fid, "kind": f.kind,
            "subject": f.subject, "age_min": f.age_min, "detail": f.detail,
            "escalated": True, "delivered": bool(delivered), "method": method,
            "seat": seat,
        }
        _append_ledger(ESCALATION_LEDGER, rec)
        produced.append(rec)
    return produced


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="net.deadman",
        description="dead-man escalation: an unacked pair stall past threshold pages the operator (#149/F10)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ack", help="ack a finding id (the cheap explicit act, not 'alarm stopped')")
    a.add_argument("finding_id", help="the finding id, e.g. 'stuck-while-working:executor'")
    a.add_argument("--as", dest="seat", default=None, help="acking seat (default: MAW_BRIDGE_ROLE)")
    a.add_argument("--ttl-min", type=int, default=DEFAULT_ACK_TTL_MIN,
                   help="how long this ack suppresses the dead-man (default 30) — an "
                        "ack is 'I looked NOW', so a stall entered after it re-escalates")
    mode = a.add_mutually_exclusive_group()
    mode.add_argument("--until-clear", action="store_true",
                      help="hold while the finding stays present, not --ttl-min: "
                           "for a wait only the operator ends (his prompt in the "
                           "peer's pane, an outstanding arm). Dies when dead-man "
                           "checks see the finding gone, or when no check has "
                           "watched it for 5 minutes")
    mode.add_argument("--revoke", action="store_true",
                      help="cancel every ack recorded for this finding (an ack "
                           "given before you read the pane)")

    c = sub.add_parser("check", help="sweep the pair, escalate any unacked stall past threshold")
    c.add_argument("--for", dest="for_seat", required=True, metavar="SEAT",
                   help="scope to SEAT's pair (required — the dead-man is per-pair)")
    c.add_argument("--escalate-min", type=int, default=DEFAULT_ESCALATE_MIN,
                   help="page a stall still unacked this many minutes after a "
                        "sweep first saw it (the supervisor's ack window)")
    c.add_argument("--evidence-fresh-min", type=int, default=DEFAULT_EVIDENCE_FRESH_MIN,
                   help="a dark-session whose seat wrote its transcript within this "
                        "many minutes is alive, not dark, and does not page (#190). "
                        "No readable evidence still pages.")

    ns = p.parse_args(argv)
    seat_env = os.environ.get("MAW_NET_NAME") or os.environ.get("MAW_BRIDGE_ROLE") or ""

    if ns.cmd == "ack":
        seat = (ns.seat or seat_env or "unknown").strip()
        if ns.revoke:
            print(revoke(ns.finding_id, seat))
            return 0
        print(ack(ns.finding_id, seat, ttl_min=ns.ttl_min, until_clear=ns.until_clear))
        return 0

    # check
    findings = findings_for_pair(sweep(), ns.for_seat)
    escalations = escalate(findings, ns.for_seat, escalate_min=ns.escalate_min,
                           evidence_fresh_min=ns.evidence_fresh_min,
                           subjects=pair_subjects(ns.for_seat))
    if not escalations:
        return 0
    for e in escalations:
        print(f"[deadman] ESCALATED {e['finding_id']} (unacked {e['age_min']}min) "
              f"— {e['method']}; the agent must PushNotification the operator")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
