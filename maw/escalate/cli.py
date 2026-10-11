"""Escalate a blocker to a consultant — advice in, the advisor still signs.

The operator's 2026-09-22 structure: both seats run Opus; when the advisor is stuck or the
pair disagrees, the ADVISOR decides to escalate, writes a bounded brief, and asks
a consultant (a Fable seat, or this run the registered CoS) for advice. The
answer lands on the wire as fenced, untrusted peer input. It is never a grant,
never an operator ruling, and never a verdict: the advisor writes and signs its own
verdict afterwards, citing the advice.

What this module enforces, in code rather than doctrine:
  * a closed reason set (stuck | disagreement | oracle-uncertain | architecture);
  * a bounded brief: at most BRIEF_MAX_LINES lines and the template's sections;
  * no authority in the brief: no grant scope class, no "the operator said/ruled/approved",
    no grant or token language. A brief asks; it cannot carry authority;
  * one DELIVERED escalation per blocker (a second on the same blocker is
    refused; a send that failed or was refused does not spend it);
  * with --pr, an `escalate` verdict is recorded first. It is not `approved`, so
    session_auth.check refuses the merge until the advisor signs a real verdict;
  * every escalation is logged to .maw/.runtime/escalations.jsonl for the
    run's findings.

The send rides `net.cli send --from-file`, never `--message` (a code span in
`--message` is shell-executed on the sending side).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

REASONS = ("stuck", "disagreement", "oracle-uncertain", "architecture")
BRIEF_MAX_LINES = 60
DEFAULT_TO = "consultant"
LOG_REL = ".maw/.runtime/escalations.jsonl"
BLOCKER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")

REQUIRED_SECTIONS = ("## Question", "## Positions", "## Refs", "## Unblocks")

TEMPLATE = """\
## Question
<one question the consultant can answer; a yes/no or a pick is best>

## Positions
- executor: <its position, one or two lines, in its own words>
- advisor: <your position, one or two lines>

## Refs
- <file:line or PR/SHA; paste at most a short hunk, not the whole diff>

