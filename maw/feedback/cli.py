"""feedback.cli — entry point for /maw:feedback and /maw:feedback-review.

The dogfood loop (operator ruling 2026-07-13): seats running the maw pair on OTHER
repos capture friction at session end; the maw advisor reads it on the next
consult. Without this, friction dies with the session that felt it.

Capture is deliberately git-free. A seat in any maw-equipped repo writes ONE
markdown file per session into the shared feedback drop-box — no checkout of
the maw repo, no branch, no PR. Curation stays gated: /maw:feedback-review
(advisor side) triages pending files and promotes anything actionable into
findings/ or an issue through the normal PR path, then archives the file.

Resolution:
  feedback dir: $MAW_FEEDBACK_DIR, else ~/.maw/feedback
  pending  = top-level *.md in that dir
  reviewed = <dir>/reviewed/*.md (archive == move, content untouched)

Filenames sort chronologically and collide gracefully:
  <YYYY-MM-DD>-<repo>-<seat>.md, then -2, -3… (both seats of a pair closing
  the same day is the normal case, not an edge case).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REVIEWED_DIRNAME = "reviewed"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

TEMPLATE = """\
---
repo: {repo}
seat: {seat}
date: {date}
arc: {arc}
prs: []
---

## What worked
-

## Friction
-

## Wish I had
-
"""


def feedback_dir() -> Path:
    env = os.environ.get("MAW_FEEDBACK_DIR")
    if env:
        return Path(env)
    return Path.home() / ".maw" / "feedback"


def _validated(kind: str, value: str) -> str:
    if not NAME_RE.match(value):
        raise ValueError(
            f"invalid {kind} {value!r}: must match {NAME_RE.pattern} "
            "(it becomes part of a filename)"
        )
    return value


def _unique_path(base_dir: Path, stem: str) -> Path:
    candidate = base_dir / f"{stem}.md"
    n = 2
    while candidate.exists():
        candidate = base_dir / f"{stem}-{n}.md"
        n += 1
    return candidate


def _feedback_files(directory: Path) -> "list[Path]":
    """Real feedback notes only: regular *.md files, no dotfiles (editor swap /
    .DS_Store-style strays), no directories (__pycache__, a dir named x.md)."""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*.md")
        if p.is_file() and not p.name.startswith(".")
    )


def cmd_new(repo: str, seat: str, arc: str, *, now: "datetime | None" = None) -> int:
    try:
        repo = _validated("repo", repo)
        seat = _validated("seat", seat)
    except ValueError as e:
        print(f"feedback: {e}", file=sys.stderr)
        return 1
    base = feedback_dir()
    base.mkdir(parents=True, exist_ok=True)
    # UTC date, NOT local (#148): a file minted at 01:15Z was stamped the PREVIOUS
    # day under a UTC-7 localtime, colliding with the prior run's filenames and
    # breaking newest-first sort. `now` is injectable for the midnight-boundary test.
    dt = now if now is not None else datetime.now(timezone.utc)
    today = dt.astimezone(timezone.utc).date().isoformat()
    path = _unique_path(base, f"{today}-{repo}-{seat}")
    path.write_text(TEMPLATE.format(repo=repo, seat=seat, date=today, arc=arc))
    print(path)
    return 0


def cmd_list() -> int:
    base = feedback_dir()
    pending = _feedback_files(base)
    reviewed = _feedback_files(base / REVIEWED_DIRNAME)
    print(f"feedback_dir: {base}")
    print(f"pending: {len(pending)}")
    for p in pending:
        print(f"  {p.name}")
    print(f"reviewed: {len(reviewed)}")
    return 0


def cmd_archive(name: str) -> int:
    base = feedback_dir()
    src = base / name
    # a path-y name would let archive move files outside the drop-box
    if src.name != name or not src.is_file():
        print(f"feedback: no pending file named {name!r} in {base}", file=sys.stderr)
        return 1
    if src.suffix != ".md" or name.startswith("."):
        print(f"feedback: refusing {name!r}: only non-hidden .md notes live in the drop-box", file=sys.stderr)
        return 1
    reviewed_dir = base / REVIEWED_DIRNAME
    reviewed_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(reviewed_dir, src.stem)
    src.rename(dest)
    print(dest)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feedback.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="create a feedback file from the template, print its path")
    p_new.add_argument("--repo", required=True, help="repo the session ran in (e.g. my-app)")
    p_new.add_argument("--seat", required=True, help="seat role (advisor/executor/solo)")
    p_new.add_argument("--arc", default="", help="one-line description of the run")

    sub.add_parser("list", help="list pending + reviewed feedback files")

    p_arch = sub.add_parser("archive", help="move a pending file to reviewed/")
    p_arch.add_argument("name", help="pending filename (as shown by list)")

    args = parser.parse_args(argv)
    if args.command == "new":
        return cmd_new(args.repo, args.seat, args.arc)
    if args.command == "list":
        return cmd_list()
    return cmd_archive(args.name)


if __name__ == "__main__":
    sys.exit(main())
