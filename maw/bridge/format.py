"""Bridge file format — append-only markdown with HTML-comment turn delimiters.

Per PRD §3.2 + §4.2.4. Stdlib-only; no third-party deps. macOS/Linux only.

Each turn is a block bounded by `<!--BRIDGE-TURN-BEGIN turn_id=N-->` and
`<!--BRIDGE-TURN-END turn_id=N-->` comments, with a YAML-shaped frontmatter
block and a single base64-encoded body line. Body is base64-encoded to keep
`consumed_at: null`-style regex matches from triggering on body content.

Each block carries a body_sha256 hash computed BEFORE base64 encoding, so
manual edits and partial writes are detectable on read.

Read parser is line-by-line (no DOTALL regex). Whitespace-strict on write,
whitespace-tolerant on read for the base64 body — accidental editor reflow
doesn't false-trigger the integrity check, but content tampering does.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

MAX_BODY_BYTES = 64 * 1024
LOCK_TIMEOUT_S = 5.0
LOCK_POLL_S = 0.1

BEGIN_RE = re.compile(r"^<!--BRIDGE-TURN-BEGIN turn_id=(\d+)-->\s*$")
END_RE = re.compile(r"^<!--BRIDGE-TURN-END turn_id=(\d+)-->\s*$")
FRONTMATTER_DELIM = "---"
FRONTMATTER_LINE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$")


class BridgeError(Exception):
    """Base class for bridge format errors."""


class BridgeMalformedError(BridgeError):
    """Block boundaries don't line up, or frontmatter is unparseable."""


class BridgeIntegrityError(BridgeError):
    """body_sha256 in frontmatter doesn't match decoded body."""


class BridgeSizeExceededError(BridgeError):
    """Body exceeds MAX_BODY_BYTES."""


class BridgeEmptyBodyError(BridgeError):
    """Empty body rejected at write time."""


class BridgeLockTimeout(BridgeError):
    """Could not acquire lock within LOCK_TIMEOUT_S."""


@dataclass(frozen=True)
class Turn:
    turn_id: int
    ts: str
    role: str
    session_id: str
    revision: int
    consumed_at: str | None
    body_sha256: str
    body: str
    # The model that produced this turn (B2 provenance), e.g. "claude-fable-5".
    # Stamped at bridge-out from the seat's model; "" on turns written before
    # the field existed (parse tolerates its absence — no migration). Not part
    # of the body, so it never affects body_sha256 integrity.
    model: str = ""
    # An attached the operator grant (#106 stage 4a): base64 of the grant JSON, "" when
    # none. A SIDECAR header on purpose — the body stays the pure literal
    # instruction (instruction_sha256 hashes it whole, no strip-the-fence seam),
    # and grant bytes never enter the body scan. Same absent-tolerated pattern
    # as `model`; not part of body_sha256.
    grant: str = ""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_frontmatter(lines: list[str]) -> dict[str, str]:
    """Frontmatter values stay as raw strings; callers cast at access time.

    Auto-coercing "0000...0" to int 0 would lose 64 leading zeros and surface
    legitimate hash-tampering as a type error rather than as the integrity
    error it actually is — keep values raw and let the schema do the talking.
    """
    result: dict[str, str] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        m = FRONTMATTER_LINE_RE.match(line)
        if not m:
            raise BridgeMalformedError(f"unparseable frontmatter line: {line!r}")
        result[m.group(1)] = m.group(2).strip()
    return result


def _get_int(fm: dict[str, str], key: str) -> int:
    if key not in fm:
        raise BridgeMalformedError(f"frontmatter missing required key: {key}")
    try:
        return int(fm[key])
    except ValueError as e:
        raise BridgeMalformedError(
            f"frontmatter key {key} not an int: {fm[key]!r}"
        ) from e


def _get_str(fm: dict[str, str], key: str) -> str:
    if key not in fm:
        raise BridgeMalformedError(f"frontmatter missing required key: {key}")
    return fm[key]


def _get_optional_str(fm: dict[str, str], key: str) -> str | None:
    if key not in fm:
        raise BridgeMalformedError(f"frontmatter missing required key: {key}")
    value = fm[key]
    if value == "null":
        return None
    return value


