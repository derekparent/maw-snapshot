"""net.liveness — proof of life that ticks WITHOUT a prompt submit (#190).

WHY THIS EXISTS. The registry heartbeat (`last_seen`) advances only on
UserPromptSubmit. A seat grinding through a long tool-heavy turn, or woken by a
Monitor while the operator is away, therefore reads heartbeat-stale while fully alive —
and `watch.dark_sessions` fires on exactly that silence. On 2026-07-22 the live
drill escalated `dark-session` for BOTH seats of a working pair and the dead-man
paged the CoS; three parties independently confirmed both seats were working.
That is the false positive this module supplies the evidence to refuse.

THE EVIDENCE. THE SEAT'S OWN transcript file: Claude Code appends to
`~/.claude/projects/<slug>/<session>.jsonl` on every turn AND every tool result,
with no prompt submit involved. So that file's mtime advances while the seat
works and freezes when it wedges — which is the property `last_seen` lacks.

IT MUST BE THE SEAT'S FILE, NOT THE DIRECTORY. A project dir holds a transcript
for EVERY session that ran in that cwd: headless `claude -p` children (the
sanctioned delegation shape on a seat), a rotated predecessor seat, a parallel
session. Taking the newest mtime across the dir means a wedged seat whose
backgrounded child keeps writing reads ALIVE forever — the gate would suppress
exactly the page it exists to send (caught in review on PR #210, where the
executor's own project dir held four transcripts, three written in the same
minute). So the registry pins the seat's transcript at register time and this
module stats THAT file.

The dir-max read survives only as a FALLBACK for an entry with no pinned
transcript (an old transplant, a hand-registered seat). It is weaker evidence and
every consumer is told so via `Evidence.basis`, so a suppression on that basis is
legible in the ledger rather than silently equal to the real thing. The slug
derivation is shared with `bridge.livemodel` (one definition of a project dir).

FAIL-OPEN TOWARD THE PAGE — the opposite of the usual fail-open. Every miss (no
project dir, no transcript, unreadable, unknown seat, unreadable registry)
returns None, meaning NO EVIDENCE, and the caller escalates. This module can
only ever suppress a page on positive proof of life; a broken read must never
silence an alarm.

WHAT IT IS NOT. Transcript mtime proves the session is WRITING, not that it is
making progress on the right thing. It belongs on the silence-triggered
`dark-session` finding only — never on a finding that already carries positive
evidence of a wedge (`stuck-while-working`, `blocked-seat`).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import NamedTuple

from bridge.livemodel import project_dir
from net.registry import Entry, who


class Evidence(NamedTuple):
    """Liveness evidence and WHAT IT RESTS ON. `age_min` is None when there is no
    readable evidence, which every consumer must treat as "escalate"."""

    age_min: float | None
    # "session-transcript" — the seat's own pinned transcript (the real thing)
    # "dir-max-fallback"   — newest transcript in the seat's project dir; weaker,
    #                        a sibling session can satisfy it
    # "none"               — nothing readable
    basis: str


NO_EVIDENCE = Evidence(None, "none")


def transcript_age_min(
    cwd: str,
    now_s: float | None = None,
    projects_root: Path | None = None,
) -> float | None:
    """Minutes since the freshest transcript write for the session in `cwd`.

    None when there is no readable evidence. A future mtime (clock skew, a
    restored file) clamps to 0.0 — skew must read as fresh, never as stale.
    """
    now = now_s if now_s is not None else time.time()
    proj = project_dir(cwd, projects_root)
    try:
        if not proj.is_dir():
            return None
        mtimes = [p.stat().st_mtime for p in proj.glob("*.jsonl")]
    except OSError:
        return None
    if not mtimes:
        return None
    return max(0.0, (now - max(mtimes)) / 60.0)


def pinned_age_min(transcript_path: str, now_s: float | None = None) -> float | None:
    """Minutes since the seat's OWN transcript was written, or None if it is not
    readable. A pinned path that has vanished is NO evidence — never a reason to
    fall back to the dir, which is what let a sibling session vouch for a wedged
    seat."""
    now = now_s if now_s is not None else time.time()
    try:
        mtime = Path(transcript_path).stat().st_mtime
    except OSError:
        return None
    return max(0.0, (now - mtime) / 60.0)


def evidence_for(
    seat: str,
    now_s: float | None = None,
    entries: list[Entry] | None = None,
    projects_root: Path | None = None,
) -> Evidence:
    """Liveness evidence for the named seat, with the basis it rests on.

    Prefers the transcript the registry pinned for this seat. Falls back to the
    newest transcript in the seat's project dir ONLY when no transcript was
    pinned, and says so in the basis.
    """
    try:
        the_entries = entries if entries is not None else who(prune=False)
    except Exception:  # noqa: BLE001 — an unreadable registry is no evidence
        return NO_EVIDENCE
    for entry in the_entries:
        if entry.name != seat:
            continue
        pinned = (entry.transcript_path or "").strip()
        if pinned:
            age = pinned_age_min(pinned, now_s)
            return NO_EVIDENCE if age is None else Evidence(age, "session-transcript")
        age = transcript_age_min(entry.cwd, now_s, projects_root)
        return NO_EVIDENCE if age is None else Evidence(age, "dir-max-fallback")
    return NO_EVIDENCE
