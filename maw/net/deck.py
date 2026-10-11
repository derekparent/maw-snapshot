"""net.deck — a seat's run report to the operator's run page (lane R4).

The Deck is a claude.ai artifact the operator watches. A seat reports its run
there through the `ArtifactData` MODEL TOOL: the seat itself makes the tool
call, so this module cannot write anything. It does two things:

  render  print the exact ArtifactData arguments for one `runs` write, built
          and validated here so the seat never hand-types a payload;
  audit   scan a seat's own transcript for ArtifactData writes to any
          collection other than `runs`, and exit nonzero if it finds one.

THE HARD RULE: A SEAT WRITES ONLY `runs`. Never `cards`, never `signals`. A seat
calls ArtifactData as the operator's account, so a seat's write to `cards` is
indistinguishable from the operator's own tap on the Deck: it would be a forged
decision. `render` has no way to name another collection (there is no flag),
and `audit` is the after-the-fact check that the rule held.

WHEN A SEAT WRITES: at confirm-start (`set`, stage working), after each merge,
on a block (stage blocked, alongside the `net.question ask`), and at wrap
(stage wrapped). Nothing more frequent. The first write is `set`; every later
write is `update` pinned with `--if-version` (the version the previous write
returned), so a concurrent write by the other seat fails loudly instead of
being overwritten.

THE DOC ID RULE (`docid`). One run, one doc, one id both seats use:
  1. a launch brief that names an id wins: `doc id maw-deck-0930` anywhere in
     a line, mid-sentence included; placeholders (`TBD`, `<id>`, a
     `<repo>-...` template) are not ids, and two different real ids refuse;
  2. else `<repo>-<YYYYMMDD>-<HHMM>` from the run's confirm-start UTC time.
The advisor runs `docid` ONCE at goal-confirm and puts the id in its
goal-confirm wire turn; every later `render` takes it as `--doc-id` and never
re-derives it (a re-derived time-based id would name a different doc). The
repo slug is DERIVED from the origin remote, else the main worktree, never the
cwd basename: a per-seat worktree's basename is `maw-executor`, not `maw`.

WHO AUDITS WHOM. A seat auditing itself is the claimant vouching for itself,
so at CLOSE each seat audits its PEER (`audit --as <peer>`, the peer found by
role in `net.cli who`) and itself. The peer's audit is the gate; nonzero from
either pages the operator.

WHAT THE AUDIT MISSES. It is detective, not preventive, and it reads one file:
  - writes made from another session or tool on the same account (a headless
    child whose transcript is not under this seat's session dir, another
    machine, the CoS, a browser), and the Deck UI itself;
  - a transcript that was rotated, compacted away, truncated or deleted before
    the audit ran, and any write made after the audit ran;
  - a tool renamed to something that does not end in `ArtifactData`, or a
    write that reaches the database by another path (the page's own code,
    a raw HTTP call from Bash);
  - an input shape it does not parse is reported as a violation, not skipped,
    but a shape that parses yet hides the collection somewhere new (a future
    field) is invisible to it;
  - it trusts that the transcript records what was sent. A tool_use block is
    what the model asked for; a call the harness refused still counts.
  - `--as` reads the transcript path from the switchboard registry. A seat that
    unregisters first leaves a tombstone (its last transcript pointer, #352)
    that `--as` falls back to, so the peer audit still lands; the stand-down
    ritual should still order the peer's audit BEFORE unregister;
  - that path is written into the seat's OWN registry file at SessionStart, so
    a seat that rewrites its registry entry can point the audit at a clean
    file. The peer audit narrows this (the peer, not the claimant, runs the
    check) but does not close it;
  - the LAST seat to stand down is audited only by itself (its peer is gone).
    The next run's advisor can narrow that at grounding with `--transcript` /
    `--session-id` from the previous run's stand-down turn.
It checks the collection only, not the artifact URL: a seat's `cards` write to
ANY artifact fires, and a `runs` write to the wrong artifact passes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import opconf

COLLECTION = "runs"  # the ONLY collection a seat writes; deliberately not a parameter
STAGES = ("working", "blocked", "wrapping", "wrapped")
FIELDS = ("repo", "goal", "stage", "advisor_pane", "executor_pane", "prs_merged",
          "prs_open", "blocked_on", "last_report", "next")
# ArtifactData's own doc_id pattern (one path segment).
DOC_ID_RE = re.compile(r"^(?!\.\.?$)[A-Za-z0-9_\-.~:@+]{1,200}$")
# Anywhere in a line: the real CoS brief names the id mid-sentence
# ("Report to runs (doc id maw-deck-0930) at start, ..."). `<>` are captured so
# a template like `<repo>-<YYYYMMDD>` is seen whole and refused as a placeholder.
_BRIEF_ID_RE = re.compile(
    r"\bdoc[ _-]?id\b\**\s*(?:[:=]|\bis\b)?\s*`?([A-Za-z0-9_\-.~:@+<>]+)", re.IGNORECASE)
_PLACEHOLDERS = frozenset({"tbd", "todo", "xxx", "none", "n-a", "na", "tba", "id"})
READ_ACTIONS = frozenset({"get", "list", "query", "profiles"})
TOOL_SUFFIX = "ArtifactData"


class DeckError(ValueError):
    pass


def _one_line(label: str, value: str, *, allow_empty: bool = False) -> str:
    v = value.strip()
    if not v and not allow_empty:
        raise DeckError(f"{label} is empty")
    if any(c in v for c in "\r\n\x00  "):
        raise DeckError(f"{label} must be one line ({v[:40]!r})")
    return v


def parse_ints(label: str, raw: str) -> list[int]:
    """`1,2, 3` -> [1, 2, 3]. Empty -> []. Anything not a positive int refuses."""
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip().lstrip("#")
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise DeckError(f"{label}: {part!r} is not a PR number")
        out.append(int(part))
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- derivation ---------------------------------------------------------------

def _git(args: list[str], cwd: str | None) -> str:
    try:
        cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _slug_from_remote(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def repo_slug(cwd: str | None = None) -> str:
    """The repo's identity: the origin remote's repo name, else the MAIN
    worktree's slug. Refuses rather than fall back to the cwd basename."""
    slug = _slug_from_remote(_git(["remote", "get-url", "origin"], cwd))
    if not slug:
        try:
            from net import worktree
            slug = worktree.logical_repo_slug(cwd)
        except Exception:  # noqa: BLE001 — refused below, never guessed
            slug = ""
    slug = re.sub(r"[^A-Za-z0-9_\-.]", "-", slug).strip("-.")
    if not slug:
        raise DeckError("cannot derive the repo slug (no origin remote, not a git repo)")
    return slug


