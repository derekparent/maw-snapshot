"""net.waitstate — is a quiet seat WAITING on a known counterpart? (T2, Lane D P2)

WHY THIS EXISTS. `watch.dark_sessions` fires on silence, and a pair is silent by
design most of the time: the executor grounds and then holds for the advisor's
/maw:goal; the executor sends a review request and holds for the verdict; the
advisor asks the operator a question and holds for the answer. Every one of
those was flagged dark, and the dead-man escalated two of them to the operator
in one evening (2026-10-01 ~02:30Z; earlier 2026-09-23 18:48Z and 19:17Z).
An idle seat waiting on a live counterpart is not dark. The counterpart is the
one holding the ball, and if IT goes quiet, its own finding fires.

DERIVED FROM AN ARTIFACT, NEVER FROM THE SEAT'S SAY-SO. A seat cannot declare
itself waiting. The two artifacts read here are things the seat DID, with an
effect outside itself:

  1. The pair bridge's turn order. The seat's newest durable turn is newer than
     every turn its peer has written, the peer is registered and pid-alive, and
     the seat holds no unconsumed inbound turn. So the last word on the wire is
     the seat's, and the next move is the peer's.
  2. An open `net.question` the seat asked (it was sent to the CoS). The next
     move is the operator's (or the CoS's).
  3. An outstanding ARM request (#345). The pair's advisor ran
     `session_auth grant-request session.arm` (batch or single) and no arm has
     landed since. Until the operator arms, the next move is the operator's for
     BOTH seats: the advisor asked, and the executor's confirm-start gate holds
     it. The 1002/1002c runs read the held executor `dark-session` (it held the
     ball by turn order) and the advisor "waiting on executor", 3-4 hand acks a
     run. A token armed at or after the request means the request is answered,
     and the excuse is gone even if that token has since expired.
     The excuse also lapses ARM_WAIT_MAX_MIN (120) after the OLDEST request
     still unanswered, so asking again never restarts the clock. And it never
     covers a pair whose advisor already holds a live token: that pair is
     armed and holds work, so a re-request made mid-run (a token armed BEFORE
     it is not an answer to it) must not turn a dead seat, the advisor
     included, into a waiting one.

Both are bounded to THIS registration (`started_at`): seat names are reused
across runs, and a predecessor's turn or question must not excuse a new seat
that has said nothing.

WHAT IT DOES NOT EXCUSE. A seat holding the ball (the peer spoke last, or an
inbound turn waits unconsumed) is never waiting, however quiet; an unpulled
inbound turn outranks an open question. A seat with no reachable pane (gone,
push-deaf, herdr unreadable) is never waiting: it cannot receive the reply. A seat whose
counterpart is gone (unregistered or pid-dead) is never waiting. A herdr
`working` or `blocked` seat never reaches this module (dark_sessions handles
those first). Every miss reads as NOT waiting, so the dark finding fires: this
module can only suppress on positive evidence, never on a failed read.
"""
from __future__ import annotations

import calendar
import glob
import json
import re
import time
from pathlib import Path

from net import bridgelink
from net.registry import Entry, pid_alive

# How long an outstanding arm request excuses a quiet pair (#345). Counted
# from the oldest request still unanswered; after it, the pair reads dark again.
ARM_WAIT_MAX_MIN = 120


