"""Capability detection schema + helpers for /maw:init (PRD §4.1.1, KD9).

Capabilities are tools that maw substrate may need: deferred tools like
CronCreate and PushNotification (v0.6 detected + branched), and v0.7-ready
stubs like agent teams, managed agents, RemoteTrigger (detected but NOT
branched in v0.6 per KD9 — capability detection is in v0.6, branching the
implementation paths is v0.7).

This module provides the schema + read/write helpers ONLY. Probes happen
inside the /maw:init slash command (Claude calls the deferred tool with a
no-op arg, observes success/failure) — Python doesn't run probes itself,
since the deferred tools live in the Claude Code harness, not in Python's
import space.

Phase 1c (when /maw:init lands) wires:
  - probe execution inside the slash command's Claude prose
  - smoke test execution (PushNotification quick smoke ~1s; CronCreate
    smoke opt-in via --validate per PRD §4.1.1.1)
  - merging the to_config_block() output into config.yaml
  - calling write_failures_file() if any probe fails
  - /maw:status reading the capabilities back via the Phase 1c YAML reader
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from init.state import write_atomic_text

# Names per PRD §4.1.1. Order matters for to_config_block (YAML field order
# in the rendered block matches PRD §3.2 schema).
V06_CAPABILITIES = ("cron_create", "push_notification")
V07_STUB_CAPABILITIES = ("agent_teams", "managed_agents", "remote_trigger")
ALL_CAPABILITIES = V06_CAPABILITIES + V07_STUB_CAPABILITIES

# capability-failures.json schema version. v2 (PRD §3.2.4) adds schema_version +
# a full `results` map + per-result cleanup_error. v1 files have NO
# schema_version key and only a `failures` array — the reader infers v1 from the
# ABSENCE of schema_version and reconstructs the failure set.
SCHEMA_VERSION = 2

# Canonical runtime location (target-repo-local, gitignored). The native
# SessionStart prompt-hook (Phase B) writes it; read_capability_state reads it.
RUNTIME_FILE = ".maw/.runtime/capability-failures.json"


class CapabilityError(Exception):
    """Base class for capability errors."""


class UnknownCapabilityError(CapabilityError):
    """record() called with a name not in ALL_CAPABILITIES."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CapabilityResult:
    name: str
    probe_passed: bool
    smoke_passed: bool | None = None  # None = smoke skipped (e.g., --validate not passed)
    error: str | None = None
    timestamp: str | None = None
    # Set when a cleanup step (CronDelete after CronCreate, TeamDelete after
    # TeamCreate) fails AFTER the parent probe succeeded. The parent stays
    # probe_passed=True and this is NOT counted as a failure — it's a forensic
    # breadcrumb (contract §7). None = clean / no cleanup attempted.
    cleanup_error: str | None = None


@dataclass
class CapabilityRegistry:
    """Mutable registry of probe + smoke results, populated during /maw:init."""

    results: dict[str, CapabilityResult] = field(default_factory=dict)

    def record(
        self,
        name: str,
        probe_passed: bool,
        smoke_passed: bool | None = None,
        error: str | None = None,
        cleanup_error: str | None = None,
    ) -> None:
        if name not in ALL_CAPABILITIES:
            raise UnknownCapabilityError(
                f"unknown capability: {name!r}. Known: {sorted(ALL_CAPABILITIES)}"
            )
        self.results[name] = CapabilityResult(
            name=name,
            probe_passed=probe_passed,
            smoke_passed=smoke_passed,
            error=error,
            timestamp=_utcnow_iso(),
            cleanup_error=cleanup_error,
        )

    def _probe_value(self, name: str) -> bool:
        # Default to False if the capability wasn't probed at all — fail-closed
        # (the substrate treats absent capability as unavailable, which is the
        # safe assumption for `/maw:init --upgrade` flows where a new capability lands
        # in the schema before the existing init writes it).
        result = self.results.get(name)
        return bool(result.probe_passed) if result else False

    def to_config_block(self) -> dict[str, dict[str, bool]]:
        """Produce the dict structure that gets serialized into config.yaml.

        Returns:
          {
            "capabilities": {"cron_create": bool, "push_notification": bool},
            "capabilities_v07_stubs": {"agent_teams": bool, "managed_agents": bool,
                                       "remote_trigger": bool},
          }
        """
        return {
            "capabilities": {n: self._probe_value(n) for n in V06_CAPABILITIES},
            "capabilities_v07_stubs": {n: self._probe_value(n) for n in V07_STUB_CAPABILITIES},
        }

    def to_yaml_text(self) -> str:
        """Render the capabilities + capabilities_v07_stubs blocks as YAML text.

        Output is suitable for substitution into a profile template via the
        {{capabilities_block}} placeholder. Format mirrors PRD §3.2 example:
        2-space indent, lowercase-true/false, no quotes around boolean values.
        """
        block = self.to_config_block()
        lines = ["capabilities:"]
        for name, value in block["capabilities"].items():
            lines.append(f"  {name}: {'true' if value else 'false'}")
        lines.append("")
        lines.append("# v0.7-ready stubs (detected but not branched in v0.6 per KD9):")
        lines.append("capabilities_v07_stubs:")
        for name, value in block["capabilities_v07_stubs"].items():
            lines.append(f"  {name}: {'true' if value else 'false'}")
        return "\n".join(lines)

    def failures(self) -> list[CapabilityResult]:
        """Results where probe failed OR smoke ran-and-failed.

        Smoke = None (skipped) is NOT a failure — it just means the smoke
        wasn't requested. Smoke = False is a failure (smoke ran and the tool
        misbehaved).
        """
        return [
            r for r in self.results.values()
            if not r.probe_passed or r.smoke_passed is False
        ]