def _parse_block(block_lines: list[str], expected_id: int) -> Turn:
    if len(block_lines) < 5:
        raise BridgeMalformedError(f"turn_id={expected_id}: block too short")

    if block_lines[1].rstrip("\n") != FRONTMATTER_DELIM:
        raise BridgeMalformedError(
            f"turn_id={expected_id}: expected '---' on line 2, "
            f"got {block_lines[1]!r}"
        )

    closing = None
    for i in range(2, len(block_lines) - 1):
        if block_lines[i].rstrip("\n") == FRONTMATTER_DELIM:
            closing = i
            break
    if closing is None:
        raise BridgeMalformedError(
            f"turn_id={expected_id}: no closing '---' for frontmatter"
        )

    fm = _parse_frontmatter(block_lines[2:closing])

    fm_turn_id = _get_int(fm, "turn_id")
    if fm_turn_id != expected_id:
        raise BridgeMalformedError(
            f"BEGIN/frontmatter turn_id mismatch: BEGIN={expected_id} fm={fm_turn_id}"
        )
    body_encoding = _get_str(fm, "body_encoding")
    if body_encoding != "base64":
        raise BridgeMalformedError(
            f"unsupported body_encoding: {body_encoding!r}"
        )
    body_sha256 = _get_str(fm, "body_sha256")

    body_lines = block_lines[closing + 1:-1]
    if not body_lines:
        raise BridgeMalformedError(f"turn_id={expected_id}: no body line")
    b64_clean = "".join(line.strip() for line in body_lines)
    try:
        body_bytes = base64.b64decode(b64_clean, validate=True)
    except Exception as e:
        raise BridgeMalformedError(
            f"turn_id={expected_id}: body not valid base64: {e}"
        ) from e

    computed = hashlib.sha256(body_bytes).hexdigest()
    if computed != body_sha256:
        raise BridgeIntegrityError(
            f"turn_id={expected_id}: body_sha256 mismatch "
            f"(frontmatter={body_sha256}, computed={computed})"
        )

    return Turn(
        turn_id=fm_turn_id,
        ts=_get_str(fm, "ts"),
        role=_get_str(fm, "role"),
        session_id=_get_str(fm, "session_id"),
        revision=_get_int(fm, "revision"),
        consumed_at=_get_optional_str(fm, "consumed_at"),
        body_sha256=body_sha256,
        body=body_bytes.decode("utf-8"),
        # Tolerate absence: turns written before B2 have no model key -> "".
        model=fm.get("model", ""),
        # Tolerate absence: pre-#106 turns have no grant key -> "".
        grant=fm.get("grant", ""),
    )


def parse_turns(file: Path) -> Iterator[Turn]:
    """Yield Turns from a bridge file. Line-by-line; raises on malformed."""
    if not file.exists():
        return
    with open(file, "r", encoding="utf-8") as f:
        state = "between"
        block: list[str] = []
        expected_id: int | None = None
        for line in f:
            if state == "between":
                m = BEGIN_RE.match(line)
                if m:
                    state = "in_block"
                    block = [line]
                    expected_id = int(m.group(1))
            else:
                block.append(line)
                end_m = END_RE.match(line)
                if end_m:
                    if int(end_m.group(1)) != expected_id:
                        raise BridgeMalformedError(
                            f"BEGIN={expected_id} END={end_m.group(1)}"
                        )
                    yield _parse_block(block, expected_id)
                    state = "between"
                    block = []
                    expected_id = None
        if state == "in_block":
            raise BridgeMalformedError(
                f"unterminated block for turn_id={expected_id}"
            )


def make_turn(
    *,
    turn_id: int,
    role: str,
    session_id: str,
    body: str,
    revision: int = 1,
    consumed_at: str | None = None,
    ts: str | None = None,
    model: str = "",
    grant: str = "",
) -> Turn:
    """Construct a Turn, computing body_sha256 from body bytes.

    Empty body and oversized body are rejected here so callers don't have
    to repeat the check before append_turn.
    """
    body_bytes = body.encode("utf-8")
    if not body_bytes:
        raise BridgeEmptyBodyError("body is empty; turn not written")
    if len(body_bytes) > MAX_BODY_BYTES:
        raise BridgeSizeExceededError(
            f"body is {len(body_bytes)} bytes; max is {MAX_BODY_BYTES}; "
            f"trim or split across turns"
        )
    return Turn(
        turn_id=turn_id,
        ts=ts or utcnow_iso(),
        role=role,
        session_id=session_id,
        revision=revision,
        consumed_at=consumed_at,
        body_sha256=hashlib.sha256(body_bytes).hexdigest(),
        body=body,
        model=model,
        grant=grant,
    )


