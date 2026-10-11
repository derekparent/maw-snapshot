"""Capability probe runner plumbing (Phase 1c-part-3, contract 2026-05-13).

Deferred tools (CronCreate, PushNotification, TeamCreate, RemoteTrigger)
live in the Claude Code harness — Python cannot invoke them. So the probe
itself is performed by Claude *in-session*, instructed by a SessionStart
hook. This module is everything around that prose step, kept deterministic:

  gate   — decide whether a probe is due (config present? runtime file
           missing/stale/malformed? MAW_REPROBE forced?) and, when due,
           emit the probe instruction prompt on stdout. The SessionStart
           hook forwards that stdout into the session as context. Silent
           when nothing is due — zero token cost on a fresh state.

  write  — accept the probe results (JSON on stdin) from the in-session
           probe, validate against the capability schema, and write the
           canonical schema-v2 runtime file via capabilities.write_failures_file
           plus a probe-log.jsonl audit line per result. Malformed input
           goes to probe-errors.jsonl and the runtime file is NOT touched
           (contract §7) — the next session re-probes.

Cadence (contract §4): TTL default 24h, knob `capability_probe_ttl_hours`
in .maw/config.yaml, MAW_REPROBE=1 env forces. The managed_agents /
remote_trigger pair collapses to one RemoteTrigger probe reported under
both names (contract §9 Q1/Q3 resolution).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from init.capabilities import (
    ALL_CAPABILITIES,
    SCHEMA_VERSION,
    RUNTIME_FILE,
    CapabilityError,
    CapabilityRegistry,
    UnknownCapabilityError,
    read_capability_state,
    write_failures_file,
)

DEFAULT_TTL_HOURS = 24
CONFIG_FILE = ".maw/config.yaml"
PROBE_LOG = ".maw/.runtime/probe-log.jsonl"
PROBE_ERRORS = ".maw/.runtime/probe-errors.jsonl"

TTL_KNOB_RE = re.compile(r"(?m)^capability_probe_ttl_hours:\s*(\d+)\s*(?:#.*)?$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


def read_ttl_hours(repo_root: Path) -> int:
    """TTL knob from .maw/config.yaml; DEFAULT_TTL_HOURS when absent/bad."""
    try:
        text = (repo_root / CONFIG_FILE).read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_TTL_HOURS
    m = TTL_KNOB_RE.search(text)
    return int(m.group(1)) if m else DEFAULT_TTL_HOURS


def read_written_at(repo_root: Path) -> datetime | None:
    """The runtime snapshot's written_at, or None when missing/unparseable.

    Deliberately tolerant: any defect here just means "treat as stale",
    and the gate's malformed-payload arm handles corrupt JSON explicitly.
    """
    path = repo_root / RUNTIME_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _parse_iso(payload.get("written_at", ""))


def probe_state(repo_root: Path, now: datetime | None = None) -> str:
    """Classify the runtime snapshot: 'missing' | 'malformed' | 'stale' |
    'version-mismatch' | 'fresh'. Pure function of disk state; `now` is
    injectable for tests."""
    path = repo_root / RUNTIME_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return "missing"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "malformed"
    if not isinstance(payload, dict):
        return "malformed"
    if payload.get("schema_version") != SCHEMA_VERSION:
        return "version-mismatch"
    written = _parse_iso(payload.get("written_at", ""))
    if written is None:
        return "malformed"
    # Envelope alone isn't enough: a valid schema_version + written_at over
    # garbage results would read "fresh" here while every reader raises —
    # a TTL-long wedge where the gate refuses to re-probe a snapshot nobody
    # can use. Run the same validator the readers run.
    try:
        read_capability_state(repo_root)
    except CapabilityError:
        return "malformed"
    age_hours = ((now or _utcnow()) - written).total_seconds() / 3600
    if age_hours < 0:
        # Future written_at (clock jump, NTP correction, planted file) would
        # otherwise read as fresher-than-fresh and suppress probing forever.
        return "stale"
    return "fresh" if age_hours <= read_ttl_hours(repo_root) else "stale"


def probe_due(
    repo_root: Path,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """(due?, reason). Not a maw repo → never due. MAW_REPROBE=1 → always."""
    if not (repo_root / CONFIG_FILE).is_file():
        return False, "no-maw-config"
    environ = os.environ if env is None else env
    if environ.get("MAW_REPROBE") == "1":
        return True, "forced (MAW_REPROBE=1)"
    state = probe_state(repo_root, now=now)
    if state == "fresh":
        return False, "fresh"
    return True, state


def probe_prompt(repo_root: Path, plugin_root: str) -> str:
    """The instruction text injected into the session when a probe is due.

    Both paths land inside a shell command Claude is told to run, and the
    repo path also appears as display text in an *instruction* — so both
    are shell-quoted and the display copy is repr-quoted. A path with
    spaces/metacharacters must not split argv, and a crafted path must
    read as data, not as instruction text.
    """
    write_cmd = (
        f"{shlex.quote(plugin_root + '/bin/maw-run')} init.probe write "
        f"--repo-root {shlex.quote(str(repo_root))}"
    )
    return f"""[maw] Capability probe due for this repo ({str(repo_root)!r}).
