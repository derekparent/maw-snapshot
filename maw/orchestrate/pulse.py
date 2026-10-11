"""pulse — the deterministic advisor PULSE (the orchestration loop's data core).

The operator's 2026-06-17 vision: the ADVISOR is the orchestrator of the advisor/executor
pair, human-gated at merge. The advisor runs a dynamic ``/loop`` that, each
cycle, must decide ONE thing: **page the operator, or stay quiet.** That decision must be
reproducible by a median model, so the inputs to it are gathered here, in plain
deterministic Python, and the advisor LLM reasons on top.

``pulse(home, runner)`` gathers, WITHOUT CONSUMING ANYTHING:

  * inbox  — count + senders of UNCONSUMED durable-bridge turns addressed to the
             advisor (PEEK only; the loop drains separately via ``net recv``).
             Flags whether any pending turn looks like a structured
             BLOCKED-SUMMARY (a documented heuristic, below).
  * watch  — the net.watch alarms (dark / parked-wire / orphaned-draft /
             refused-heartbeat / genuinely-dark), by REUSING net.watch's
             finding functions.
  * prs    — NEW open PRs since a recorded cursor (``gh pr list`` via the
             injectable runner; cursor persisted under ``~/.maw/.runtime``).
  * recommendation — a structured ``Pulse`` with ``page: bool`` + ``reasons``.

``page`` is True iff ANY of:
  (a) a blocked-summary turn is present in the inbox, OR
  (b) the executor is dark/frozen per watch (a genuinely-dark finding), OR
  (c) a merge-pending gate event is present (a new PR whose branch is on the
      merge-pending set — the loop watches the gate, it never operates it), OR
  (d) an orphaned-draft finding is present (a stuck /rc draft parking a pane —
      Operator ruling 2026-07-18; PAGE_WORTHY_KINDS).

Else: quiet.

THE FLOOR. ``pulse`` only ever RECOMMENDS a page. It consumes no turn, opens no
PR, and merges nothing. The Layer-1 floor (merge-to-main / money / external
comms / credentials / scope-of-authority) is enforced by the loop and the seat,
not weakened here — pulse simply surfaces the gate events that MUST reach the operator.

Hermetic: ``home`` overrides the cursor-state root; ``runner`` drives every
subprocess (gh / git / net.watch's git), so tests stage state and assert
DECISIONS, never importability or live I/O.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.cli import _resolve_repo_slug  # noqa: E402
from bridge.format import Turn, parse_turns  # noqa: E402
from net import watch  # noqa: E402
from net.registry import Entry, who  # noqa: E402

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

ADVISOR_ROLE = "advisor"
EXECUTOR_ROLE = "executor"

# net.watch findings that mean the executor (or any seat) is dark/frozen — the
# (b) page trigger. ONLY genuinely-dark: it is hours-grained AND commit-aware
# (a recent commit anywhere in the repo suppresses it via --branches), so a
# long synchronous build — xcodebuild/test runs that don't tick the
# UserPromptSubmit heartbeat for 15-20 minutes — does NOT false-page. The bare
# minute-grained dark-session is DELIBERATELY excluded: heartbeats are
# UserPromptSubmit-driven, so a single long synchronous turn lets last_seen go
# stale at DEFAULT_DARK_MIN (15 min) of LEGITIMATE work — paging on it cries
# wolf and trains alarm fatigue (advisor_ios lived this; the rev-1 lesson the
# watcher itself encodes). parked-wire / refused-heartbeat are surfaced in the
# pulse but likewise do not by THEMSELVES force a page (recoverable by a recv /
# reseat). See _decide and the away-mode supervision section of commands/advisor.md.
DARK_KINDS = ("genuinely-dark",)

# net.watch findings that force a page for their OWN reason (a (d) trigger),
# distinct from executor-dark. Operator ruling 2026-07-18: an orphaned draft (a typed-
# but-never-submitted /rc prompt) PAGES — it cost him five rulings in three days
# and silently parks the pane, and it is deterministic + low-FP after the >=2-park
# guard, so it clears the alarm-fatigue bar the bare minute-grained dark timer
# does not. It is NOT "dark/frozen" (the seat may be fine; the draft is stuck), so
# it is its own kind, not folded into DARK_KINDS.
PAGE_WORTHY_KINDS = ("orphaned-draft",)

# The away-mode genuinely-dark threshold (HOURS). Generous on purpose: the page
# must trip only on hours-grained, commit-aware darkness, never on minute-grained
# silence. A synchronous build running LONGER than this with ZERO commits and
# zero heartbeat is the acknowledged uncloseable-cheaply hole (indistinguishable
# from abandoned by wire+commit signals alone) — kept generous, leaning on the
# deterministic parked-wire alarm + the operator presence. See commands/advisor.md (section 4, Away-mode supervision).
GENUINELY_DARK_HOURS = 2


def _run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _runtime_dir(home: Path) -> Path:
    return home / ".maw" / ".runtime"


def _pr_cursor_file(home: Path) -> Path:
    return _runtime_dir(home) / "orchestrate-pr-cursor.json"


@dataclass(frozen=True)
class InboxState:
    """PEEK of the advisor's unconsumed durable-bridge inbox (no consume)."""

    count: int
    senders: list[str]
    blocked_summary: bool
    turn_ids: list[int]