def render_turn(turn: Turn) -> str:
    """Serialize a Turn to a string suitable for appending. Verifies body_sha256."""
    body_bytes = turn.body.encode("utf-8")
    sha = hashlib.sha256(body_bytes).hexdigest()
    if turn.body_sha256 != sha:
        raise BridgeIntegrityError(
            "Turn.body_sha256 doesn't match Turn.body — caller built it wrong"
        )
    b64 = base64.b64encode(body_bytes).decode("ascii")
    consumed_at_field = "null" if turn.consumed_at is None else turn.consumed_at
    # Emit model only when set — a blank turn renders exactly as a pre-B2 turn,
    # so nothing changes for un-stamped turns and the round-trip is stable.
    model_line = f"model: {turn.model}\n" if turn.model else ""
    # Emit grant only when set — same absent-tolerated round-trip as model.
    grant_line = f"grant: {turn.grant}\n" if turn.grant else ""
    return (
        f"<!--BRIDGE-TURN-BEGIN turn_id={turn.turn_id}-->\n"
        f"---\n"
        f"turn_id: {turn.turn_id}\n"
        f"ts: {turn.ts}\n"
        f"role: {turn.role}\n"
        f"session_id: {turn.session_id}\n"
        f"revision: {turn.revision}\n"
        f"consumed_at: {consumed_at_field}\n"
        f"{model_line}"
        f"{grant_line}"
        f"body_encoding: base64\n"
        f"body_sha256: {sha}\n"
        f"---\n"
        f"{b64}\n"
        f"<!--BRIDGE-TURN-END turn_id={turn.turn_id}-->\n"
    )