def _epoch(ts: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def _since_registration(ts: str, entry: Entry) -> bool:
    """True if `ts` is at or after this seat registered. An unparseable either
    side is False (no evidence)."""
    t, start = _epoch(ts), _epoch(entry.started_at)
    return t is not None and start is not None and t >= start


def _peer(entry: Entry, entries: list[Entry]) -> Entry | None:
    """The live bridge peer sharing this seat's slug, or None."""
    if entry.role not in bridgelink.BRIDGE_ROLES:
        return None
    want = bridgelink._other_role(entry.role)
    try:
        slug = bridgelink._shared_slug(entry)
    except Exception:  # noqa: BLE001 — no slug, no pair, no excuse
        return None
    for e in entries:
        if e.name == entry.name or e.role != want:
            continue
        try:
            if bridgelink._shared_slug(e) != slug:
                continue
        except Exception:  # noqa: BLE001
            continue
        if pid_alive(e.pid):
            return e
    return None


def waiting_on_peer(entry: Entry, entries: list[Entry]) -> str | None:
    """Why the seat is waiting on its peer, or None. See the module docstring (1)."""
    peer = _peer(entry, entries)
    if peer is None:
        return None
    try:
        mine = bridgelink.sent_durable(entry)
        theirs = bridgelink.sent_durable(peer)
    except Exception:  # noqa: BLE001 — an unreadable bridge is no evidence
        return None
    if not mine:
        return None
    last = max(mine, key=lambda t: (_epoch(t.ts) or 0.0, t.turn_id))
    last_s = _epoch(last.ts)
    if last_s is None or not _since_registration(last.ts, entry):
        return None
    peer_last = max((_epoch(t.ts) or 0.0 for t in theirs), default=0.0)
    if last_s <= peer_last:
        return None  # the peer spoke last (or at the same second): the seat's move
    return (f"waiting on {peer.name}: its bridge turn {last.turn_id} ({last.ts}) is "
            f"the newest on the pair wire")


def waiting_on_operator(entry: Entry) -> str | None:
    """Why the seat is waiting on the operator, or None. Module docstring (2)."""
    try:
        from net import question
        asked = question.state(entry.name)
    except Exception:  # noqa: BLE001
        return None
    open_qs = [q for q in asked.values()
               if q.get("status") == "open" and _since_registration(str(q.get("ts", "")), entry)]
    if not open_qs:
        return None
    q = max(open_qs, key=lambda q: str(q.get("ts", "")))
    return (f"waiting on {q.get('to') or 'the operator'}: open question "
            f"{q.get('id')} ({q.get('ts')})")


def _advisor_repo(advisor: Entry) -> str:
    """The advisor's `owner/name`, as `grant-request` wrote it into the
    request records, or '' if it cannot be read (then no record matches)."""
    try:
        from guards import session_auth as sa
        return sa._repo_identity(Path(advisor.repo_root))
    except Exception:  # noqa: BLE001
        return ""


def _arm_requests(advisor: Entry, now: float) -> list[float]:
    """Epochs of this advisor's arm requests made during this registration,
    oldest first. Two kinds of record count, both found by the registry
    session id and matched on (session id, repo), so neither a predecessor's
    request, nor a longer id sharing the prefix, nor another repo's seat with
    the same id matches: the arm-wait marker every `grant-request session.arm`
    writes (`<sid>.<key>.arm-wait.json`, which lists every request still
    unanswered), and the batch request record (`<sid>.<key>.json` since #338,
    `<sid>.json` before it; deleted when an arm consumes it). A timestamp in
    the future is not a request."""
    sid = advisor.session_id
    if not sid:
        return []
    repo = _advisor_repo(advisor)
    if not repo:
        return []
    try:
        from guards import human_grant as hg
        base = hg.grants_dir() / "requests"
    except Exception:  # noqa: BLE001
        return []
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:80] or "_"
    try:
        paths = sorted(set(base.glob(f"{glob.escape(safe)}.*.json"))
                       | {base / f"{safe}.json"})
    except OSError:
        return []
    out: set[float] = set()
    for path in paths:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        line = rec.get("line") if isinstance(rec.get("line"), dict) else {}
        if (rec.get("session_id") or line.get("session_id")) != sid:
            continue
        if (rec.get("repo") or line.get("repo")) != repo:
            continue
        stamps = [rec.get("created_at")]
        if isinstance(rec.get("requests"), list):
            stamps += rec["requests"]
        for made in stamps:
            if not isinstance(made, str):
                continue
            at = _epoch(made)
            if at is None or at > now or not _since_registration(made, advisor):
                continue
            out.add(at)
    return sorted(out)