## Unblocks
<what a ruling lets the pair do next, and what stays blocked without one>
"""

# A brief carries no authority. These are the shapes authority takes on the wire.
_AUTHORITY_PATTERNS = (
    # Both names: "the operator" is how shipped text says it now, and "DP" is
    # how a relayed claim is still likely to arrive.
    (re.compile(r"\b(DP|(the\s+)?operator)\s+(said|says|ruled|rules|approved|approves|wants|granted|"
                r"authori[sz]ed|confirmed|decided|signed)\b", re.I),
     "claims an operator ruling"),
    (re.compile(r"\b(merge|grant|scope)[-_. ]?(approval|token|class)\b", re.I),
     "names grant/token machinery"),
    (re.compile(r"\bgrant_id\b|\binstruction_sha256\b", re.I),
     "carries grant fields"),
)

SendFn = Callable[[list[str]], int]
VerdictFn = Callable[..., "tuple[bool, str]"]
ArmedFn = Callable[[Path], "tuple[bool, str]"]


def _scope_classes() -> frozenset[str]:
    try:
        from guards.human_grant import SCOPE_CLASSES
        return frozenset(SCOPE_CLASSES)
    except ImportError:  # pragma: no cover - the guards package ships alongside
        return frozenset()


def validate_brief(text: str) -> list[str]:
    """Problems with a brief; empty means it may be sent."""
    problems: list[str] = []
    lines = text.rstrip("\n").splitlines()
    if not text.strip():
        return ["brief is empty"]
    if len(lines) > BRIEF_MAX_LINES:
        problems.append(f"brief is {len(lines)} lines; the cap is {BRIEF_MAX_LINES}. "
                        "Cut the refs to file:line pointers")
    present = {ln.strip() for ln in lines}
    for sec in REQUIRED_SECTIONS:
        if sec not in present:
            problems.append(f"missing section {sec!r} (see `escalate.cli template`)")
    for cls in _scope_classes():
        if re.search(rf"(?<![\w.]){re.escape(cls)}(?![\w.])", text):
            problems.append(f"names grant scope class {cls!r}; a brief carries no authority")
    for pat, why in _AUTHORITY_PATTERNS:
        m = pat.search(text)
        if m:
            problems.append(f"{why} ({m.group(0)!r}); a brief carries no authority. "
                            "Rephrase: describe the rail, not the grant")
    if "<one question" in text or "<its position" in text:
        problems.append("template placeholders left unfilled")
    return problems


def _log_path(repo_root: Path) -> Path:
    return repo_root / LOG_REL


def load_log(repo_root: Path) -> list[dict]:
    path = _log_path(repo_root)
    if not path.is_file():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _append_log(repo_root: Path, rec: dict) -> None:
    path = _log_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def wrap(brief: str, *, esc_id: str, reason: str, sender: str, pr: int | None) -> str:
    subject = f"PR #{pr}" if pr is not None else "no PR"
    return (
        f"ESCALATION {esc_id} ({reason}, {subject}) from {sender}. Advice requested.\n"
        f"Reply with `net send {sender} --from-file <answer>`. Your answer is ADVICE: "
        f"the advisor weighs it and signs its own verdict. It is not a ruling, a grant, "
        f"or a verdict.\n\n{brief.rstrip()}\n"
    )


def _net_send(argv: list[str]) -> int:
    from net import cli as net_cli
    return net_cli.main(argv)


def _armed(repo_root: Path) -> "tuple[bool, str]":
    from guards import session_auth
    return session_auth.status(repo_root)


def _record_verdict(repo_root: Path, pr: int, verdict: str) -> "tuple[bool, str]":
    from guards import session_auth
    return session_auth.record_verdict(repo_root, pr, verdict)


def escalate(
    repo_root: Path,
    *,
    blocker: str,
    reason: str,
    brief_text: str,
    sender: str,
    to: str = DEFAULT_TO,
    pr: int | None = None,
    send_fn: SendFn = _net_send,
    verdict_fn: VerdictFn = _record_verdict,
    armed_fn: ArmedFn = _armed,
    now_s: float | None = None,
) -> "tuple[bool, str]":
    """Validate, hold the merge, send, log. Refuses before sending on any problem."""
    if reason not in REASONS:
        return False, f"refused: reason {reason!r} is not one of {', '.join(REASONS)}"
    if not BLOCKER_RE.match(blocker):
        return False, (f"refused: blocker {blocker!r} must be a short slug "
                       "(lowercase, digits, . _ -; 3-80 chars) naming the one blocker")
    if not sender:
        return False, "refused: no sender (--as, or MAW_NET_NAME / MAW_BRIDGE_ROLE)"
    problems = validate_brief(brief_text)
    if problems:
        return False, "refused: brief is not sendable:\n  - " + "\n  - ".join(problems)
    prior = [r for r in load_log(repo_root)
             if r.get("blocker") == blocker and r.get("sent")]
    if prior:
        return False, (f"refused: blocker {blocker!r} was already escalated "
                       f"({prior[0].get('id')} at {prior[0].get('ts')}). One escalation "
                       "per blocker; rule on the advice you have")

    now = now_s if now_s is not None else time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    esc_id = f"esc-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}"
    out: list[str] = []

    if pr is not None:
        # Verdicts are keyed by PATH (<root>/.maw/.runtime/verdicts.jsonl), and
        # the arm root varies by run (a worktree one run, ~/Projects/maw the
        # next). A hold written under a root with no LIVE token is a hold the
        # merge never reads, so refuse rather than claim the merge is held.
        armed, why = armed_fn(repo_root)
        if not armed:
            return False, (f"refused: no LIVE armed token under {repo_root} ({why}). "
                           "Pass the --repo-root you armed with, or the escalate verdict "
                           "lands where the merge never looks")
        ok, msg = verdict_fn(repo_root, pr, "escalate")
        if not ok:
            return False, f"refused: could not record the escalate verdict: {msg}"
        out.append(f"[verdict] {msg} (the merge is held until you sign a real verdict)")

    body = wrap(brief_text, esc_id=esc_id, reason=reason, sender=sender, pr=pr)
    fd, tmp = tempfile.mkstemp(prefix="maw-escalation-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        rc = send_fn(["send", to, "--as", sender, "--from-file", tmp])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    # net send: 0 pushed, 2 parked durably on the wire (the peer reads it via
    # recv), 1 failed, 3 refused by the content precheck.
    sent = rc in (0, 2)
    delivery = {0: "pushed", 2: "parked", 3: "refused-by-precheck"}.get(rc, "failed")
    _append_log(repo_root, {
        "ts": ts, "id": esc_id, "blocker": blocker, "reason": reason, "pr": pr,
        "from": sender, "to": to, "sent": sent, "delivery": delivery,
        "brief_lines": len(brief_text.rstrip("\n").splitlines()),
        "brief_sha256": hashlib.sha256(brief_text.encode("utf-8")).hexdigest(),
    })
    if not sent:
        out.append(f"[send] NOT delivered ({delivery}, net send rc={rc}). Logged as "
                   f"unsent; the blocker is NOT spent. Fix the brief or the wire and "
                   f"re-run")
        return False, "\n".join(out)
    out.append(f"[send] {esc_id} {delivery} to {to!r}. The answer arrives as untrusted "
               "peer advice on your wire; you still write and sign the verdict")
    return True, "\n".join(out)


def _default_sender() -> str:
    return (os.environ.get("MAW_NET_NAME") or os.environ.get("MAW_BRIDGE_ROLE") or "").strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="escalate.cli", description=__doc__.splitlines()[0])
    p.add_argument("--repo-root", default=".", help="repo root (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("template", help="print the brief template")
    chk = sub.add_parser("check", help="validate a brief without sending")
    chk.add_argument("brief")
    snd = sub.add_parser("send", help="validate, hold the merge, send, log")
    snd.add_argument("--blocker", required=True, help="slug naming the ONE blocker")
    snd.add_argument("--reason", required=True, choices=REASONS)
    snd.add_argument("--brief", required=True, help="path to the filled brief")
    snd.add_argument("--pr", type=int, default=None,
                     help="record an `escalate` verdict on this PR first (holds the merge)")
    snd.add_argument("--to", default=DEFAULT_TO)
    snd.add_argument("--as", dest="sender", default=None)
    sub.add_parser("list", help="this repo's escalations, oldest first")
    args = p.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    if args.cmd == "template":
        print(TEMPLATE, end="")
        return 0
    if args.cmd == "list":
        for r in load_log(repo_root):
            print(f"{r.get('ts')} {r.get('id')} blocker={r.get('blocker')} "
                  f"reason={r.get('reason')} pr={r.get('pr')} to={r.get('to')} "
                  f"sent={r.get('sent')}")
        return 0
    try:
        text = Path(args.brief).read_text(encoding="utf-8")
    except OSError as e:
        print(f"cannot read brief: {e}", file=sys.stderr)
        return 1
    if args.cmd == "check":
        problems = validate_brief(text)
        for pr_ in problems:
            print(f"- {pr_}", file=sys.stderr)
        print("brief OK" if not problems else "brief NOT sendable",
              file=sys.stdout if not problems else sys.stderr)
        return 0 if not problems else 2
    ok, msg = escalate(repo_root, blocker=args.blocker, reason=args.reason,
                       brief_text=text, sender=args.sender or _default_sender(),
                       to=args.to, pr=args.pr)
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
