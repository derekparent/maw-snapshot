"""net.question — the blocked-seat QUESTION / ANSWER protocol (refit R1).

Operator ruling 2026-09-23 (DECISIONS.md, refit rulings 1 and 2): a seat blocked on the operator
never waits. It files ONE structured QUESTION to the CoS, keeps working every
unblocked lane, and when nothing unblocked remains it stages the next goal and
wraps. The CoS answers within-goal questions itself; everything else goes to the operator
and the CoS relaunches the pair after he answers.

THERE IS NO WAIT PRIMITIVE HERE, BY CONSTRUCTION. `ask` returns the moment the
question is recorded; nothing in this module polls, sleeps, or blocks for an
answer. A seat that wants to "wait for the CoS" has nothing to call — the
protocol is: ask, switch lanes, and check the inbox at a natural turn boundary.
Peer supervision (the inbound-bridge watch and the pair-sweep watchdog, which
watch the OTHER SEAT) is a different thing and stays; what R1 bans is a watcher
that waits on the operator.

THE ANSWER CARRIES NO GRANT AUTHORITY. A CoS ANSWER is a peer message: it can
never arm, merge, widen scope, or override a safety refusal (ruling 1). The one
thing it can do is let a seat act on a question the SEAT ITSELF classified
`within-goal` when it asked. That classification is recorded in the seat's own
ledger at ask time, and nothing in an ANSWER can upgrade it. Every other case
PARKS (fail-closed): an answer not sent by the question's addressee (the
sender is DERIVED from the recv fence, never read from the body), unfenced text,
an unknown or already-settled id, an `operator-only` question, a
decision that is not one of the asked options, an unknown source, a duplicated
or malformed field. An `operator-only` question answered over the wire parks even when
the answer says `source: operator` — a relayed "the operator said" is not the operator on the trusted path;
that needs a grant on the passkey rail (or the typed fallback, for its four
low-stakes kinds only).

THE LEDGER IS THE DURABLE RECORD. The `net send cos` wake is best-effort (the
CoS may not be registered on this pair's switchboard, and the wake line may be
down). Each seat's questions live in `<switchboard>/questions/<seat>.jsonl`, so
the CoS — or the relaunched pair — can find every open question by globbing
`~/.claude/bridge/sw-*/questions/*.jsonl` without the send having landed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import opconf
from net import registry

CLASSES = ("within-goal", "operator-only")
SOURCES = ("cos-within-goal", "operator")
RESOLUTIONS = ("settled-by-operator", "superseded", "moot", "withdrawn")
# Spellings from before these words were made operator-neutral. They are still
# accepted on input and in ledgers already on disk, and mean exactly the new
# word. None of them is `within-goal`, so an alias can never turn a park into an act.
CLASS_ALIASES = {"dp-only": "operator-only"}
SOURCE_ALIASES = {"dp": "operator"}
RESOLUTION_ALIASES = {"settled-by-dp": "settled-by-operator"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ANSWER_FIELDS = ("id", "decision", "source")
_WIRE_SUBHEADER_KEYS = ("to", "push", "bridge_turn")

Sender = Callable[[str, str, str], "tuple[bool, str]"]


class QuestionError(ValueError):
    pass


def ledger_path(seat: str) -> Path:
    return registry.net_dir() / "questions" / f"{seat}.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _one_line(label: str, value: str) -> str:
    v = value.strip()
    if not v:
        raise QuestionError(f"{label} is empty")
    if any(c in v for c in "\r\n\x00|"):
        raise QuestionError(f"{label} must be one line with no '|' ({v[:40]!r})")
    return v


def _read(seat: str, path: "Path | None" = None) -> list[dict]:
    p = path or ledger_path(seat)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _append(seat: str, row: dict, path: "Path | None" = None) -> None:
    p = path or ledger_path(seat)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _well_formed_ask(row: dict) -> bool:
    strs = ("id", "seat", "class", "question", "default", "to", "ts")
    if not all(isinstance(row.get(k), str) for k in strs):
        return False
    return all(isinstance(row.get(k), list) and all(isinstance(x, str) for x in row[k])
               for k in ("options", "blocks"))


def state(seat: str, path: "Path | None" = None) -> dict[str, dict]:
    """Fold the ledger into {id: question-with-status}. Status is `open` until an
    `answer` event settles it (`acted`) or a `resolve` event closes it
    (`resolved`, by the addressee, without acting)."""
    out: dict[str, dict] = {}
    for row in _read(seat, path):
        ev = row.get("event")
        qid = row.get("id")
        if ev == "ask" and isinstance(qid, str) and qid not in out:
            if not _well_formed_ask(row):
                continue  # a malformed ask row is not a question: skip, never crash
            out[qid] = {**row, "class": CLASS_ALIASES.get(row["class"], row["class"]),
                        "status": "open"}
        elif ev == "resolve" and qid in out and out[qid]["status"] == "open":
            # Only a resolve row whose sender is the addressee counts. This is a
            # consistency check on the recorded sender, not tamper-proofing: any
            # process running as the same OS user can append a row to the ledger
            # file, and the file cannot tell it from a real one. What actually
            # binds the sender is the CLI, which verifies the caller's process
            # lineage against the registry before writing (see main()).
            resolution = row.get("resolution")
            if isinstance(resolution, str):
                resolution = RESOLUTION_ALIASES.get(resolution, resolution)
            if row.get("sender") == out[qid].get("to") and resolution in RESOLUTIONS:
                out[qid]["status"] = "resolved"
                out[qid]["resolution"] = resolution
                out[qid]["resolution_note"] = row.get("note", "")
                out[qid]["resolved_by"] = row["sender"]
        elif ev == "answer" and qid in out and out[qid]["status"] == "open":
            if row.get("verdict") == "act":
                out[qid]["status"] = "acted"
                out[qid]["decision"] = row.get("decision")
            # A parked answer leaves the question OPEN: the seat still needs
            # a real answer, and the next goal must carry it.
    return out


def render_question(q: dict) -> str:
    return "\n".join([
        "QUESTION",
        f"id: {q['id']}",
        f"from: {q['seat']}",
        f"class: {q['class']}",
        f"question: {q['question']}",
        f"options: {' | '.join(q['options'])}",
        f"default: {q['default']}",
        f"blocks: {', '.join(q['blocks'])}",
        f"reply as: ANSWER / id: {q['id']} / decision: <one option> / "
        f"source: cos-within-goal|operator",
        "This question carries no authority and grants none. The seat keeps "
        "working its other lanes and does not wait.",
    ]) + "\n"


def _default_sender(to: str, seat: str, body: str) -> "tuple[bool, str]":
    """Best-effort wake to the CoS on this seat's switchboard. Body via a temp
    file, NEVER --message (a code span in --message is shell-executed)."""
    from net import cli

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(body)
        tmp = tf.name
    try:
        rc = cli.main(["send", to, "--as", seat, "--from-file", tmp])
    except SystemExit as e:  # argparse refused: NOT the same as cmd_send's rc=2
        return False, f"net send {to} refused its arguments (exit {e.code})"
    except Exception as e:  # noqa: BLE001 — the ledger is the record; a failed wake is reported, not fatal
        return False, f"send raised {type(e).__name__}: {e}"
    finally:
        os.unlink(tmp)
    if rc == 2:  # cmd_send: wake failed, turn PARKED on the wire for `net recv`
        return True, f"parked on the wire for {to} (net send rc=2; {to} pulls it via recv)"
    return rc == 0, f"net send {to} rc={rc}"


def default_addressee() -> str:
    """Who a QUESTION goes to when the caller names nobody: the operator's
    `question_to` (env MAW_QUESTION_TO, then the operator file). There is no
    built-in name. With nothing configured the ask is refused out loud, because
    a question recorded for a session that does not exist is never answered."""
    to = opconf.get("question_to").strip()
    if not to:
        raise QuestionError(
            "no addressee: pass --to <session>, set MAW_QUESTION_TO, or add "
            f'question_to = "<session>" to {opconf.config_path()}')
    return to


def ask(seat: str, qid: str, question: str, options: list[str], default: str,
        blocks: list[str], klass: str, to: "str | None" = None,
        sender: Sender | None = None) -> "tuple[dict, bool, str]":
    """Record a QUESTION and fire a best-effort wake. Returns immediately."""
    to = to or default_addressee()
    if not ID_RE.match(qid or ""):
        raise QuestionError(f"id {qid!r} must match {ID_RE.pattern}")
    klass = CLASS_ALIASES.get(klass, klass)
    if klass not in CLASSES:
        raise QuestionError(f"class must be one of {CLASSES}")
    opts = [_one_line("option", o) for o in options]
    if len(opts) < 2 or len(set(opts)) != len(opts):
        raise QuestionError("need at least two distinct options")
    default = _one_line("default", default)
    if default not in opts:
        raise QuestionError(f"default {default!r} is not one of the options")
    lanes = [_one_line("blocks", b) for b in blocks]
    if not lanes:
        raise QuestionError("name the lane(s) this question blocks")
    if qid in state(seat):
        raise QuestionError(f"question id {qid!r} already used by {seat}")
    q = {
        "event": "ask", "ts": _now(), "id": qid, "seat": seat, "class": klass,
        "question": _one_line("question", question), "options": opts,
        "default": default, "blocks": lanes, "to": to,
    }
    _append(seat, q)
    delivered, how = (sender or _default_sender)(to, seat, render_question(q))
    return q, delivered, how


def parse_answer(text: str) -> dict[str, str]:
    """Strict parse of ONE turn's ANSWER as `net recv` prints it, fence included.

    The SENDER is derived, never trusted from the body: the text must open with
    the NET MSG fence, and the fence header's `from:` line (written by recv from
    the lineage-pinned sender record, not by the peer) names who sent it. A
    self-declared `source:` field says nothing about who is speaking. Unfenced
    text has no derived sender and is refused. The first non-blank body line
    after the header must be `ANSWER`, so an ANSWER quoted mid-message ("the cos
    would say: ANSWER ...") does not count. Each field appears exactly once.
    """
    from net.transport import NET_MSG_BEGIN

    lines = [ln.strip() for ln in text.splitlines()]
    nonblank = [n for n, ln in enumerate(lines) if ln]
    if not nonblank or lines[nonblank[0]] != NET_MSG_BEGIN:
        raise QuestionError("not a fenced NET MSG: pass `net recv` output "
                            "verbatim (unfenced text has no derived sender)")
    begin = nonblank[0]
    try:
        sep = lines.index("---", begin + 1)
    except ValueError:
        raise QuestionError("fence header has no `---` end") from None
    sender = ""
    for ln in lines[begin + 1:sep]:
        if ln.startswith("from:"):
            parts = ln[len("from:"):].split()
            sender = parts[0] if parts else ""
            break
    if not sender:
        raise QuestionError("fence header has no `from:` line")
    body = [n for n in range(sep + 1, len(lines)) if lines[n]]
    addressed_to = ""
    if body and lines[body[0]].startswith("to:"):
        # A wire-pull turn carries the sending side's audit sub-header (wire.py
        # `record_send`): `to:`, `push:`, optional `bridge_turn:`, then `---`.
        # Written by the lineage-pinned send, not the peer's body. Parse it
        # structurally; any other key inside it is malformed.
        try:
            sub_end = lines.index("---", body[0])
        except ValueError:
            raise QuestionError("wire sub-header has no `---` end") from None
        for ln in lines[body[0]:sub_end]:
            key, _, val = ln.partition(":")
            if key not in _WIRE_SUBHEADER_KEYS:
                raise QuestionError(f"unexpected wire sub-header line {ln[:40]!r}")
            if key == "to":
                addressed_to = val.strip()
        body = [n for n in range(sub_end + 1, len(lines)) if lines[n]]
    if not body or lines[body[0]] != "ANSWER":
        raise QuestionError("the first body line is not `ANSWER`")
    i = body[0]
    fields: dict[str, str] = {"sender": sender, "addressed_to": addressed_to}
    for ln in lines[i + 1:]:
        if not ln or ln.startswith("--- NET MSG END"):
            break
        key, sep, val = ln.partition(":")
        key = key.strip().lower()
        if not sep or key not in ANSWER_FIELDS:
            continue
        if key in fields:
            raise QuestionError(f"field {key!r} appears twice")
        fields[key] = val.strip()
    missing = [k for k in ANSWER_FIELDS if not fields.get(k)]
    if missing:
        raise QuestionError(f"missing field(s): {', '.join(missing)}")
    return fields


def decide(seat: str, text: str) -> "tuple[str, str, dict]":
    """Judge an ANSWER against the seat's own ledger. Returns (verdict, reason,
    fields); verdict is `act` or `park`. Records the verdict. Fail-closed:
    anything short of a clean within-goal match parks."""
    try:
        fields = parse_answer(text)
    except QuestionError as e:
        return "park", f"malformed ANSWER: {e}", {}
    qid, decision, source = fields["id"], fields["decision"], fields["source"]
    source = SOURCE_ALIASES.get(source, source)
    q = state(seat).get(qid)
    if q is None:
        verdict, reason = "park", f"no question {qid!r} in {seat}'s ledger"
    elif fields["addressed_to"] and fields["addressed_to"] != seat:
        verdict, reason = "park", (
            f"turn is addressed to {fields['addressed_to']!r}, not {seat!r}")
    elif fields["sender"] != q["to"]:
        verdict, reason = "park", (
            f"answered by {fields['sender']!r}, but the question went to "
            f"{q['to']!r}: only the addressee may settle it")
    elif q["status"] != "open":
        verdict, reason = "park", f"question {qid!r} is already {q['status']}"
    elif source not in SOURCES:
        verdict, reason = "park", f"unknown source {source!r}"
    elif decision not in q["options"]:
        verdict, reason = "park", f"decision {decision!r} is not an asked option"
    elif q["class"] != "within-goal":
        verdict, reason = "park", (
            "operator-only question: a relayed answer is advice, not the operator on the trusted "
            "path — it needs a grant (passkey rail) or the operator in this pane")
    else:
        verdict, reason = "act", (
            f"within-goal question, decision {decision!r} is an asked option")
    if q is not None:
        _append(seat, {"event": "answer", "ts": _now(), "id": qid,
                       "sender": fields["sender"],
                       "decision": decision, "source": source,
                       "verdict": verdict, "reason": reason})
    return verdict, reason, fields


def resolve(seat: str, qid: str, sender: str, resolution: str, note: str,
            path: "Path | None" = None) -> dict:
    """The addressee closes a stale-open question WITHOUT acting on it. Appends a
    `resolve` event; carries no grant authority and never yields an `act` verdict
    (decide() on a resolved id parks, and an operator-only question stays operator-only).
    Refused unless `sender` equals the question's `to` (the check decide uses)."""
    resolution = RESOLUTION_ALIASES.get(resolution, resolution)
    if resolution not in RESOLUTIONS:
        raise QuestionError(f"resolution must be one of {RESOLUTIONS}")
    note = _one_line("note", note)
    q = state(seat, path).get(qid)
    if q is None:
        raise QuestionError(f"no question {qid!r} in {seat}'s ledger")
    if sender != q["to"]:
        raise QuestionError(
            f"resolved by {sender!r}, but the question went to {q['to']!r}: "
            f"only the addressee may resolve it")
    if q["status"] != "open":
        raise QuestionError(f"question {qid!r} is already {q['status']}")
    row = {"event": "resolve", "ts": _now(), "id": qid, "sender": sender,
           "resolution": resolution, "note": note}
    _append(seat, row, path)
    return row