def _token(advisor: Entry) -> "tuple[dict | None, str]":
    """(token, state) for the advisor's worktree: state is 'ok', 'none' (no
    token file) or 'unreadable'."""
    try:
        from guards import session_auth as sa
        data, refusal = sa._load_token(Path(advisor.repo_root))
    except Exception:  # noqa: BLE001
        return None, "unreadable"
    if refusal is not None or data is None:
        return None, "none" if getattr(refusal, "bound", "") == "no-token" else "unreadable"
    return data, "ok"


def _armed_since(advisor: Entry, asked: float) -> bool:
    """True if the advisor's worktree holds a token armed at or after `asked`
    (live or since expired: either way the request was answered). A token that
    cannot be read is NOT an answer."""
    data, _state = _token(advisor)
    if data is None:
        return False
    armed = _epoch(str(data.get("armed_at", "")))
    return armed is not None and armed >= asked


def _holds_live_token(advisor: Entry, now: float) -> bool:
    """True if this advisor session holds an armed token inside its window, or
    the token cannot be read (no proof the pair is unarmed). A predecessor
    session's token left in the worktree is not this pair's arm."""
    data, state = _token(advisor)
    if data is None:
        return state == "unreadable"
    if str(data.get("session_id", "")) != advisor.session_id:
        return False
    try:
        from guards import session_auth as sa
        return sa._time_bounds(data, now) is None
    except Exception:  # noqa: BLE001
        return True


def waiting_on_arm(entry: Entry, entries: list[Entry],
                   now_s: float | None = None) -> str | None:
    """Why the seat is waiting on the operator's ARM, or None. Module
    docstring (3). The advisor is the seat that asks; its live executor peer
    waits on the same arm. Neither is excused while the advisor already holds
    a live token: the pair is then armed and holds work, and a re-request made
    mid-run must not turn a dead seat, advisor or executor, into a waiting one.
    The excuse lapses ARM_WAIT_MAX_MIN after the OLDEST unanswered request of
    this registration: the marker is a plain file any process can write, and
    an arm nobody answers for two hours is a pair to look at, not a wait to
    hide, however often it is asked for again."""
    if entry.role == "advisor":
        advisor = entry
    else:
        advisor = _peer(entry, entries)
        if advisor is None or advisor.role != "advisor":
            return None
    now = now_s if now_s is not None else time.time()
    if _holds_live_token(advisor, now):
        return None
    open_ = [at for at in _arm_requests(advisor, now) if not _armed_since(advisor, at)]
    if not open_:
        return None
    asked = open_[0]
    if not 0 <= now - asked <= ARM_WAIT_MAX_MIN * 60:
        return None  # stale: no operator leaves an arm unanswered this long
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(asked))
    who = "its" if advisor is entry else f"{advisor.name}'s"
    return (f"waiting on the operator's arm: {who} arm request ({when}) is "
            f"outstanding and no arm has landed since")


def holds_unpulled_inbound(entry: Entry) -> bool:
    """True if a durable turn waits for this seat unpulled, or the read fails.
    That turn is the seat's next move, so it gates EVERY wait path: an open
    question elsewhere never excuses a seat sitting on its peer's turn."""
    try:
        return bridgelink.next_inbound(entry) is not None
    except Exception:  # noqa: BLE001 — unreadable inbox: assume it holds the ball
        return True


def wait_reason(entry: Entry, entries: list[Entry],
                now_s: float | None = None) -> str | None:
    """The artifact-derived reason this quiet seat is waiting, or None (= dark)."""
    if holds_unpulled_inbound(entry):
        return None
    return (waiting_on_operator(entry) or waiting_on_arm(entry, entries, now_s)
            or waiting_on_peer(entry, entries))