@dataclass(frozen=True)
class PRState:
    """New open PRs since the recorded cursor (the review queue)."""

    new: list[dict]  # each: {"number": int, "title": str, "headRefName": str}
    cursor_advanced_to: int  # the highest PR number now recorded


@dataclass(frozen=True)
class Pulse:
    """One advisor-loop cycle's gathered state + the page-or-quiet decision."""

    inbox: InboxState
    watch: list[watch.Finding]
    prs: PRState
    page: bool
    reasons: list[str] = field(default_factory=list)


def looks_blocked_summary(body: str) -> bool:
    """Heuristic: does this turn body look like a structured BLOCKED-SUMMARY?

    The /maw:goal protocol (commands/goal.md) tells the executor to post, on a REAL
    block, a summary with done / outstanding / recommended-next. We detect it
    cheaply and on purpose loosely — a false positive pages the operator (safe; he glances
    and dismisses), a false negative strands a real block (unsafe). So the test
    is deliberately generous:

      * a line that begins with 'BLOCKED' (case-insensitive, after strip), OR
      * a 'recommended next' line (the summary's signature closing section).

    Documented + simple by design (the vision's instruction). The structured
    marker is prose discipline, not a parsed schema — keeping the detector a
    heuristic means a slightly-off summary still pages rather than silently
    falling through.
    """
    for raw in body.splitlines():
        line = raw.strip().lower()
        if line.startswith("blocked"):
            return True
        if line.startswith("recommended next"):
            return True
    return False


def _advisor_repo_root(runner: Runner) -> Path:
    """The logical repo root for slug resolution.

    Prefer the live advisor registry entry (its repo identity is authoritative —
    same source bridgelink uses). Fall back to git rev-parse, then cwd, so the
    pulse still resolves a bridge file when run outside a registered seat (tests,
    a cold launch). Slug resolution itself is the single source of truth
    (_resolve_repo_slug), so this only has to land on the right repo_root.
    """
    for entry in who(prune=False):
        if entry.role == ADVISOR_ROLE and entry.repo_root:
            return Path(entry.repo_root)
    try:
        cp = runner(["git", "rev-parse", "--show-toplevel"])
        out = cp.stdout.strip()
        if cp.returncode == 0 and out:
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def _advisor_inbox_file(runner: Runner) -> Path:
    """The bridge file the advisor READS: from-<executor>.md under the shared slug.

    Uses the unified slug resolver (_resolve_repo_slug — the SAME one
    /maw:bridge-in and net recv use) so the pulse peeks exactly the file the
    drain consumes; they cannot diverge on a worktree pair.
    """
    repo_root = _advisor_repo_root(runner)
    slug = _resolve_repo_slug(repo_root)
    return Path.home() / ".claude" / "bridge" / slug / f"from-{EXECUTOR_ROLE}.md"


