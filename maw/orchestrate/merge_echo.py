"""merge_echo — the bounded-merge gate's literal artifact.

The operator's 2026-06-17 ruling: the executor runs ``gh pr merge``, but the safety is in
WHAT is authorized, not who types it. The operator's "Y" is taken against an EXPLICIT,
LITERAL, ORDERED echo of the exact PRs + titles + order that will merge — never
a bare "yes", never a standing "merge whatever you finish".

``merge_echo(pr_numbers, runner)`` fetches each PR's title (``gh pr view`` via the
injectable runner) and returns that literal echo: a numbered list of
``#<n> — <title>`` IN THE GIVEN ORDER, closed by the fixed confirmation line. The
order is the order the operator confirms; this helper never re-sorts and never merges. It
only renders the text the gate is taken against.

The merge plan it echoes must already be ADVISOR-VETTED (the advisor reviewed the
PRs and recommended this order); branch protection (PR-only, enforce_admins) is
the structural floor under the soft gate, so only open, reviewed PR content is
mergeable at all. This helper is the human-legible ceremony on top of that floor.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

CONFIRM_LINE = "Confirm Y to merge in this exact order"
_TITLE_UNAVAILABLE = "(title unavailable — verify on GitHub before Y)"


def _run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def pr_title(number: int, runner: Runner = _run) -> str:
    """The PR's title via ``gh pr view --json title``.

    On any gh failure or unparseable output, returns a LOUD placeholder rather
    than a blank or a fabricated title — a merge echo with a hidden title would
    let the operator's Y land against a PR he can't see. The placeholder forces a manual
    check before confirmation.
    """
    try:
        cp = runner(["gh", "pr", "view", str(number), "--json", "title"])
    except (OSError, subprocess.SubprocessError):
        return _TITLE_UNAVAILABLE
    if cp.returncode != 0 or not cp.stdout.strip():
        return _TITLE_UNAVAILABLE
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return _TITLE_UNAVAILABLE
    title = data.get("title") if isinstance(data, dict) else None
    if not isinstance(title, str) or not title.strip():
        return _TITLE_UNAVAILABLE
    return title.strip()


def merge_echo(pr_numbers: list[int], runner: Runner = _run) -> str:
    """The literal ordered merge-plan echo the operator's Y is taken against.

    pr_numbers: the EXACT order the executor will merge (advisor-vetted). This
    function preserves that order verbatim — it does not sort, dedup, or filter.

    Returns a numbered list, one ``N. #<pr> — <title>`` per line, followed by the
    fixed CONFIRM_LINE. An empty list returns a single explicit line so a
    "nothing to merge" echo can never read as a blank confirmation prompt.
    """
    if not pr_numbers:
        return "(no PRs in the merge plan — nothing to confirm)"
    lines: list[str] = []
    for idx, number in enumerate(pr_numbers, start=1):
        title = pr_title(number, runner)
        lines.append(f"{idx}. #{number} — {title}")
    lines.append(CONFIRM_LINE)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="orchestrate.merge_echo",
        description="Render the literal ordered merge-plan echo for the operator's Y.",
    )
    parser.add_argument(
        "pr_numbers",
        nargs="+",
        type=int,
        metavar="PR",
        help="PR numbers in the EXACT merge order (advisor-vetted)",
    )
    args = parser.parse_args(argv)
    print(merge_echo(args.pr_numbers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
