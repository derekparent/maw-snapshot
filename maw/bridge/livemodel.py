"""Read the LIVE model of a Claude session from its transcript tail.

Shared by two consumers so they agree on one definition of "the model that
actually ran":
  - bridge-out / durable send (B2) — stamp a turn with the model that WROTE
    it, so a fallback session can't label its verdict with the pinned model;
  - pulse (B3) — compare that live model against the launcher-pinned model
    to surface a flip.

MAIN-LOOP ONLY: a subagent (isSidechain) record carries its OWN model — a
seat that just ran a haiku/sonnet subagent must not read that as its own
model. Fail-open everywhere: any miss (no dir, vanished file, unreadable,
unparseable) returns None; a model-provenance read must never crash a caller.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
TAIL_BYTES = 262_144


def project_dir(cwd: str, projects_root: Path | None = None) -> Path:
    """Claude Code's transcript dir for a cwd: every '/' and '.' becomes '-'."""
    root = projects_root or PROJECTS_ROOT
    return root / re.sub(r"[/.]", "-", cwd)


def tail_last_main_models(path: Path, limit: int = 5) -> list[str]:
    """The models of the last `limit` MAIN-LOOP assistant records, newest first.

    One record is not a state: a single transient fallback turn stays "the last"
    until a fresh turn overwrites it, which is how B3's flip check fired on a seat
    whose pane said 4.8 (#188). A caller that wants PERSISTENCE reads several and
    decides. Same tail-read, same isSidechain skip, same fail-open ([] on any miss).
    """
    out: list[str] = []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        if rec.get("isSidechain"):
            continue
        message = rec.get("message")
        if isinstance(message, dict) and message.get("model"):
            out.append(str(message["model"]))
            if len(out) >= limit:
                break
    return out


def live_models_for_cwd(cwd: str, limit: int = 5,
                        projects_root: Path | None = None) -> list[str]:
    """The last `limit` main-loop models of the session in `cwd`, newest first."""
    proj = project_dir(cwd, projects_root)
    try:
        if not proj.is_dir():
            return []
        transcripts = sorted(
            proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return []
    if not transcripts:
        return []
    return tail_last_main_models(transcripts[0], limit)


def tail_last_main_model(path: Path) -> str | None:
    """The model of the last MAIN-LOOP assistant record in a transcript.

    Tail-read (last TAIL_BYTES); scan backwards for the most recent assistant
    record that is NOT a sidechain and carries a model. Any read/parse failure
    -> None (fail-open).
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        if rec.get("isSidechain"):
            continue  # subagent record — its model is not the seat's model
        message = rec.get("message")
        if isinstance(message, dict) and message.get("model"):
            return str(message["model"])
    return None


def live_model_for_cwd(cwd: str, projects_root: Path | None = None) -> str | None:
    """The model actually running the session in `cwd`, or None.

    A live seat writes its transcript continuously, so the most-recently-
    modified .jsonl in the seat's project dir is its live conversation
    (per-seat worktrees give each seat its own cwd -> its own project dir).
    The glob/stat/sort is wrapped: a transcript can vanish mid-scan, and a
    provenance read must fail open (None), never crash the caller.
    """
    proj = project_dir(cwd, projects_root)
    try:
        if not proj.is_dir():
            return None
        transcripts = sorted(
            proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    if not transcripts:
        return None
    return tail_last_main_model(transcripts[0])