def _switchboard_entries(switchboard: Path) -> "list[registry.Entry]":
    """Every readable registry entry on ONE switchboard (torn files skipped)."""
    out = []
    rd = switchboard / "registry"
    if not rd.is_dir():
        return out
    for f in sorted(rd.glob("*.json")):
        try:
            out.append(registry._entry_from_data(json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return out


def ledger_for(seat: str, ledger: "str | None") -> "tuple[Path, Path]":
    """(ledger file, its switchboard dir). --ledger must be exactly
    <switchboard>/questions/<seat>.jsonl; anything else is refused."""
    if not ID_RE.match(seat or ""):
        raise QuestionError(f"seat {seat!r} must match {ID_RE.pattern}")
    if ledger is None:
        p = ledger_path(seat)
    else:
        p = Path(ledger).expanduser().resolve()
        if p.name != f"{seat}.jsonl" or p.parent.name != "questions":
            raise QuestionError(
                f"resolve refused: --ledger must be <switchboard>/questions/{seat}.jsonl "
                f"(got {ledger!r})")
    return p, p.parent.parent


def verify_resolver(seat: str, qid: str, sender: str, ledger: "str | None",
                    runner: "Callable | None" = None) -> Path:
    """Pin the typed --as to process lineage on the LEDGER's own switchboard.

    The caller's verified registry entry there must be named exactly the
    question's `to`, and the caller's pid must own no other entry there (a seat
    that also registered a second entry named after the addressee — or that is
    itself the asking seat — is refused). Returns the ledger path to write."""
    path, switchboard = ledger_for(seat, ledger)
    q = state(seat, path).get(qid)
    if q is None:
        raise QuestionError(f"no question {qid!r} in {seat}'s ledger ({path})")
    if sender != q["to"]:
        raise QuestionError(
            f"resolved by {sender!r}, but the question went to {q['to']!r}: "
            f"only the addressee may resolve it")
    kw = {"runner": runner} if runner else {}
    entries = _switchboard_entries(switchboard)
    entry = next((e for e in entries if e.name == q["to"]), None)
    if entry is None:
        raise QuestionError(
            f"resolve refused: sender {sender!r} is not a registered session on "
            f"the ledger's switchboard ({switchboard})")
    if not registry.verify_caller_is(entry, **kw):
        raise QuestionError(
            f"resolve refused: this process does not belong to session "
            f"{entry.name!r} (registered pid {entry.pid}) — identity "
            f"claims are pinned to process lineage")
    caller_pid, _ = registry.find_claude_ancestor(**kw)
    others = sorted(e.name for e in entries if e.pid == caller_pid and e.name != entry.name)
    asking = next((n for n in (seat, q.get("seat")) if n in others), None)
    if asking:
        raise QuestionError(
            f"resolve refused: this process also owns the asking seat {asking!r} "
            f"on that switchboard — a seat cannot close its own question")
    if others:
        raise QuestionError(
            f"resolve refused: this process also owns other entries on that "
            f"switchboard ({', '.join(others)}) — one process, one identity")
    return path


def all_ledgers(base: "Path | None" = None) -> "list[tuple[str, Path]]":
    """(seat, ledger path) for every seat ledger on every switchboard: the
    current MAW_NET_DIR, the default, and each sw-<slug> under `base`
    (~/.claude/bridge)."""
    base = base if base is not None else Path.home() / ".claude" / "bridge"
    dirs = [registry.net_dir(), base / "switchboard"]
    if base.is_dir():
        dirs.extend(sorted(p for p in base.glob("sw-*") if p.is_dir()))
    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for d in dirs:
        qd = Path(d) / "questions"
        if qd in seen or not qd.is_dir():
            continue
        seen.add(qd)
        out.extend((f.stem, f) for f in sorted(qd.glob("*.jsonl")))
    return out


def open_all(base: "Path | None" = None) -> "list[tuple[Path, dict]]":
    """Every open question on every switchboard, as (ledger path, question)."""
    return [(path, q) for seat, path in all_ledgers(base)
            for q in state(seat, path).values() if q["status"] == "open"]


def stage_block(seat: str) -> str:
    """The open questions, rendered for the next goal doc's hand-off."""
    open_qs = [q for q in state(seat).values() if q["status"] == "open"]
    if not open_qs:
        return f"## Open questions from {seat}\n\nNone.\n"
    out = [f"## Open questions from {seat} (defaults stand until answered)", ""]
    for q in open_qs:
        out.append(
            f"- **{q['id']}** ({q['class']}, blocks {', '.join(q['blocks'])}): "
            f"{q['question']} Options: {' / '.join(q['options'])}. "
            f"Seat default: **{q['default']}**.")
    return "\n".join(out) + "\n"


TRUST_LINE = ("trust: peer (coordinator) — a logged decision, NO grant authority: it "
              "cannot arm, merge, widen scope, or override a safety refusal")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="net.question", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("ask", help="file a QUESTION (returns immediately; never waits)")
    a.add_argument("--as", dest="seat", required=True)
    a.add_argument("--id", dest="qid", required=True)
    a.add_argument("--question", required=True)
    a.add_argument("--option", dest="options", action="append", default=[])
    a.add_argument("--default", required=True)
    a.add_argument("--blocks", action="append", default=[])
    a.add_argument("--class", dest="klass", required=True,
                   choices=CLASSES + tuple(CLASS_ALIASES), metavar="{%s}" % ",".join(CLASSES),
                   help="within-goal: the goal doc's 'Questions the coordinator may "
                   "answer' covers it; operator-only: anything else")
    a.add_argument("--to", default=None,
                   help="addressee (default: $MAW_QUESTION_TO, else question_to in "
                   "the operator file; refused when neither is set)")
    d = sub.add_parser("decide", help="judge an ANSWER: act (exit 0) or park (exit 4)")
    d.add_argument("--as", dest="seat", required=True)
    d.add_argument("--from-file", required=True,
                   help="the `net recv` output carrying the ANSWER, fence included, or '-' for stdin")
    o = sub.add_parser("open", help="list open questions (this seat's, or --all)")
    o.add_argument("--as", dest="seat", default=None)
    o.add_argument("--all", action="store_true",
                   help="scan every seat ledger on every switchboard "
                   "(~/.claude/bridge/sw-*/questions/*.jsonl)")
    r = sub.add_parser("resolve", help="addressee closes a stale-open question "
                       "without acting on it (no authority)")
    r.add_argument("--as", dest="sender", required=True,
                   help="who you are; must equal the question's `to`")
    r.add_argument("--seat", required=True, help="the asking seat (ledger name)")
    r.add_argument("--id", dest="qid", required=True)
    r.add_argument("--resolution", required=True,
                   choices=RESOLUTIONS + tuple(RESOLUTION_ALIASES),
                   metavar="{%s}" % ",".join(RESOLUTIONS))
    r.add_argument("--note", required=True,
                   help='one line: who settled it, where, when')
    r.add_argument("--ledger", default=None,
                   help="ledger file (from `open --all`) when the seat is not on "
                   "the current switchboard")
    s = sub.add_parser("stage", help="render open questions for the next goal doc")
    s.add_argument("--as", dest="seat", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ask":
            q, delivered, how = ask(args.seat, args.qid, args.question, args.options,
                                    args.default, args.blocks, args.klass, args.to)
            print(f"QUESTION {q['id']} recorded in {ledger_path(args.seat)}")
            print(f"wake: {'delivered' if delivered else 'NOT delivered'} ({how}); "
                  f"the ledger is the durable record")
            print(f"now switch lanes — {', '.join(q['blocks'])} stay blocked; "
                  f"do not wait (no watcher, no poll)")
            return 0
        if args.command == "decide":
            text = (sys.stdin.read() if args.from_file == "-"
                    else Path(args.from_file).read_text(encoding="utf-8"))
            verdict, reason, _ = decide(args.seat, text)
            print(f"verdict: {verdict} — {reason}")
            print(TRUST_LINE)
            return 0 if verdict == "act" else 4
        if args.command == "open":
            if args.all:
                rows = open_all()
                for path, q in rows:
                    print(f"{q['seat']}\t{q['id']}\t{q['class']}\tto={q['to']}\t"
                          f"ts={q['ts']}\tblocks={','.join(q['blocks'])}\t"
                          f"default={q['default']}\tledger={path}")
                if not rows:
                    print("no open questions")
                return 0
            if not args.seat:
                raise QuestionError("open needs --as <seat> or --all")
            open_qs = [q for q in state(args.seat).values() if q["status"] == "open"]
            for q in open_qs:
                print(f"{q['id']}\t{q['class']}\tblocks={','.join(q['blocks'])}\t"
                      f"default={q['default']}")
            if not open_qs:
                print("no open questions")
            return 0
        if args.command == "resolve":
            ledger = verify_resolver(args.seat, args.qid, args.sender, args.ledger)
            row = resolve(args.seat, args.qid, args.sender, args.resolution,
                          args.note, ledger)
            print(f"QUESTION {row['id']} resolved ({row['resolution']}) by {row['sender']}")
            print("resolve carries no authority: nothing was acted on, no grant issued")
            return 0
        if args.command == "stage":
            sys.stdout.write(stage_block(args.seat))
            return 0
    except QuestionError as e:
        print(f"net.question: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
