"""External-oracle pattern helper — probe-live / mock-for-CI / snapshot-for-human
(backlog item 2; executor_ios capability-asymmetry).

Some oracles aren't a rendered UI you can force — they're a remote service whose
truth you can only get by ASKING it: a hosted OpenAI Assistant, a vector store
whose corpus isn't local, a weather embed host. The reviewer's problem is the
capability asymmetry: the builder can reach the live service, a credential-
isolated / CI / unattended reviewer cannot. Signing a verdict on the builder's
say-so is the exact trust failure review exists to prevent.

This helper composes review.oracle into the three honest postures, each driven
by a caller-supplied PRODUCT probe hook:

  probe-live  — hit the real service (needs credentials). The honest answer when
                you can reach it. Optionally snapshot the live answer too.
  mock-for-ci — a deterministic stand-in for CI / the credential-isolated
                reviewer; the live hole is ACKNOWLEDGED (live_hole=True), never
                hidden. This is the ONLY path that runs without credentials.
  snapshot    — record/diff the (live or mock) answer so a human can compare it
                across runs and across SHAs. A "new" baseline is NOT a pass.

REUSABLE here: the posture selection, the live-hole bookkeeping, the snapshot
composition. PRODUCT-SPECIFIC (injected): the `probe` callable that reaches the
real service and the `mock` callable that stands in for it. maw ships neither —
An iOS consumer supplies the OpenAI-Assistant probe + its recorded fixture.

Stdlib-only; no network in this module — the network lives in the caller's probe.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from review import oracle

PROBE = "probe-live"
MOCK = "mock-for-ci"
SNAPSHOT = "snapshot"


class ExternalOracleError(Exception):
    pass


@dataclass
class ExternalOracleResult:
    mode: str
    probe: oracle.ProbeResult
    snapshot: oracle.SnapshotResult | None

    @property
    def live_hole(self) -> bool:
        return self.probe.live_hole

    @property
    def ok(self) -> bool:
        """A pass requires the answer be trustworthy for the chosen mode.

        - probe-live: the live probe must have succeeded (a mock fallback after a
          live failure is ok=False on the ProbeResult — not a pass).
        - mock-for-ci: the mock ran; the live hole is acknowledged, so this is a
          conditional pass (ok=True, live_hole=True — the verdict must SAY mock).
        - snapshot: gated on the snapshot matching AND the probe being honest.
        """
        if self.snapshot is not None:
            return self.snapshot.ok and self.probe.ok
        return self.probe.ok


def check_external(
    *,
    mode: str,
    probe: Callable[[], object],
    mock: Callable[[], object],
    name: str | None = None,
    snapshot_dir: Path | None = None,
    update: bool = False,
) -> ExternalOracleResult:
    """Run the external-oracle check in the requested posture.

    `probe` and `mock` are the PRODUCT hooks. The mode decides whether the live
    probe runs (probe-live) or is deliberately skipped (mock-for-ci); snapshot
    additionally records/diffs the answer and requires name + snapshot_dir.
    """
    if mode not in (PROBE, MOCK, SNAPSHOT):
        raise ExternalOracleError(f"unknown mode {mode!r}: use {PROBE}/{MOCK}/{SNAPSHOT}")

    live = mode != MOCK  # mock-for-ci is the only no-credentials posture
    pres = oracle.probe_or_mock(probe, mock, live=live)

    snap = None
    if mode == SNAPSHOT:
        if name is None or snapshot_dir is None:
            raise ExternalOracleError("snapshot mode requires name + snapshot_dir")
        snap = oracle.verify(name, pres.value, snapshot_dir, update=update)

    return ExternalOracleResult(mode=mode, probe=pres, snapshot=snap)


def render(res: ExternalOracleResult) -> str:
    lines = [f"EXTERNAL ORACLE [{res.mode}]"]
    lines.append(f"  source: {res.probe.source}  ({res.probe.detail})")
    if res.live_hole:
        lines.append("  LIVE HOLE: this verdict was NOT confirmed against the live oracle")
    if res.snapshot is not None:
        lines.append(f"  snapshot: {res.snapshot.status} → {res.snapshot.path}")
        if res.snapshot.status == "changed":
            lines.append(res.snapshot.diff.rstrip("\n"))
    lines.append(f"  verdict: {'PASS' if res.ok else 'FAIL/UNVERIFIED'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI — the probe/mock are PRODUCT argv commands; their STDOUT is the oracle
# answer. (probe-live runs --probe; mock-for-ci runs --mock; snapshot records
# whichever ran.)
#   python3 -m review.external_oracle check --mode mock-for-ci \
#       --probe './scripts/ask-assistant.sh "is asbestos regulated?"' \
#       --mock  'cat fixtures/assistant-asbestos.txt' \
#       [--name asbestos --dir .maw/review-snaps]
# --------------------------------------------------------------------------

def _argv_value(cmd: str):
    import shlex
    import subprocess

    def _fn() -> object:
        cp = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=600)
        if cp.returncode != 0:
            raise ExternalOracleError(f"probe/mock command failed: {cp.stderr.strip()}")
        return cp.stdout
    return _fn


def cmd_check(args: argparse.Namespace) -> int:
    try:
        res = check_external(
            mode=args.mode,
            probe=_argv_value(args.probe),
            mock=_argv_value(args.mock),
            name=args.name,
            snapshot_dir=Path(args.dir) if args.dir else None,
            update=args.update,
        )
    except ExternalOracleError as e:
        print(f"external-oracle failed: {e}", file=sys.stderr)
        return 1
    print(render(res))
    return 0 if res.ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review.external_oracle", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="probe-live / mock-for-ci / snapshot an external oracle (probe is a product hook)")
    c.add_argument("--mode", required=True, choices=[PROBE, MOCK, SNAPSHOT])
    c.add_argument("--probe", required=True, help="PRODUCT live-probe command (argv string)")
    c.add_argument("--mock", required=True, help="PRODUCT mock/fixture command (argv string)")
    c.add_argument("--name", default=None, help="snapshot identity (snapshot mode)")
    c.add_argument("--dir", default=None, help="snapshot store directory (snapshot mode)")
    c.add_argument("--update", action="store_true", help="rewrite the snapshot to the answer")
    c.set_defaults(fn=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