def _acquire_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_S) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                os.close(fd)
                raise
        if time.monotonic() >= deadline:
            os.close(fd)
            raise BridgeLockTimeout(
                f"could not acquire lock {lock_path} within {timeout}s"
            )
        time.sleep(LOCK_POLL_S)


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_turn(file: Path, turn: Turn, lock_path: Path | None = None) -> None:
    """Append a turn block to file. flock-protected; fsynced before unlock."""
    if lock_path is None:
        lock_path = file.parent / ".lock"
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = render_turn(turn)
    fd = _acquire_lock(lock_path)
    try:
        with open(file, "a", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(file, 0o600)
    finally:
        _release_lock(fd)


def mark_consumed(
    file: Path,
    turn_id: int,
    consumed_at: str | None = None,
    revision: int | None = None,
    lock_path: Path | None = None,
) -> bool:
    """In-place edit: set consumed_at on a (turn_id, revision) block.

    Anchored on the BEGIN comment; only the consumed_at line is rewritten.
    Body bytes and body_sha256 are untouched, so integrity stays intact.

    If revision is None, the highest revision matching turn_id is updated.
    Returns True if a block was modified, False if no matching unconsumed
    block found.
    """
    if lock_path is None:
        lock_path = file.parent / ".lock"
    if consumed_at is None:
        consumed_at = utcnow_iso()

    fd = _acquire_lock(lock_path)
    try:
        with open(file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        candidates: list[dict] = []
        i = 0
        while i < len(lines):
            m = BEGIN_RE.match(lines[i])
            if not m or int(m.group(1)) != turn_id:
                i += 1
                continue
            begin_idx = i
            # frontmatter opens at begin_idx+1, closes at next "---"
            if begin_idx + 1 >= len(lines) or lines[begin_idx + 1].rstrip("\n") != FRONTMATTER_DELIM:
                i += 1
                continue
            fm_close = None
            for j in range(begin_idx + 2, len(lines)):
                if lines[j].rstrip("\n") == FRONTMATTER_DELIM:
                    fm_close = j
                    break
            if fm_close is None:
                break
            block: dict = {"begin": begin_idx, "fm_close": fm_close}
            for j in range(begin_idx + 2, fm_close):
                fm_match = FRONTMATTER_LINE_RE.match(lines[j].rstrip("\n"))
                if not fm_match:
                    continue
                key, value = fm_match.group(1), fm_match.group(2).strip()
                if key == "revision":
                    block["revision"] = int(value)
                elif key == "consumed_at":
                    block["consumed_at_idx"] = j
                    block["consumed_at_value"] = value
            candidates.append(block)
            i = fm_close + 1

        unconsumed = [b for b in candidates if b.get("consumed_at_value") == "null"]
        if not unconsumed:
            return False
        if revision is None:
            target = max(unconsumed, key=lambda b: b.get("revision", 0))
        else:
            target = next((b for b in unconsumed if b.get("revision") == revision), None)
            if target is None:
                return False
        if "consumed_at_idx" not in target:
            return False

        lines[target["consumed_at_idx"]] = f"consumed_at: {consumed_at}\n"

        tmp = file.with_suffix(file.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, file)
        return True
    finally:
        _release_lock(fd)


def remove_turn_block(
    file: Path,
    turn_id: int,
    lock_path: Path | None = None,
) -> int:
    """In-place edit: remove ALL blocks whose BEGIN comment carries `turn_id`.

    Used by the `quarantine clear` operator path: when a quarantine artifact
    is dropped, the source body that triggered it must be removed too — naïve
    "rm quarantine + re-run /maw:bridge-in" would re-quarantine because the
    source body still matches. KD9-preserving cleanup, not an ingest bypass.

    Removes the full block including BEGIN comment, frontmatter, body, and
    END comment. Lines between blocks (blank-line separators) are preserved.
    All revisions of the turn_id are dropped — the dirty source body must be
    gone regardless of which revision triggered the scan.

    Returns the number of blocks removed. Mirrors `mark_consumed`'s flock +
    atomic-replace pattern so a concurrent reader never sees a half-edited
    source file.
    """
    if lock_path is None:
        lock_path = file.parent / ".lock"

    fd = _acquire_lock(lock_path)
    try:
        if not file.exists():
            return 0
        with open(file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Walk line-by-line; identify [begin, end] ranges (inclusive) to drop.
        drop_ranges: list[tuple[int, int]] = []
        i = 0
        while i < len(lines):
            m = BEGIN_RE.match(lines[i])
            if not m:
                i += 1
                continue
            block_turn_id = int(m.group(1))
            begin_idx = i
            end_idx: int | None = None
            for j in range(begin_idx + 1, len(lines)):
                end_m = END_RE.match(lines[j])
                if end_m and int(end_m.group(1)) == block_turn_id:
                    end_idx = j
                    break
            if end_idx is None:
                # Malformed source — leave untouched, surface via parse_turns
                raise BridgeMalformedError(
                    f"unterminated block for turn_id={block_turn_id}"
                )
            if block_turn_id == turn_id:
                drop_ranges.append((begin_idx, end_idx))
            i = end_idx + 1

        if not drop_ranges:
            return 0

        keep: list[str] = []
        cursor = 0
        for begin_idx, end_idx in drop_ranges:
            keep.extend(lines[cursor:begin_idx])
            cursor = end_idx + 1
        keep.extend(lines[cursor:])

        tmp = file.with_suffix(file.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, file)
        return len(drop_ranges)
    finally:
        _release_lock(fd)


def find_unconsumed_latest(
    file: Path,
    skip_turn_ids: set[int] | None = None,
) -> Turn | None:
    """Return the highest-turn_id unconsumed turn, picking the highest revision.

    skip_turn_ids: turn_ids the caller wants treated as if consumed (e.g.,
    quarantined turns whose body matched a forbidden pattern).
    """
    skip = skip_turn_ids or set()
    by_id: dict[int, Turn] = {}
    for t in parse_turns(file):
        if t.consumed_at is not None or t.turn_id in skip:
            continue
        prior = by_id.get(t.turn_id)
        if prior is None or t.revision > prior.revision:
            by_id[t.turn_id] = t
    if not by_id:
        return None
    return by_id[max(by_id)]


def find_unconsumed_oldest(
    file: Path,
    skip_turn_ids: set[int] | None = None,
) -> Turn | None:
    """Return the lowest-turn_id unconsumed turn, picking the highest revision.

    FIFO counterpart to find_unconsumed_latest. Default selector for cmd_in,
    cmd_watch, and cmd_peek_next as of the Q1 2026-05-13 closeout — matches
    the natural "I want to read what they sent me first" mental model when
    multiple turns are pending. find_unconsumed_latest remains available via
    `--latest` on the subcommands where it makes sense.

    skip_turn_ids: turn_ids the caller wants treated as if consumed (e.g.,
    quarantined turns whose body matched a forbidden pattern).
    """
    skip = skip_turn_ids or set()
    by_id: dict[int, Turn] = {}
    for t in parse_turns(file):
        if t.consumed_at is not None or t.turn_id in skip:
            continue
        prior = by_id.get(t.turn_id)
        if prior is None or t.revision > prior.revision:
            by_id[t.turn_id] = t
    if not by_id:
        return None
    return by_id[min(by_id)]


__all__ = [
    "BridgeError",
    "BridgeMalformedError",
    "BridgeIntegrityError",
    "BridgeSizeExceededError",
    "BridgeEmptyBodyError",
    "BridgeLockTimeout",
    "Turn",
    "MAX_BODY_BYTES",
    "make_turn",
    "render_turn",
    "parse_turns",
    "append_turn",
    "mark_consumed",
    "remove_turn_block",
    "find_unconsumed_latest",
    "find_unconsumed_oldest",
    "utcnow_iso",
]