Run it now, before the user's work; it takes a few tool calls and must not
page anyone or leave artifacts.

For each capability, attempt the probe and record probe_passed + a short
error string on failure. The tools are DEFERRED — load each via ToolSearch
first; if ToolSearch finds no match, that capability is probe_passed=false
with error "tool not in surface".

1. cron_create — CronCreate with a minimal prompt and recurring: false (a
   one-shot). It still returns a job id, so CronDelete that job; a failed
   cleanup goes in cleanup_error (probe_passed stays true).
2. push_notification — probe availability by ToolSearch surface presence ONLY
   (same as remote_trigger below). Found in the tool surface →
   probe_passed=true; ToolSearch finds no match → probe_passed=false, error
   "tool not in surface". Do NOT invoke PushNotification during the probe and do
   NOT send a real page — an invalid probe call renders a red "Invalid tool
   parameters" line to the user every session (#90).
3. agent_teams — TeamCreate(description:"maw probe; ignore") then TeamDelete
   immediately; failed cleanup → cleanup_error.
4. remote_trigger — probe RemoteTrigger availability once (ToolSearch
   surface check is sufficient; do not fire a real trigger). Report the SAME
   result under BOTH "managed_agents" and "remote_trigger".

Then write the results (do not hand-author the runtime file):

  echo '<results-json>' | {write_cmd}

where <results-json> is one object keyed by capability name:
  {{"cron_create": {{"probe_passed": true, "error": null, "cleanup_error": null}}, ...}}

