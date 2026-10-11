"""guards.typed_approval — the operator's TYPED backup approval (refit R3c).

Operator ruling 2026-09-23 (refit ruling 4): a typed approval in the CoS chat
(terminal or Remote Control) is accepted as a last-resort fallback for low-stakes
kinds only: `goal.stage`, `prompt.relay`, `unblock.instruct`, and `scope.amend`
within the same repo. It is short-lived and logged, and the digest shows it.
`session.arm` and `merge.approval` stay passkey only. An emailed code was
rejected, because the agents have mail access.

WHAT THIS IS, WITHOUT SPIN. A typed approval carries NO signature. It is the
CoS's RECORD that the operator typed words in its chat. Nothing here can prove the operator typed
them: any process running as the operator can write the same record. It is a confusion
gate over a logged receipt, the same honesty session_auth states about its token
file. So the seat renders it `trust: OPERATOR-TYPED`, never `OPERATOR-AUTHORIZED`, and the
kinds it may carry are the four low-stakes ones the operator named, no others.

WHO RECORDED IT IS DERIVED, NEVER ASSERTED (advisor F1 on #238). Ruling 4 is "a
typed approval IN THE COS CHAT": the CoS's record, no seat's. record() derives the
recorder from process lineage (the claude ancestor's pid, matched against live
switchboard entries on every net) and REFUSES unless that process is registered
with role `cos` and holds no seat role (advisor / executor / consultant). The
derived recorder rides the ISSUED ledger line, and verify() checks it there. A
seat that wanted to widen its own armed scope, or to dress a peer relay up as
The operator's word, cannot record one. Every refused record is audited in the ledger.

The armed seat cannot launder itself into a recorder either: session_auth
refuses a typed amend whose issued line's recorder_pid is the armed token's pid.
So self-widening needs a SECOND claude process, not four commands.

THE RESIDUAL, UNSPUN: a process that hand-writes BOTH the grant ledger and a
switchboard registry entry can still forge a typed approval. There is no fix for
that from inside the same user account; like session_auth's token file, this is
a confusion gate, not a lock.

What it does bind, like a grant: the literal instruction (instruction_sha256),
the whole payload (the issued line stores its canonical digest, so a receipt
cannot be re-presented as a different kind, target or session), one
scope_class, a short TTL, the CoS's issued record (the allow-list), and
single use (the shared grant ledger's atomic consume). A `scope.amend` also
carries the same `session` object a passkey amend does, so session_auth derives
and matches the repo, seat, session and scopes exactly as it does for a grant.
"""
from __future__ import annotations

import hashlib
import sys
import time
import uuid
from collections.abc import Collection

from guards import human_grant as hg

# Exactly the operator's four kinds. session.arm and merge.approval are refused by name.
TYPED_CLASSES = frozenset({"goal.stage", "prompt.relay", "unblock.instruct", "scope.amend"})
PASSKEY_ONLY = frozenset({"session.arm", "merge.approval", hg.BATCH_CLASS})
CHANNELS = ("cos-terminal", "remote-control")
MAX_TTL_SECONDS = hg.MAX_TTL_SECONDS
_REQUIRED = ("grant_id", "instruction_sha256", "scope_class", "target", "issued_at",
             "ttl_seconds", "nonce", "typed")
_TYPED_KEYS = {"dp_text", "channel", "recorded_by"}


def is_typed(record: object) -> bool:
    return isinstance(record, dict) and "typed" in record


def class_refusal(scope_class: object) -> "str | None":
    if not isinstance(scope_class, str):
        return f"scope_class must be a string, not {type(scope_class).__name__}"
    if scope_class in PASSKEY_ONLY:
        return f"{scope_class} is passkey only: a typed approval can never carry it"
    if scope_class not in TYPED_CLASSES:
        return f"{scope_class!r} is not a typed-approval kind ({sorted(TYPED_CLASSES)})"
    return None


RECORDER_ROLE = "cos"


