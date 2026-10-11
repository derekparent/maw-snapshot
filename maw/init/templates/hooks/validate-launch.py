#!/usr/bin/env python3
"""validate-launch.py — handoff-schema validator (PRD §3.3 + §4.5).

PreToolUse hook on `Task|Agent`.

Behavior:
  1. Reads PreToolUse event from stdin (JSON).
  2. Returns 0 immediately for non-Task/Agent tools.
  3. Extracts the first JSON-fenced block (```json ... ```), or the first
     balanced top-level JSON object, from `tool_input.prompt`. If no block
     is found, returns 0 (legitimate non-templated launches must pass).
  4. Looks up the schema variant by `kind` (orchestration, live_sweep,
     implementer). Validates required fields, types, and enum constraints
     manually (no jsonschema dep — keeps the substrate zero-dep).
  5. Re-enforces hard-coded invariants in code (defense-in-depth per D28
     — schema file could in theory be edited; invariants are the guard):
       - push_target ∈ {"dev"}
       - recursion_depth_max in [1, 3]
       - fan_out_max in [1, 3]
       - ci_gate === True
       - max_wall_clock_hours in [1, 8]

Exit codes:
  0 = allow
  2 = block (with stderr explanation)

YAML support deferred (PyYAML not in venv). Templates use JSON-fenced
blocks. Document this in template authoring guide for downstream phases.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any


SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "schemas",
    "handoff.json",
)

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
SESSION_LOCK_TTL_SECONDS = 300


def _state_path() -> str:
    """Resolved at call time so MAW_STATE_PATH can override (used by tests)."""
    return os.environ.get("MAW_STATE_PATH") or os.path.join(
        REPO_ROOT, ".maw-lead-state.json"
    )


def emit_block(reason: str) -> int:
    sys.stderr.write(f"validate-launch: BLOCKED — {reason}\n")
    return 2


def extract_json_block(prompt: str) -> dict | None:
    """Find first ```json ... ``` fenced block, or first balanced {...}."""
    # 1. Fenced block: ```json or ```javascript or ```
    fence = re.search(r"```(?:json|javascript|js)?\s*\n(.*?)\n```", prompt, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            return None

    # 2. Inline object: find first balanced top-level {...}
    depth = 0
    start = -1
    for i, c in enumerate(prompt):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = prompt[start : i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict) and "kind" in obj:
                        return obj
                except json.JSONDecodeError:
                    start = -1
                    continue

    return None


# ---------- Session lock ----------

def check_session_lock() -> str | None:
    """Return error message if a recent orchestration lead holds the lock.

    Lock = .maw-lead-state.json with `session_id` field present AND mtime
    within SESSION_LOCK_TTL_SECONDS. Refuses second concurrent orchestration
    spawn at the structural layer. Stale locks (mtime older than TTL) are
    auto-released — no separate cleanup step needed.
    """
    state_path = _state_path()
    if not os.path.exists(state_path):
        return None
    try:
        mtime = os.path.getmtime(state_path)
    except OSError:
        return None
    age = time.time() - mtime
    if age >= SESSION_LOCK_TTL_SECONDS:
        return None
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    sid = state.get("session_id")
    if not sid:
        return None
    return (
        f"session lock active (held by {sid!r}; mtime={age:.0f}s ago, "
        f"TTL={SESSION_LOCK_TTL_SECONDS}s). Run .claude/panic-stop.sh to "
        f"force-clear, or wait for lock to expire."
    )


# ---------- Hard-coded invariants (NEVER trust schema file alone) ----------

def check_invariants_orchestration(data: dict) -> str | None:
    if data.get("push_target") != "dev":
        return f"push_target must be 'dev' (got {data.get('push_target')!r})"
    rdm = data.get("recursion_depth_max")
    if not isinstance(rdm, int) or not 1 <= rdm <= 3:
        return f"recursion_depth_max must be int in [1,3] (got {rdm!r})"
    fom = data.get("fan_out_max", 1)
    if not isinstance(fom, int) or not 1 <= fom <= 3:
        return f"fan_out_max must be int in [1,3] (got {fom!r})"
    if data.get("ci_gate") is not True:
        return f"ci_gate must be true (got {data.get('ci_gate')!r})"
    mwc = data.get("max_wall_clock_hours", 4)
    if not isinstance(mwc, int) or not 1 <= mwc <= 8:
        return f"max_wall_clock_hours must be int in [1,8] (got {mwc!r})"
    return None


# ---------- Per-variant required-field check (lightweight schema) ----------

REQUIRED = {
    "orchestration": {"phase", "template", "halt_conditions", "push_target",
                      "ci_gate", "recursion_depth_max", "fan_out_max"},
    "live_sweep": {"prod_url", "credentials_source", "targets", "output_path"},
    # Implementer must declare an exit_doc_path (structured exit doc per
    # maw-templates/agent-exit.md). Run-time enforcement (file actually
    # written + matches template shape) deferred; the lead enforces at
    # INTEGRATE step in the meantime per the templates' specific refusal
    # clause.
    "implementer": {"unit_id", "worktree_branch_descriptive", "scope",
                    "acceptance", "exit_doc_path"},
}

LIVE_SWEEP_CRED_CONST = "{{live_sweep_cred_const}}"
EXIT_DOC_PATH_RE = re.compile(r"^findings/\.tmp/agent-exits/.+-exit\.md$")


def check_required(kind: str, data: dict) -> str | None:
    required = REQUIRED.get(kind)
    if required is None:
        return f"unknown kind {kind!r} (allowed: {sorted(REQUIRED)})"
    missing = required - set(data)
    if missing:
        return f"missing required fields for kind={kind}: {sorted(missing)}"
    if kind == "live_sweep" and data.get("credentials_source") != LIVE_SWEEP_CRED_CONST:
        return f"live_sweep.credentials_source must be {LIVE_SWEEP_CRED_CONST!r}"
    if kind == "implementer":
        branch = data.get("worktree_branch_descriptive", "")
        if not re.fullmatch(r"refactor/[a-z0-9-]+-[a-z0-9-]{3,40}", branch):
            return f"worktree_branch_descriptive must match refactor/<name>-<unit_id>"
        exit_path = data.get("exit_doc_path", "")
        if not isinstance(exit_path, str) or not EXIT_DOC_PATH_RE.match(exit_path):
            return (
                f"exit_doc_path must match {EXIT_DOC_PATH_RE.pattern!r} "
                f"(got {exit_path!r}); see maw-templates/agent-exit.md"
            )
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = event.get("tool_name", "")
    if tool_name not in ("Task", "Agent"):
        return 0

    prompt = (event.get("tool_input") or {}).get("prompt", "")
    if not isinstance(prompt, str):
        return 0

    data = extract_json_block(prompt)
    if data is None:
        # No structured launch block — pass. Free-form Agent prompts are legitimate.
        return 0

    kind = data.get("kind")
    if not isinstance(kind, str):
        return emit_block("launch block missing 'kind' field")

    err = check_required(kind, data)
    if err:
        return emit_block(err)

    if kind == "orchestration":
        err = check_invariants_orchestration(data)
        if err:
            return emit_block(err)
        err = check_session_lock()
        if err:
            return emit_block(err)

    return 0


if __name__ == "__main__":
    sys.exit(main())
