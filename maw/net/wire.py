"""The audit line — every push is recorded as a bridge-format turn.

The bridge stays the work line; the switchboard adds the wake line. To
keep identity + audit unified, wake-line deliveries reuse the bridge's
turn format (frontmatter, sha256 body integrity, flock-protected append)
in switchboard-owned wire files:

    <net_dir>/wire/<to>/from-<sender>.md

The ledger write is unconditional — attempted-and-failed deliveries are
recorded with the same fidelity as successes (outcome in the turn body
header), so a flaky tmux push can never erase the record of what was said.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from bridge.format import (
    Turn,
    _acquire_lock,
    _release_lock,
    append_turn,
    make_turn,
    mark_consumed,
    parse_turns,
)
from net.registry import net_dir, utcnow_iso


def wire_file(to_name: str, from_name: str) -> Path:
    return net_dir() / "wire" / to_name / f"from-{from_name}.md"


def next_turn_id(file: Path) -> int:
    if not file.exists():
        return 1
    last = 0
    for turn in parse_turns(file):
        last = max(last, turn.turn_id)
    return last + 1


def unconsumed(to_name: str) -> list[tuple[Path, Turn]]:
    """All unconsumed wire turns addressed to `to_name`, oldest first.

    This is the pull side of the wire: a push-deaf session (no tmux
    pane) reads its inbound traffic here instead of being woken.
    """
    base = net_dir() / "wire" / to_name
    if not base.exists():
        return []
    out: list[tuple[Path, Turn]] = []
    for file in sorted(base.glob("from-*.md")):
        for turn in parse_turns(file):
            if turn.consumed_at is None:
                out.append((file, turn))
    out.sort(key=lambda ft: ft[1].ts)
    return out


def deliveries(to_name: str) -> list[Turn]:
    """Every wire turn addressed to `to_name` (consumed AND unconsumed), oldest
    first.

    `unconsumed` is the pull side — it drops turns the recipient already read.
    The orphaned-draft sweeper needs the FULL delivery history: a successful
    push (born consumed) is what proves a prior parked draft was later cleared,
    so a consumed-turn-blind reader can't tell a stuck draft from a resolved one.
    """
    base = net_dir() / "wire" / to_name
    if not base.exists():
        return []
    out: list[Turn] = []
    for file in sorted(base.glob("from-*.md")):
        out.extend(parse_turns(file))
    out.sort(key=lambda t: t.ts)
    return out


def consume(file: Path, turn_id: int) -> bool:
    return mark_consumed(file, turn_id)


def sent(from_name: str) -> list[tuple[str, Turn]]:
    """All wire turns SENT BY `from_name`, across every recipient.

    `unconsumed` is the pull side (a peer reads inbound traffic); this is the
    sender side — the consumption RECEIPT the sender lacked. Wire files are
    <net_dir>/wire/<to>/from-<from_name>.md, so finding everything one session
    sent means looking for that filename under every recipient dir. Returned as
    (to_name, turn), oldest first.
    """
    base = net_dir() / "wire"
    if not base.is_dir():
        return []
    out: list[tuple[str, Turn]] = []
    for to_dir in base.iterdir():
        if not to_dir.is_dir():
            continue
        f = to_dir / f"from-{from_name}.md"
        if f.exists():
            for turn in parse_turns(f):
                out.append((to_dir.name, turn))
    out.sort(key=lambda tt: tt[1].ts)
    return out


def refusals_file() -> Path:
    return net_dir() / "wire" / "refusals.jsonl"


def record_refusal(
    *,
    kind: str,
    claimed_sender: str,
    caller_pid: int,
    to_name: str,
    reason: str,
) -> None:
    """Append a refusal event (lineage spoof, precheck hit) to the audit log.

    Refusals are the most security-interesting events on the wake line —
    an impersonation attempt that leaves no trace can only be proven by
    the refuser's say-so. JSONL, flock-protected, one event per line.
    """
    _append_audit_line({
        "ts": utcnow_iso(),
        "kind": kind,
        "claimed_sender": claimed_sender,
        "caller_pid": caller_pid,
        "to": to_name,
        "reason": reason,
    })


def record_literal_waiver(
    *,
    sender: str,
    caller_pid: int,
    to_name: str,
    bridge_turn_id: int,
    body_sha256: str,
    waived_categories: list[str],
) -> None:
    """Audit a `net send --allow-literal` that actually waived the content scan.

    Shares refusals.jsonl (kind "allow-literal") so every precheck decision,
    refused or waived, reads from one ledger. Category NAMES only: the matched
    text is the dangerous literal, and this file is not quarantined.
    """
    _append_audit_line({
        "ts": utcnow_iso(),
        "kind": "allow-literal",
        "claimed_sender": sender,
        "caller_pid": caller_pid,
        "to": to_name,
        "bridge_turn": bridge_turn_id,
        "body_sha256": body_sha256,
        "waived_categories": list(waived_categories),
    })


def _append_audit_line(record: dict) -> None:
    file = refusals_file()
    line = json.dumps(record)
    lock = _acquire_lock(file.parent / ".refusals.lock")
    try:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        os.chmod(file, 0o600)
    finally:
        _release_lock(lock)


def record_delivery(
    *,
    sender_name: str,
    sender_session_id: str,
    to_name: str,
    pushed: bool,
    detail: str,
    body: str,
    bridge_turn_id: int | None = None,
    grant_b64: str = "",
) -> Turn:
    file = wire_file(to_name, sender_name)
    # "push: pasted+enter", never "delivered: yes" — the transport can
    # verify only that send-keys returned 0, not that the peer's TUI
    # submitted the buffer. The ledger must not assert what the wake
    # line cannot know (advisor field report, 2026-06-10).
    #
    # bridge_turn_id (item 5): when the body is durable on the bridge, the
    # wire ledger is the AUDIT trail, not the body's storage — it links to the
    # bridge turn that IS the source of truth so the receipt path can point at
    # the consumed_at the recipient actually set on pull.
    bridge_line = (
        f"bridge_turn: {bridge_turn_id}\n" if bridge_turn_id is not None else ""
    )
    audit_body = (
        f"to: {to_name}\n"
        f"push: {'pasted+enter' if pushed else 'none'} ({detail})\n"
        f"{bridge_line}"
        f"---\n"
        f"{body}"
    )
    # Wire consumed_at semantics:
    #   - bridge-backed send (bridge_turn_id set): the body's real receipt lives
    #     on the bridge turn, so the wire ledger is pure audit — born consumed
    #     so it never re-delivers via the legacy recv path (the bridge is the
    #     recovery path now). The push: header keeps the honest record.
    #   - legacy wire-only send (no bridge turn): unchanged — a pushed turn is
    #     born consumed (the wake already carried it; replaying it re-delivered
    #     turns 6-10 live on 2026-06-11), a parked turn stays unconsumed so the
    #     legacy recv remains the recovery path.
    if bridge_turn_id is not None:
        consumed_at = utcnow_iso()
    else:
        consumed_at = utcnow_iso() if pushed else None
    # Outer lock (.idlock, distinct from append_turn's .lock) makes
    # read-max-id + append atomic; without it two concurrent senders to
    # the same peer can mint the same turn_id.
    id_lock = _acquire_lock(file.parent / ".idlock")
    try:
        turn = make_turn(
            turn_id=next_turn_id(file),
            role=sender_name,
            session_id=sender_session_id,
            body=audit_body,
            consumed_at=consumed_at,
            grant=grant_b64,
        )
        append_turn(file, turn)
    finally:
        _release_lock(id_lock)
    return turn