def write_failures_file(path: Path, registry: CapabilityRegistry) -> None:
    """Write capability-failures.json (schema v2) for /maw:status + banner.

    Path is typically <repo>/.maw/.runtime/capability-failures.json. Always
    overwrites (the file is a snapshot of the most recent probe's detection
    state, not an append-log).

    Schema v2 (PRD §3.2.4) emits:
      - schema_version: 2
      - written_at: when this snapshot was written
      - results: full per-capability map (passed/smoke/error/timestamp/cleanup)
      - failures: the failure SUBSET, retained for back-compat readers

    The filename keeps "failures" for path-stability with v1 readers; the
    content is now a superset.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Re-assert 0o700 — mkdir's mode is ignored when the dir already exists,
    # and this dir holds capability state that should stay owner-only.
    path.parent.chmod(0o700)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "written_at": _utcnow_iso(),
        "results": {name: asdict(r) for name, r in registry.results.items()},
        "failures": [asdict(r) for r in registry.failures()],
    }
    # Atomic write (temp + fsync + rename), not write_text: two session-starts
    # can probe concurrently, and a reader mid-write would otherwise see a
    # torn JSON and misclassify a *good* probe as malformed. Mirrors the
    # discipline already used by init.state.write_atomic_text.
    write_atomic_text(path, json.dumps(payload, indent=2))
    path.chmod(0o600)


_RESULT_FIELDS = ("name", "probe_passed", "smoke_passed", "error", "timestamp", "cleanup_error")


def _result_from_dict(name: str, data: dict) -> CapabilityResult:
    """Reconstruct a CapabilityResult from a stored dict, tolerating extra/missing keys."""
    if not isinstance(data, dict):
        raise CapabilityError(f"capability result for {name!r} is not an object")
    if "probe_passed" not in data:
        raise CapabilityError(f"capability result for {name!r} missing probe_passed")
    return CapabilityResult(
        name=data.get("name", name),
        probe_passed=bool(data["probe_passed"]),
        smoke_passed=data.get("smoke_passed"),
        error=data.get("error"),
        timestamp=data.get("timestamp"),
        cleanup_error=data.get("cleanup_error"),
    )


def read_capability_state(repo_root: Path) -> CapabilityRegistry | None:
    """Read .maw/.runtime/capability-failures.json into a CapabilityRegistry.

    Returns:
      - None  if the file is missing or unreadable (= "never probed"; callers
        distinguish this from "probed but failed").
      - a populated CapabilityRegistry for a well-formed v1 or v2 file.

    Raises:
      - CapabilityError on a present-but-malformed file, so callers can tell
        "never probed" (None) apart from "probed but corrupt" (raises).

    Version handling:
      - schema_version == 2  → populate from the full `results` map.
      - schema_version ABSENT → infer v1; populate from the `failures` array
        only (v1 stored failures only, so successful probes are unrecoverable —
        absent capabilities read as unavailable via the fail-closed default).
      - schema_version present but != 2 → CapabilityError (unsupported version).
    """
    path = repo_root / RUNTIME_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CapabilityError(f"capability-failures.json is malformed: {e!s}") from e
    if not isinstance(payload, dict):
        raise CapabilityError("capability-failures.json malformed: expected an object at root")

    registry = CapabilityRegistry()

    if "schema_version" not in payload:
        # v1 inference: failures-only.
        failures = payload.get("failures", [])
        if not isinstance(failures, list):
            raise CapabilityError("v1 capability-failures.json: 'failures' must be an array")
        for entry in failures:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not name:
                raise CapabilityError("v1 capability-failures.json: failure entry missing name")
            registry.results[name] = _result_from_dict(name, entry)
        return registry

    version = payload["schema_version"]
    if version != SCHEMA_VERSION:
        raise CapabilityError(
            f"unsupported capability-failures.json schema_version: {version!r} "
            f"(this build reads v{SCHEMA_VERSION} and v1-by-absence)"
        )

    results = payload.get("results", {})
    if not isinstance(results, dict):
        raise CapabilityError("v2 capability-failures.json: 'results' must be an object")
    for name, data in results.items():
        registry.results[name] = _result_from_dict(name, data)
    return registry


__all__ = [
    "V06_CAPABILITIES",
    "V07_STUB_CAPABILITIES",
    "ALL_CAPABILITIES",
    "SCHEMA_VERSION",
    "RUNTIME_FILE",
    "CapabilityError",
    "UnknownCapabilityError",
    "CapabilityResult",
    "CapabilityRegistry",
    "write_failures_file",
    "read_capability_state",
]