def _registry_entries() -> "list[object]":
    """Every entry on every switchboard: this process's MAW_NET_DIR plus the
    default and each sw-<slug> under ~/.claude/bridge."""
    import json as _json
    from pathlib import Path
    from net import registry

    base = Path.home() / ".claude" / "bridge"
    dirs = [registry.net_dir(), base / "switchboard"]
    if base.is_dir():
        dirs.extend(sorted(base.glob("sw-*")))
    out, seen = [], set()
    for d in dirs:
        rd = Path(d) / "registry"
        if rd in seen or not rd.is_dir():
            continue
        seen.add(rd)
        for f in sorted(rd.glob("*.json")):
            try:
                out.append(registry._entry_from_data(_json.loads(f.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
    return out


def derive_recorder() -> "tuple[str, int]":
    """(name, pid) of the CoS session this process belongs to, derived from
    process lineage. Raises ValueError when the caller is a seat, or is not a
    registered, live `cos` session."""
    from net import registry, seatcheck

    pid, _tty = registry.find_claude_ancestor()
    mine = [e for e in _registry_entries()
            if getattr(e, "pid", None) == pid and registry.pid_alive(pid)]
    seats = sorted({e.role for e in mine if e.role in seatcheck.ROLES})
    if seats:
        raise ValueError(f"a {'/'.join(seats)} seat cannot record a typed approval: "
                         f"ruling 4 is the coordinator's record of the operator's words, not a seat's")
    cos = sorted(e.name for e in mine if e.role == RECORDER_ROLE)
    if not cos:
        raise ValueError(f"this process (pid {pid}) is not a registered live "
                         f"{RECORDER_ROLE!r} session; only the coordinator records a typed approval")
    return cos[0], pid


def record(
    *, instruction: str, scope_class: str, target: str, dp_text: str, channel: str,
    ttl_seconds: int, now_iso: str, session: "dict | None" = None, write: bool = True,
) -> dict:
    """The CoS side: build a typed approval and ledger it as issued. Raises
    ValueError on anything the seat would refuse, before it is recorded. The
    recorder is DERIVED (derive_recorder); a refusal of THAT is audited."""
    try:
        recorded_by, recorder_pid = derive_recorder()
    except ValueError as e:
        if write:
            hg._append_event({"event": "record-refused", "carrier": "typed",
                              "scope_class": scope_class, "reason": str(e), "ts": now_iso})
        raise
    why = class_refusal(scope_class)
    if why:
        raise ValueError(why)
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS} (an emailed code was rejected)")
    if not isinstance(instruction, str) or not instruction:
        raise ValueError("instruction must be a non-empty string")
    if not isinstance(dp_text, str) or not dp_text.strip():
        raise ValueError("dp_text (the operator's verbatim typed words) is required")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) \
            or not (0 < ttl_seconds <= MAX_TTL_SECONDS):
        raise ValueError("ttl_seconds out of bounds")
    shape = hg.session_shape_error(scope_class, session)
    if shape:
        raise ValueError(shape)
    rec = {
        "grant_id": f"typed-{uuid.uuid4()}",
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "scope_class": scope_class,
        "target": target,
        "issued_at": now_iso,
        "ttl_seconds": ttl_seconds,
        "nonce": str(uuid.uuid4()),
        "merge": None,
        "session": session,
        "typed": {"dp_text": dp_text, "channel": channel, "recorded_by": recorded_by},
    }
    if write:
        hg._append_event({"event": "issued", "grant_id": rec["grant_id"],
                          "carrier": "typed", "scope_class": scope_class,
                          "channel": channel, "recorded_by": recorded_by,
                          "recorder_role": RECORDER_ROLE, "recorder_pid": recorder_pid,
                          "payload_sha256": payload_digest(rec),
                          "ts": now_iso})
    return rec


def payload_digest(rec: dict) -> str:
    """sha256 of the whole approval in canonical form (the same bytes a signed
    grant's signature covers). The issued line stores it; verify() recomputes it,
    so no field of an issued receipt can change without the receipt failing."""
    return hashlib.sha256(hg.canonical_bytes(rec)).hexdigest()


def _typed_issued_line(grant_id: str) -> "dict | None":
    for e in hg._ledger_events():
        if (e.get("event") == "issued" and e.get("grant_id") == grant_id
                and e.get("carrier") == "typed"):
            return e
    return None


def verify(rec: object, instruction_text: str, now_iso: str, *,
           consumer: "str | None" = None, relayed_by: "str | None" = None,
           receiver: "Collection[str] | None" = None,
           ceremony: bool = False) -> hg.VerifyResult:
    """Fail-closed check of a typed approval against the literal instruction.
    Does NOT consume; callers act only after hg.consume() returns True.

    A session-class approval (scope.amend) verifies only with `ceremony=True`,
    which only guards.session_auth passes: the same seam as hg.verify (R16).

    `consumer` is the seat about to act on it: it may not be its own recorder.
    `relayed_by` is the lineage-pinned sender of the turn that carried it: it must
    BE the recorder (a typed approval travels only from the CoS that recorded it).
    `receiver` is the set of names the receiving seat answers to (derived by the
    delivery point); when given, `target` must be one of them, as for a grant.
    """
    rej = hg._reject
    if not isinstance(rec, dict):
        return rej("typed approval is not an object")
    for k in _REQUIRED:
        if k not in rec:
            return rej(f"missing field: {k}")
    if "sig" in rec or "webauthn" in rec:
        return rej("a typed approval carries no signature; this one does")
    why = class_refusal(rec["scope_class"])
    if why:
        return rej(why)
    if rec["scope_class"] in hg.CEREMONY_CLASSES and not ceremony:
        return rej(f"a {rec['scope_class']} approval verifies only inside the "
                   f"session_auth ceremony")
    typed = rec["typed"]
    if not isinstance(typed, dict) or set(typed) != _TYPED_KEYS:
        return rej(f"typed object must have exactly {sorted(_TYPED_KEYS)}")
    if typed.get("channel") not in CHANNELS:
        return rej("typed approval channel is not a coordinator chat")
    ttl = rec["ttl_seconds"]
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not (0 < ttl <= MAX_TTL_SECONDS):
        return rej("ttl_seconds out of bounds")
    if rec.get("merge") is not None:
        return rej("merge present on a typed approval")
    shape = (hg.session_shape_error(rec["scope_class"], rec.get("session"))
             or hg.batch_shape_error(rec["scope_class"], rec))
    if shape:
        return rej(shape)
    if not isinstance(instruction_text, str) or hashlib.sha256(
            instruction_text.encode("utf-8")).hexdigest() != rec["instruction_sha256"]:
        return rej("instruction hash mismatch")
    issued_e, now_e = hg._epoch(rec["issued_at"]), hg._epoch(now_iso)
    if issued_e is None or now_e is None:
        return rej("unparseable timestamp")
    if now_e + hg._FUTURE_SKEW_SECONDS < issued_e:
        return rej("issued_at is in the future")
    if now_e > issued_e + ttl:
        return rej("typed approval expired")
    gid = str(rec["grant_id"])
    line = _typed_issued_line(gid)
    if line is None:
        return rej("typed approval not in the issued ledger (never recorded by the coordinator)")
    recorder = line.get("recorded_by")
    pid = line.get("recorder_pid")
    if line.get("recorder_role") != RECORDER_ROLE or not recorder \
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return rej("the issued ledger line names no derived coordinator recorder")
    if typed.get("recorded_by") != recorder:
        return rej(f"record says recorded_by={typed.get('recorded_by')!r}, the ledger says {recorder!r}")
    if consumer is not None and consumer == recorder:
        return rej(f"{consumer!r} recorded this typed approval and cannot also act on it")
    if relayed_by is not None and relayed_by != recorder:
        return rej(f"typed approval relayed by {relayed_by!r}, not by its recorder {recorder!r}")
    if receiver is not None and (not isinstance(rec["target"], str)
                                 or rec["target"] not in receiver):
        return rej(f"typed approval target {rec['target']!r} is not this receiver "
                   f"({sorted(receiver)})")
    try:
        digest = payload_digest(rec)
    except (TypeError, ValueError):
        return rej("typed approval is not JSON-serializable")
    if line.get("payload_sha256") != digest:
        return rej("typed approval payload does not match what the coordinator recorded "
                   "(the issued line binds every field)")
    if hg.is_consumed(gid):
        return rej("typed approval already consumed (replay)")
    return hg.VerifyResult(True)


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    import json
    from pathlib import Path

    p = argparse.ArgumentParser(prog="guards.typed_approval",
                                description="coordinator: record the operator's typed approval (R3c)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--scope-class", required=True, choices=sorted(TYPED_CLASSES))
    r.add_argument("--target", required=True)
    r.add_argument("--instruction-file", required=True,
                   help="the exact instruction text (a file, never argv)")
    # --dp-text-file is the flag's first name, kept so existing callers still work.
    r.add_argument("--operator-text-file", "--dp-text-file", dest="operator_text_file",
                   required=True, help="the operator's verbatim typed words")
    r.add_argument("--channel", required=True, choices=CHANNELS)
    r.add_argument("--ttl", type=int, default=600)
    r.add_argument("--session-file", default=None, help="scope.amend: the session object (JSON)")
    r.add_argument("--out", default=None)
    args = p.parse_args(argv)
    try:
        session = (json.loads(Path(args.session_file).read_text(encoding="utf-8"))
                   if args.session_file else None)
        rec = record(
            instruction=Path(args.instruction_file).read_text(encoding="utf-8"),
            scope_class=args.scope_class, target=args.target,
            dp_text=Path(args.operator_text_file).read_text(encoding="utf-8").strip(),
            channel=args.channel, ttl_seconds=args.ttl, session=session,
            now_iso=time.strftime(hg._TS_FMT, time.gmtime()))
    except (OSError, ValueError) as e:
        print(f"typed approval refused: {e}", file=sys.stderr)
        return 1
    out = json.dumps(rec, indent=2)
    if args.out:
        Path(args.out).write_text(out + "\n", encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