All five names required: cron_create, push_notification, agent_teams,
managed_agents, remote_trigger. Do not run any other tools for this probe;
do not announce the probe to the user beyond a one-line note; continue
with their session normally afterwards."""


def cmd_gate(repo_root: Path, plugin_root: str) -> int:
    """Print the probe prompt iff a probe is due. Always exit 0 (hook-safe)."""
    due, reason = probe_due(repo_root)
    if due:
        sys.stdout.write(probe_prompt(repo_root, plugin_root))
        sys.stdout.write(f"\n[maw] probe reason: {reason}\n")
    return 0


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def cmd_write(repo_root: Path, stdin_text: str) -> int:
    """Validate stdin results and write the canonical schema-v2 snapshot.

    Any defect → one line in probe-errors.jsonl, runtime file untouched,
    exit 1 (contract §7: a bad probe never overwrites a prior good state).
    """
    def reject(msg: str) -> int:
        _append_jsonl(
            repo_root / PROBE_ERRORS,
            {"at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "error": msg},
        )
        sys.stderr.write(f"probe write rejected: {msg}\n")
        return 1

    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError as e:
        return reject(f"stdin is not valid JSON: {e!s}")
    if not isinstance(payload, dict):
        return reject("stdin root must be an object keyed by capability name")

    missing = sorted(set(ALL_CAPABILITIES) - set(payload))
    if missing:
        return reject(f"missing capabilities: {', '.join(missing)}")

    registry = CapabilityRegistry()
    for name, data in payload.items():
        if not isinstance(data, dict) or "probe_passed" not in data:
            return reject(f"result for {name!r} must be an object with probe_passed")
        # JSON booleans only — bool("false") is True, so a coercing write
        # would record an unavailable capability as available.
        if not isinstance(data["probe_passed"], bool):
            return reject(f"result for {name!r}: probe_passed must be a JSON boolean")
        if data.get("smoke_passed") is not None and not isinstance(
            data["smoke_passed"], bool
        ):
            return reject(f"result for {name!r}: smoke_passed must be boolean or null")
        for str_field in ("error", "cleanup_error"):
            if data.get(str_field) is not None and not isinstance(
                data[str_field], str
            ):
                return reject(f"result for {name!r}: {str_field} must be string or null")
        try:
            registry.record(
                name,
                probe_passed=data["probe_passed"],
                smoke_passed=data.get("smoke_passed"),
                error=data.get("error"),
                cleanup_error=data.get("cleanup_error"),
            )
        except UnknownCapabilityError as e:
            return reject(str(e))

    write_failures_file(repo_root / RUNTIME_FILE, registry)
    for name, result in registry.results.items():
        _append_jsonl(
            repo_root / PROBE_LOG,
            {
                "name": name,
                "timestamp": result.timestamp,
                "probe_passed": result.probe_passed,
                "error": result.error,
            },
        )
    sys.stdout.write(
        f"probe state written: {len(registry.results)} capabilities, "
        f"{len(registry.failures())} failure(s)\n"
    )
    return 0


def capabilities_line(repo_root: Path, now: datetime | None = None) -> str:
    """Render the banner's `[2] Capabilities:` payload from probed state.

    Used by /maw:init's banner and /maw:status. Falls back to the honest
    deferred-probe text whenever there is no fresh, well-formed snapshot.
    """
    state = probe_state(repo_root, now=now)
    if state == "missing":
        return (
            "cron_create=deferred-probe push_notification=deferred-probe "
            "(v0.7 stubs: agent_teams, managed_agents, remote_trigger) "
            "— probe runs at next session start"
        )
    if state in ("malformed", "version-mismatch"):
        return f"probe state {state}; will re-probe at next session start"

    try:
        registry = read_capability_state(repo_root)
    except CapabilityError as e:
        return f"probe state unreadable ({e!s}); will re-probe at next session start"
    if registry is None:
        return "cron_create=deferred-probe push_notification=deferred-probe"

    def render(name: str) -> str:
        r = registry.results.get(name)
        if r is None:
            return f"{name}=unprobed"
        if r.probe_passed:
            return f"{name}=available"
        reason = (r.error or "unavailable").strip()
        if len(reason) > 40:
            reason = reason[:37] + "..."
        return f"{name}=unavailable({reason})"

    parts = [render(n) for n in ALL_CAPABILITIES]
    suffix = " [stale; re-probes at next session start]" if state == "stale" else ""
    return " ".join(parts) + suffix


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="init.probe")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gate = sub.add_parser("gate", help="emit probe prompt iff a probe is due")
    p_gate.add_argument("--repo-root", default=".", type=Path)
    p_gate.add_argument(
        "--plugin-root",
        default=os.environ.get("CLAUDE_PLUGIN_ROOT", ""),
        help="path used in the emitted write command (defaults to $CLAUDE_PLUGIN_ROOT)",
    )

    p_write = sub.add_parser("write", help="write probe results from stdin JSON")
    p_write.add_argument("--repo-root", default=".", type=Path)

    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    if args.cmd == "gate":
        return cmd_gate(repo_root, args.plugin_root or str(repo_root / "maw"))
    return cmd_write(repo_root, sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main())