def _is_placeholder(raw: str) -> bool:
    return ("<" in raw or ">" in raw or raw.lower() in _PLACEHOLDERS
            or not any(c.isdigit() for c in raw)
            or not DOC_ID_RE.match(raw))


def brief_doc_id(text: str) -> str | None:
    """The doc id a launch brief names, or None. Placeholders (TBD, `<id>`, a
    `<repo>-...` template, anything with no digit) are not ids: every real id
    carries one (the CoS's `maw-deck-0930`, the derived date), prose does not. Two
    different real ids refuse; the same id named twice is fine."""
    found: set[str] = set()
    for m in _BRIEF_ID_RE.finditer(text):
        raw = m.group(1).rstrip(".,:;")
        if raw and not _is_placeholder(raw):
            found.add(raw)
    if len(found) > 1:
        raise DeckError(f"the brief names more than one doc id: {sorted(found)}")
    return found.pop() if found else None


def derive_doc_id(repo: str, started: datetime) -> str:
    return f"{repo}-{started.astimezone(timezone.utc):%Y%m%d-%H%M}"


def _parse_utc(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise DeckError(f"--started {raw!r} is not an ISO time") from None
    if dt.tzinfo is None:
        raise DeckError("--started must carry a zone (Z or +00:00)")
    return dt


def resolve_doc_id(*, brief_text: str | None, repo: str, started: datetime) -> str:
    doc_id = (brief_doc_id(brief_text) if brief_text else None) or derive_doc_id(repo, started)
    if not DOC_ID_RE.match(doc_id):
        raise DeckError(f"doc id {doc_id!r} is not a valid ArtifactData doc_id")
    return doc_id


def seat_panes(cwd: str | None = None) -> dict[str, str]:
    """{advisor_pane, executor_pane} from this repo's live switchboard entries.

    A role with no entry, or with more than one, is "" — a guessed pane would
    point the operator at the wrong seat."""
    out = {"advisor_pane": "", "executor_pane": ""}
    try:
        from net import registry
        common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
        common = str(Path(common).resolve()) if common else ""
        entries = registry.who(prune=False)
    except Exception:  # noqa: BLE001 — panes are display-only
        return out
    for role in ("advisor", "executor"):
        mine = [e for e in entries if e.role == role
                and (not common or not e.repo_common_dir or e.repo_common_dir == common)]
        if len(mine) == 1:
            out[f"{role}_pane"] = mine[0].herdr_pane or mine[0].tmux_pane or ""
        elif not mine and os.environ.get("MAW_BRIDGE_ROLE") == role:
            # Unregistered but launched into this seat: our own launch env is the pane.
            out[f"{role}_pane"] = (registry.detect_herdr_pane()
                                   or os.environ.get("TMUX_PANE", "").strip())
    return out


# --- render -------------------------------------------------------------------

def build_run(*, repo: str, goal: str, stage: str, advisor_pane: str, executor_pane: str,
              prs_merged: list[int], prs_open: list[int], blocked_on: str, next_step: str,
              last_report: str | None = None) -> dict:
    """The full `runs` document, every field present and validated."""
    if stage not in STAGES:
        raise DeckError(f"stage must be one of {STAGES}, not {stage!r}")
    for label, xs in (("prs_merged", prs_merged), ("prs_open", prs_open)):
        if not all(type(x) is int and x > 0 for x in xs):
            raise DeckError(f"{label} must be positive ints: {xs!r}")
    blocked_on = _one_line("blocked_on", blocked_on, allow_empty=True)
    if stage == "blocked" and not blocked_on:
        raise DeckError("stage blocked needs --blocked-on (what the run waits on)")
    doc = {
        "repo": _one_line("repo", repo),
        "goal": _one_line("goal", goal),
        "stage": stage,
        "advisor_pane": _one_line("advisor_pane", advisor_pane, allow_empty=True),
        "executor_pane": _one_line("executor_pane", executor_pane, allow_empty=True),
        "prs_merged": list(prs_merged),
        "prs_open": list(prs_open),
        "blocked_on": blocked_on,
        "last_report": last_report or now_iso(),
        "next": _one_line("next", next_step),
    }
    if tuple(doc) != FIELDS:  # not an assert: the check must survive `python -O`
        raise DeckError(f"runs doc fields drifted from FIELDS: {tuple(doc)}")
    return doc


def build_call(*, url: str, doc_id: str, doc: dict, if_version: int | None) -> dict:
    """The ArtifactData arguments. `set` creates the doc; any later write is an
    `update` pinned to the version the previous write returned."""
    if not DOC_ID_RE.match(doc_id):
        raise DeckError(f"doc id {doc_id!r} is not a valid ArtifactData doc_id")
    if not url.startswith("https://claude.ai/"):
        raise DeckError(f"run page url {url!r} is not a claude.ai artifact link")
    call: dict = {"url": url, "action": "set" if if_version is None else "update",
                  "collection": COLLECTION, "doc_id": doc_id, "data": doc}
    if if_version is not None:
        if if_version < 1:
            raise DeckError("--if-version must be >= 1")
        call["if_version"] = if_version
    return call


# --- audit --------------------------------------------------------------------

def _tool_uses(record: object) -> Iterator[dict]:
    if not isinstance(record, dict):
        return
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def write_violations(inp: object) -> list[str]:
    """Why this ArtifactData input breaks the runs-only rule ([] when it does
    not). A read is fine. Anything not provably a runs write is a violation:
    an unknown action counts as a write, a batch entry we cannot read counts."""
    if not isinstance(inp, dict):
        return ["input is not an object"]
    action = inp.get("action")
    if action in READ_ACTIONS:
        return []
    if action == "batch":
        writes = inp.get("writes")
        if not isinstance(writes, list) or not writes:
            return ["batch with no readable `writes` list"]
        out = []
        for n, w in enumerate(writes):
            if not isinstance(w, dict):
                out.append(f"batch[{n}] is not an object")
            elif w.get("collection") != COLLECTION:
                out.append(f"batch[{n}] {w.get('op')!s} to collection "
                           f"{w.get('collection')!r}")
        return out
    if inp.get("collection") != COLLECTION:
        return [f"{action!s} to collection {inp.get('collection')!r}"]
    return []


def audit_lines(lines: Iterator[str], source: str) -> list[str]:
    """Findings for one transcript: one line each, naming file and line."""
    findings: list[str] = []
    for n, line in enumerate(lines, 1):
        # No substring prefilter: JSON may escape any character of the tool
        # name or of "tool_use" (\u0041rtifactData), so parse every line.
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if TOOL_SUFFIX.lower() in line.lower():
                findings.append(f"{source}:{n}: unparseable line mentions {TOOL_SUFFIX}")
            continue
        for block in _tool_uses(record):
            name = block.get("name")
            if not (isinstance(name, str) and name.lower().endswith(TOOL_SUFFIX.lower())):
                continue
            for why in write_violations(block.get("input")):
                findings.append(f"{source}:{n}: {name} {why} (tool_use {block.get('id')})")
    return findings


def transcript_files(transcript: Path) -> list[Path]:
    """The session transcript plus its subagents' transcripts, which Claude
    Code keeps under `<session>/subagents/` (same account, same seat)."""
    sub = transcript.with_suffix("") / "subagents"
    return [transcript, *sorted(sub.glob("*.jsonl"))] if sub.is_dir() else [transcript]


def audit(transcript: Path) -> list[str]:
    findings: list[str] = []
    for f in transcript_files(transcript):
        with f.open(encoding="utf-8", errors="replace") as fh:
            findings.extend(audit_lines(fh, str(f)))
    return findings


def resolve_transcript(*, path: str | None, session_id: str | None, cwd: str | None,
                       seat: str | None) -> Path:
    """The ONE transcript to audit. Never the newest file in a project dir: that
    dir holds a transcript per session (headless children, rotated seats)."""
    if path:
        return Path(path).expanduser()
    if session_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise DeckError(f"session id {session_id!r} is not a plain id")
        from bridge.livemodel import project_dir
        return project_dir(cwd or str(Path.cwd())) / f"{session_id}.jsonl"
    if seat:
        from net import registry
        entry = registry.load_entry(seat)
        if entry is None:
            # #352: a seat that stood down before its peer's counter-audit left a
            # tombstone with its last transcript pointer — audit that instead of
            # exiting 2 'no switchboard entry'.
            tomb = registry.load_tombstone(seat)
            if tomb and str(tomb.get("transcript_path") or "").strip():
                print("audit fell back to last-known tombstone: session "
                      f"{tomb.get('session_id') or '?'}, "
                      f"unregistered {tomb.get('unregistered_at') or '?'}",
                      file=sys.stderr)
                return Path(str(tomb["transcript_path"]))
            raise DeckError(f"no switchboard entry for {seat!r}")
        if not (entry.transcript_path or "").strip():
            raise DeckError(f"{seat!r} has no pinned transcript; pass --transcript "
                            f"or --session-id")
        return Path(entry.transcript_path)
    raise DeckError("name the transcript: --as, --session-id or --transcript")


# --- CLI ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="net.deck", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("docid", help="the run page doc id for this run (run ONCE, at goal-confirm)")
    d.add_argument("--brief", default=None,
                   help="the goal doc; a `doc id <id>` line in it wins")
    d.add_argument("--started", default=None,
                   help="the run's confirm-start time, ISO UTC (default: now)")

    r = sub.add_parser("render", help="print the ArtifactData arguments for one runs write")
    r.add_argument("--doc-id", required=True,
                   help="from the advisor's goal-confirm wire turn (see `docid`)")
    r.add_argument("--stage", required=True, choices=STAGES)
    r.add_argument("--goal", required=True, help="one plain line")
    r.add_argument("--next", dest="next_step", required=True, help="one plain line")
    r.add_argument("--blocked-on", default="", help="one plain line; required for blocked")
    r.add_argument("--prs-merged", default="", help="comma list, e.g. 12,14")
    r.add_argument("--prs-open", default="", help="comma list")
    r.add_argument("--advisor-pane", default=None, help="default: from the switchboard")
    r.add_argument("--executor-pane", default=None, help="default: from the switchboard")
    r.add_argument("--if-version", type=int, default=None,
                   help="the version the previous write returned; omit ONLY on the first write")
    r.add_argument("--url", default=None, help="default: deck_url (env MAW_DECK_URL / operator.toml)")

    a = sub.add_parser("audit", help="exit 1 if the transcript holds a non-runs ArtifactData write")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--as", dest="seat", default=None,
                   help="this seat's switchboard name: audits its PINNED transcript")
    g.add_argument("--session-id", default=None,
                   help="a Claude session id, resolved under --cwd's project dir")
    g.add_argument("--transcript", default=None, help="a transcript .jsonl path")
    a.add_argument("--cwd", default=None, help="the session's launch cwd (with --session-id)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "docid":
            brief = (Path(args.brief).expanduser().read_text(encoding="utf-8")
                     if args.brief else None)
            started = _parse_utc(args.started) if args.started else datetime.now(timezone.utc)
            print(resolve_doc_id(brief_text=brief, repo=repo_slug(), started=started))
            return 0
        if args.command == "render":
            url = args.url or opconf.get("deck_url")
            if not url:
                raise DeckError("no run page url: pass --url or set deck_url "
                                "(MAW_DECK_URL / ~/.maw/operator.toml)")
            panes = seat_panes()
            doc = build_run(
                repo=repo_slug(), goal=args.goal, stage=args.stage,
                advisor_pane=args.advisor_pane if args.advisor_pane is not None
                else panes["advisor_pane"],
                executor_pane=args.executor_pane if args.executor_pane is not None
                else panes["executor_pane"],
                prs_merged=parse_ints("--prs-merged", args.prs_merged),
                prs_open=parse_ints("--prs-open", args.prs_open),
                blocked_on=args.blocked_on, next_step=args.next_step)
            call = build_call(url=url, doc_id=args.doc_id, doc=doc,
                              if_version=args.if_version)
            print(json.dumps(call, indent=2))
            print("Send these as the ArtifactData arguments, unchanged. Keep the "
                  "returned version for the next --if-version. Collection is "
                  "always runs: never write cards or signals.", file=sys.stderr)
            return 0
        if args.command == "audit":
            path = resolve_transcript(path=args.transcript, session_id=args.session_id,
                                      cwd=args.cwd, seat=args.seat)
            if not path.is_file():
                print(f"net.deck audit: transcript not found: {path} — cannot vouch "
                      f"for this seat; page the operator", file=sys.stderr)
                return 2
            findings = audit(path)
            for f in findings:
                print(f)
            if findings:
                print(f"net.deck audit: {len(findings)} ArtifactData write(s) outside "
                      f"`{COLLECTION}` — page the operator", file=sys.stderr)
                return 1
            return 0
    except (DeckError, OSError) as e:
        print(f"net.deck: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