def _unconsumed_turns(file: Path) -> list[Turn]:
    """Every unconsumed turn in a bridge file, highest revision per turn_id.

    PEEK semantics — reads only. find_unconsumed_oldest/_latest return ONE turn;
    the pulse needs the full pending set (count + senders + blocked scan), so we
    enumerate here rather than consume. Mirrors their revision-dedup so a
    --revise'd turn counts once.
    """
    if not file.exists():
        return []
    by_id: dict[int, Turn] = {}
    for t in parse_turns(file):
        if t.consumed_at is not None:
            continue
        prior = by_id.get(t.turn_id)
        if prior is None or t.revision > prior.revision:
            by_id[t.turn_id] = t
    return [by_id[k] for k in sorted(by_id)]


def gather_inbox(runner: Runner = _run) -> InboxState:
    file = _advisor_inbox_file(runner)
    turns = _unconsumed_turns(file)
    senders = sorted({t.role for t in turns})
    blocked = any(looks_blocked_summary(t.body) for t in turns)
    return InboxState(
        count=len(turns),
        senders=senders,
        blocked_summary=blocked,
        turn_ids=[t.turn_id for t in turns],
    )


def gather_watch(runner: Runner = _run) -> list[watch.Finding]:
    """net.watch's own sweep — REUSED, not reimplemented.

    genuinely_dark is opt-in (0 = off) in the watcher itself, but the AWAY-MODE
    advisor loop ARMS it here at a generous hours threshold (GENUINELY_DARK_HOURS)
    — that hours-grained, commit-aware finding is the dark/frozen one the page
    decision trips on (DARK_KINDS). dark-session (15 min) and parked-wire still
    fire on their standard thresholds and are surfaced for the advisor to reason
    on, but they do NOT force a page: a long synchronous build leaves last_seen
    stale at 15 min of legitimate work, so paging on bare dark-session would cry
    wolf. The other page-forcing finding is orphaned-draft (PAGE_WORTHY_KINDS, THE OPERATOR
    ruling 2026-07-18) — deterministic and low-FP, so it pages on its own reason.
    pulse decides what FORCES a page, the watcher decides what IS a finding.
    """
    return watch.sweep(runner=runner, genuinely_dark_hours=GENUINELY_DARK_HOURS)


def _read_pr_cursor(home: Path) -> int:
    f = _pr_cursor_file(home)
    if not f.is_file():
        return 0
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return int(data.get("last_pr", 0))
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        return 0


def _write_pr_cursor(home: Path, last_pr: int) -> None:
    f = _pr_cursor_file(home)
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"last_pr": last_pr}, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(f)


