#!/usr/bin/env python3
"""budget-guard.py — PreToolUse on `*`. Per PRD §4.5 + D22 + §11.5.

Tool-call-count governance (NOT tokens — Anthropic doesn't expose token
spend to hooks). Tracks per-session call count in
`.claude/.runtime/spend-<session_id>.json` via atomic-rename writes.

Band-based warning emission (PRD §11.5, v0.5). Bands are derived from
SOFT_LIMIT and HARD_LIMIT and the two intermediate warn points; for the
substrate baseline (600 / 1500) those bands are 600/900/1200/1500. Target
repos override SOFT_LIMIT / HARD_LIMIT via `{{budget_soft}}` /
`{{budget_hard}}` at /maw:init time; the intermediate warn-band derivation
below still produces a sensible four-band rhythm for any reasonable pair.

Band crossings emit one warning line; subsequent calls in the same band
are silent. The hard-limit band emits + blocks every call — that's the
structural backstop, not rate-limited.

Substrate baseline calibrated 2026-05-06 from observed Phase 2-4 session
data (~280 calls across 4-5 hours of legitimate phase work under 1M
context). Pre-1M placeholders were 200/800 — too tight for 1M-context
session norms. Pre-§11.5 implementation emitted on EVERY call past
SOFT — high noise. Now one line per band crossing.

Session ID source (priority order):
  1. event["session_id"] from Claude Code hook input
  2. CLAUDE_SESSION_ID env
  3. "default" (graceful degrade — single shared file)
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile

SOFT_LIMIT = {{budget_soft}}
HARD_LIMIT = {{budget_hard}}
# Intermediate warn bands are evenly distributed between SOFT and HARD.
# For the substrate 600/1500 default that yields 900 and 1200; for other
# pairs it stays proportional. Bands fall back to (SOFT,) if SOFT==HARD.
if HARD_LIMIT > SOFT_LIMIT:
    _step = max((HARD_LIMIT - SOFT_LIMIT) // 3, 1)
    WARN_BANDS = (SOFT_LIMIT, SOFT_LIMIT + _step, SOFT_LIMIT + 2 * _step)
else:
    WARN_BANDS = (SOFT_LIMIT,)
SAFE_BAND = "safe"

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RUNTIME_DIR = os.path.join(REPO, ".claude", ".runtime")


def session_id(event: dict) -> str:
    sid = event.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "default"
    # Sanitize to filesystem-safe.
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))[:64]


def compute_band(calls: int):
    """Return the band label for a given call count.

    Returns "safe" (str) for < SOFT_LIMIT, an int (one of WARN_BANDS) for
    warn bands, and HARD_LIMIT (int) at or above the hard limit.
    """
    if calls >= HARD_LIMIT:
        return HARD_LIMIT
    # Walk WARN_BANDS in reverse so the highest band that fits wins.
    for band in reversed(WARN_BANDS):
        if calls >= band:
            return band
    return SAFE_BAND


def atomic_write(path: str, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix="spend.tmp.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    sid = session_id(event)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    state_path = os.path.join(RUNTIME_DIR, f"spend-{sid}.json")

    try:
        with open(state_path) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("not a dict")
    except Exception:
        state = {
            "calls": 0,
            "soft_limit": SOFT_LIMIT,
            "hard_limit": HARD_LIMIT,
            "started_at": datetime.datetime.now().isoformat(),
        }

    state["calls"] = int(state.get("calls", 0)) + 1
    state["last_at"] = datetime.datetime.now().isoformat()

    calls = state["calls"]
    new_band = compute_band(calls)
    # Pre-migration state files (Phase 1b) lack last_emitted_band; treat
    # as None so the first warn-band crossing in a long-running session
    # still emits exactly once.
    prev_band = state.get("last_emitted_band")

    state["last_emitted_band"] = new_band

    try:
        atomic_write(state_path, state)
    except Exception:
        # If we can't write state, don't block — degrade gracefully.
        # Still emit/block according to the computed band so the operator
        # isn't silently past the hard limit.
        pass

    if new_band == HARD_LIMIT:
        sys.stderr.write(
            f"budget-guard: BLOCKED — {calls} tool calls ≥ {HARD_LIMIT} hard limit (session {sid}).\n"
        )
        return 2

    if new_band in WARN_BANDS and new_band != prev_band:
        sys.stderr.write(
            f"budget-guard: WARN — {calls} tool calls crossed {new_band} band "
            f"(session {sid}; hard {HARD_LIMIT}).\n"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
