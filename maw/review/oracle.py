"""Oracle-review harness — verifying correctness when the oracle is not
reproducible in a clean worktree (backlog item 2).

The advisor playbook's keystone gate — "reproduce at the SHA in a clean
worktree" — structurally breaks when correctness depends on something the
reviewer cannot reproduce in a worktree: rendered UI, an external service, a
hosted vector store whose corpus isn't even local. In those cases the reviewer
ends up signing a verdict on the *builder's* evidence, which is exactly the
trust failure the review exists to prevent (a slug silently rendering the wrong
layer; an embed host returning a guide page instead of a map).

What actually worked across the arcs was a per-review reinvention of one
technique. This module makes it supported tooling instead of a hack. Three
named legs:

  probe    — hit the LIVE oracle. Needs credentials/access. The honest path
             when you can reach the real thing.
  mock     — a deterministic stand-in. The ONLY path for CI and for a
             credential-isolated / unattended reviewer that cannot probe. The
             live hole is then acknowledged, not closed.
  snapshot — record the captured state so a human can diff it across runs and
             across SHAs. Deterministic, scriptable, CI-able, and grant-free
             (no Accessibility / Screen-Recording grant required).

A state-forcing harness (force the default tab / a dead host / a specific
layer / a revoked permission, then capture headlessly) is target-specific
code; its CAPTURED OUTPUT — text, JSON, a rendered string — flows into the
snapshot/probe legs here. Screenshot-as-evidence is deliberately NOT a leg:
it is the builder choosing what to show ("trust me, with a picture"), and it
is defeated by the stale-artifact hazard (item 3). Snapshot a deterministic
capture instead.

Stdlib-only; no network, no third-party deps. The probe function is supplied
by the caller, so the live leg stays the caller's concern and this module
stays testable offline.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class OracleError(Exception):
    pass


def _normalize(value: object) -> str:
    """Canonical text form of a captured value, for stable snapshots.

    str passes through (newline-normalized, trailing newline ensured); anything
    else is rendered as sorted-key, indented JSON so dict ordering never causes
    a spurious diff.
    """
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


# --------------------------------------------------------------------------
# probe / mock
# --------------------------------------------------------------------------

@dataclass
class ProbeResult:
    value: object
    source: str  # "probe" (live) | "mock" (stand-in)
    ok: bool
    detail: str

    @property
    def live_hole(self) -> bool:
        """True when we could not get live truth — evidence is mock-only.

        Surfaced so a verdict written on it says so plainly rather than
        implying the live oracle was checked.
        """
        return self.source != "probe"


def probe_or_mock(
    probe: Callable[[], object],
    mock: Callable[[], object],
    *,
    live: bool,
) -> ProbeResult:
    """Run the live probe when allowed, else the mock — and ALWAYS report which.

    `live` is the credential/access decision: a credential-isolated or
    unattended reviewer passes live=False and gets the mock path with the live
    hole acknowledged. When live=True but the probe raises (host down, auth
    failed), we fall back to the mock but mark ok=False and source="mock" so
    the fallback is never mistaken for a confirmed live result.
    """
    if not live:
        return ProbeResult(
            value=mock(),
            source="mock",
            ok=True,
            detail="credential-isolated/CI: mock path (live oracle NOT probed)",
        )
    try:
        return ProbeResult(value=probe(), source="probe", ok=True, detail="live probe ok")
    except Exception as e:  # noqa: BLE001 — any probe failure is a live hole
        return ProbeResult(
            value=mock(),
            source="mock",
            ok=False,
            detail=f"live probe FAILED ({e!r}); fell back to mock — LIVE HOLE",
        )


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

@dataclass
class SnapshotResult:
    name: str
    status: str  # "match" | "new" | "changed"
    diff: str    # unified diff when changed, else ""
    path: Path

    @property
    def ok(self) -> bool:
        # "new" is not a pass: a freshly recorded baseline has not been
        # human-reviewed yet, so it must not silently count as verified.
        return self.status == "match"


def snapshot_path(name: str, base_dir: Path) -> Path:
    safe = name.replace("/", "_")
    return base_dir / f"{safe}.snap"


def verify(
    name: str,
    value: object,
    base_dir: Path,
    *,
    update: bool = False,
) -> SnapshotResult:
    """Compare a captured value to its recorded snapshot.

    - no recorded snapshot → record it, status "new" (NOT a pass — a new
      baseline is unreviewed).
    - recorded and equal → status "match".
    - recorded and differs → status "changed" with a unified diff; the stored
      snapshot is left intact unless update=True (then it is rewritten and the
      diff is still returned so the change is visible).
    """
    path = snapshot_path(name, base_dir)
    current = _normalize(value)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current, encoding="utf-8")
        return SnapshotResult(name, "new", "", path)
    recorded = path.read_text(encoding="utf-8")
    if recorded == current:
        return SnapshotResult(name, "match", "", path)
    diff = "".join(
        difflib.unified_diff(
            recorded.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"{name} (recorded)",
            tofile=f"{name} (current)",
        )
    )
    if update:
        path.write_text(current, encoding="utf-8")
    return SnapshotResult(name, "changed", diff, path)


# --------------------------------------------------------------------------
# CLI — `python3 -m review.oracle snapshot --name X --file capture.txt --dir snaps`
# --------------------------------------------------------------------------

def cmd_snapshot(args: argparse.Namespace) -> int:
    try:
        value = Path(args.file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"oracle snapshot failed: {e}", file=sys.stderr)
        return 1
    res = verify(args.name, value, Path(args.dir), update=args.update)
    if res.status == "new":
        # A fresh baseline is recorded but UNREVIEWED — SnapshotResult.ok is
        # False for exactly this reason. Default exit 0 is a local-recording
        # convenience; under --strict/--ci it must be NON-zero so an unreviewed
        # baseline cannot silently pass a CI gate (the 'unreviewed masquerading
        # as verified' failure this module exists to prevent). Advisor note.
        print(f"NEW baseline recorded for {res.name!r} at {res.path} "
              f"(unreviewed — confirm it is correct, then commit it)"
              + ("  [--strict: NOT a pass]" if args.strict else ""))
        return 3 if args.strict else 0
    if res.status == "match":
        print(f"MATCH {res.name!r}")
        return 0
    print(f"CHANGED {res.name!r} — capture differs from recorded snapshot:")
    print(res.diff, end="")
    if args.update:
        print(f"(snapshot updated at {res.path})")
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review.oracle", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="verify a captured file against a recorded snapshot")
    snap.add_argument("--name", required=True, help="snapshot identity")
    snap.add_argument("--file", required=True, help="path to the captured output")
    snap.add_argument("--dir", required=True, help="snapshot store directory")
    snap.add_argument("--update", action="store_true", help="rewrite the snapshot to the capture")
    snap.add_argument(
        "--strict", "--ci", action="store_true", dest="strict",
        help="treat a NEW (unreviewed) baseline as a failure (exit 3) — for CI; "
        "default exit 0 is for local first-time recording",
    )
    snap.set_defaults(fn=cmd_snapshot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