def _list_open_prs(runner: Runner) -> list[dict]:
    """`gh pr list` (open) as a list of dicts; [] on any gh failure.

    A gh failure must not crash the loop or fabricate a queue — an empty list is
    the honest 'I couldn't see the PR queue this cycle' (the next cycle retries).
    """
    try:
        cp = runner(
            [
                "gh", "pr", "list", "--state", "open",
                "--json", "number,title,headRefName",
                "--limit", "100",
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and "number" in d]


def gather_prs(
    home: Path,
    runner: Runner = _run,
    advance_cursor: bool = True,
) -> PRState:
    """NEW open PRs since the recorded cursor; advance the cursor past them.

    The cursor is the highest PR number seen on a PRIOR cycle. New PRs are those
    with number > cursor. Advancing the cursor to the max seen this cycle means a
    PR enters the review queue exactly once — the loop reviews it, then it is no
    longer 'new'. advance_cursor=False supports a peek (e.g. a dry-run report)
    that must not consume the queue.
    """
    cursor = _read_pr_cursor(home)
    all_open = _list_open_prs(runner)
    numbers = [int(d["number"]) for d in all_open]
    new = [d for d in all_open if int(d["number"]) > cursor]
    highest = max([cursor, *numbers]) if numbers else cursor
    if advance_cursor and highest > cursor:
        _write_pr_cursor(home, highest)
    return PRState(new=sorted(new, key=lambda d: int(d["number"])),
                   cursor_advanced_to=highest)


def _decide(
    inbox: InboxState,
    watch_findings: list[watch.Finding],
    prs: PRState,
    merge_pending_branches: set[str],
) -> tuple[bool, list[str]]:
    """The page-or-quiet decision. Pure function of gathered state.

    merge_pending_branches: branch names the advisor has marked merge-pending
    (advisor-vetted, awaiting the operator's literal-echo Y). A NEW PR on one of those
    branches is a (c) merge-pending gate event — it MUST reach the operator because the
    next step is the bounded-merge gate. The loop never merges; it pages.
    """
    reasons: list[str] = []
    if inbox.blocked_summary:
        reasons.append(
            f"blocked-summary turn pending from {', '.join(inbox.senders) or 'executor'} "
            f"(turn_ids={inbox.turn_ids})"
        )
    dark = [f for f in watch_findings if f.kind in DARK_KINDS]
    for f in dark:
        reasons.append(f"executor dark/frozen: {f.line()}")
    stuck = [f for f in watch_findings if f.kind in PAGE_WORTHY_KINDS]
    for f in stuck:
        reasons.append(f"orphaned draft blocking a pane — service it: {f.line()}")
    gate_prs = [
        d for d in prs.new
        if str(d.get("headRefName", "")) in merge_pending_branches
    ]
    for d in gate_prs:
        reasons.append(
            f"merge-pending gate event: PR #{d['number']} on "
            f"{d.get('headRefName')} — bounded-merge gate, page the operator for the Y"
        )
    return (len(reasons) > 0), reasons


def pulse(
    home: Path | None = None,
    runner: Runner = _run,
    merge_pending_branches: set[str] | None = None,
    advance_cursor: bool = True,
) -> Pulse:
    """One advisor-loop cycle: gather (no consume) + decide page-or-quiet.

    home: root for the PR cursor state file (defaults to ~). Injectable so tests
          are hermetic.
    runner: drives gh / git / net.watch's git. Injectable for the same reason.
    merge_pending_branches: advisor-vetted branches awaiting the operator's literal-echo Y;
          a new PR on one is a (c) merge-pending page event.
    advance_cursor: when False, the PR cursor is not written (a peek/dry-run).
    """
    root = home if home is not None else Path.home()
    pending = merge_pending_branches or set()
    inbox = gather_inbox(runner)
    watch_findings = gather_watch(runner)
    prs = gather_prs(root, runner, advance_cursor=advance_cursor)
    page, reasons = _decide(inbox, watch_findings, prs, pending)
    return Pulse(inbox=inbox, watch=watch_findings, prs=prs, page=page, reasons=reasons)


def render(p: Pulse) -> str:
    """Human/loop-readable one-block summary of a pulse."""
    lines = [
        f"pulse: {'PAGE the operator' if p.page else 'quiet'}",
        f"  inbox: {p.inbox.count} unconsumed turn(s)"
        + (f" from {', '.join(p.inbox.senders)}" if p.inbox.senders else "")
        + ("  [BLOCKED-SUMMARY]" if p.inbox.blocked_summary else ""),
        f"  watch: {len(p.watch)} finding(s)"
        + ("".join(f"\n    {f.line()}" for f in p.watch) if p.watch else ""),
        f"  prs: {len(p.prs.new)} new"
        + (f" — {', '.join('#' + str(d['number']) for d in p.prs.new)}"
           if p.prs.new else ""),
    ]
    if p.reasons:
        lines.append("  page reasons:")
        lines.extend(f"    - {r}" for r in p.reasons)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="orchestrate.pulse")
    parser.add_argument(
        "--merge-pending",
        action="append",
        default=[],
        metavar="BRANCH",
        help="a merge-pending (advisor-vetted) branch; a new PR on it pages the operator",
    )
    parser.add_argument(
        "--no-advance",
        action="store_true",
        help="peek: do not advance the PR cursor (dry-run report)",
    )
    args = parser.parse_args(argv)
    p = pulse(
        merge_pending_branches=set(args.merge_pending),
        advance_cursor=not args.no_advance,
    )
    print(render(p))
    # Exit 1 on PAGE so a /loop wrapper can branch on the exit code, mirroring
    # net.watch's findings-exit convention.
    return 1 if p.page else 0


if __name__ == "__main__":
    raise SystemExit(main())
