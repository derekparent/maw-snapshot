"""State-forcing-harness ORCHESTRATOR — compose force → capture → verify
(backlog items 2/3; advisor_ios Q3; gate 11 of the ADVISOR-PLAYBOOK).

The advisor playbook's keystone gate — reproduce at the SHA in a clean worktree
— breaks when correctness lives in an unreproducible oracle (a rendered UI, a
hosted assistant, a privacy-revoked simulator). What worked across the arcs was
a per-review hand-rolled ritual: FORCE the oracle into a known state (the
default tab / a dead embed host / a specific map layer / a revoked permission),
CAPTURE the resulting state HEADLESSLY (deterministic text/JSON, not a
screenshot the builder chose), then VERIFY that capture against a recorded
snapshot. This module makes that ritual reusable tooling.

What is REUSABLE (shipped here) vs PRODUCT-SPECIFIC (injected by the caller):

  REUSABLE  — the orchestration: run force, then capture, then hand the capture
              to review.oracle.verify; stop on the first failing leg; report
              which leg failed and in what order. Deterministic, scriptable,
              grant-free.
  PRODUCT   — the force command and the capture command. They are HOOKS the
              caller supplies. An iOS consumer supplies a Ventusky-embed forcer, a
              forced-default-tab capture, a simctl privacy-revoke. maw ships
              NONE of those — building a product forcer here would be faking
              coverage. See maw/docs/non-reproducible-oracle-review.md.

Optionally gated on build-from-SHA provenance (review.provenance): when an
`expected_sha` is supplied the harness verifies the artifact under test was
built from the SHA under review BEFORE forcing it, so a capture is never trusted
off a stale artifact (item 3 — the stale-DerivedData hazard).

Stdlib-only. The force/capture hooks are caller-supplied callables (or argv
lists run through an injectable runner), so the harness stays testable offline
with no product, no network, and no grants.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from review import oracle, provenance

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
# A hook is either a no-arg callable returning (ok, output_text) or an argv list
# run through the runner (ok = exit 0, output = stdout).
Hook = Callable[[], "tuple[bool, str]"]


class HarnessError(Exception):
    pass


def _run(cmd: Sequence[str], cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=600, cwd=cwd)


def argv_hook(cmd: Sequence[str], runner: Runner = _run, cwd: str | None = None) -> Hook:
    """Adapt a product-supplied argv command into a harness hook.

    ok = exit 0; output = stdout (stderr appended on failure so the diagnosis
    is visible). The command is the PRODUCT's concern — maw ships no forcers.
    """
    def _hook() -> "tuple[bool, str]":
        cp = runner(cmd, cwd)
        out = cp.stdout
        if cp.returncode != 0 and cp.stderr.strip():
            out = f"{out}\n[stderr] {cp.stderr.strip()}"
        return cp.returncode == 0, out
    return _hook


@dataclass
class LegResult:
    name: str          # "provenance" | "force" | "capture" | "verify"
    ok: bool
    detail: str


@dataclass
class HarnessResult:
    ok: bool
    legs: list[LegResult]
    snapshot: oracle.SnapshotResult | None

    @property
    def failed_leg(self) -> str | None:
        for leg in self.legs:
            if not leg.ok:
                return leg.name
        return None

    def render(self) -> str:
        lines = []
        for leg in self.legs:
            mark = "ok  " if leg.ok else "FAIL"
            lines.append(f"  [{mark}] {leg.name}: {leg.detail}")
        head = "HARNESS PASS" if self.ok else f"HARNESS FAIL at {self.failed_leg}"
        return head + "\n" + "\n".join(lines)


def run_harness(
    *,
    name: str,
    force: Hook,
    capture: Hook,
    snapshot_dir: Path,
    update: bool = False,
    artifact: str | None = None,
    expected_sha: str | None = None,
    require_clean: bool = True,
) -> HarnessResult:
    """Orchestrate force → capture → oracle-verify, stopping at the first failure.

    Order is load-bearing and enforced here (this IS the reusable part):

      0. provenance (only if `artifact` + `expected_sha` given) — the artifact
         under test must be built from the SHA under review; a stale artifact
         fails BEFORE we waste a force/capture on it (item 3).
      1. force   — drive the oracle into the known state. A non-ok force aborts:
         capturing an un-forced oracle would snapshot the wrong state.
      2. capture — read the forced state headlessly. A non-ok capture aborts:
         there is nothing trustworthy to verify.
      3. verify  — compare the capture to the recorded snapshot via
         review.oracle. A "new" baseline is NOT a pass (unreviewed), matching
         oracle.SnapshotResult.ok.

    The force and capture hooks are caller-supplied (product-specific). This
    function supplies only the sequencing, the short-circuit, and the verdict.
    """
    legs: list[LegResult] = []

    if artifact is not None and expected_sha is not None:
        try:
            pres = provenance.verify(artifact, expected_sha, require_clean=require_clean)
        except provenance.ProvenanceError as e:
            pres = provenance.ProvenanceResult(False, f"MALFORMED provenance: {e}", None)
        legs.append(LegResult("provenance", pres.ok, pres.reason))
        if not pres.ok:
            return HarnessResult(False, legs, None)

    f_ok, f_out = force()
    legs.append(LegResult("force", f_ok, (f_out.strip() or "forced") if f_ok else (f_out.strip() or "force command failed")))
    if not f_ok:
        return HarnessResult(False, legs, None)

    c_ok, c_out = capture()
    legs.append(LegResult("capture", c_ok, ("captured" if c_ok else (c_out.strip() or "capture command failed"))))
    if not c_ok:
        return HarnessResult(False, legs, None)

    snap = oracle.verify(name, c_out, snapshot_dir, update=update)
    detail = {
        "match": f"capture matches snapshot {snap.path}",
        "new": f"NEW baseline recorded at {snap.path} (UNREVIEWED — not a pass)",
        "changed": f"capture DIFFERS from snapshot:\n{snap.diff}",
    }[snap.status]
    legs.append(LegResult("verify", snap.ok, detail))
    return HarnessResult(snap.ok, legs, snap)


# --------------------------------------------------------------------------
# CLI — force/capture are PRODUCT argv commands the caller passes in.
#   python3 -m review.harness run --name default-tab \
#       --force  './scripts/force-default-tab.sh' \
#       --capture './scripts/capture-active-tab.sh' \
#       --dir .maw/review-snaps [--artifact build/App.app --sha <SHA>]
# --------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    import shlex

    force = argv_hook(shlex.split(args.force))
    capture = argv_hook(shlex.split(args.capture))
    res = run_harness(
        name=args.name,
        force=force,
        capture=capture,
        snapshot_dir=Path(args.dir),
        update=args.update,
        artifact=args.artifact,
        expected_sha=args.sha,
        require_clean=not args.allow_dirty,
    )
    print(res.render())
    # exit 0 only on a true pass; 2 on any failing leg (force/capture/verify/prov)
    return 0 if res.ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review.harness", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="force → capture → oracle-verify (force/capture are product hooks)")
    r.add_argument("--name", required=True, help="snapshot identity")
    r.add_argument("--force", required=True, help="PRODUCT force command (argv string)")
    r.add_argument("--capture", required=True, help="PRODUCT capture command (argv string)")
    r.add_argument("--dir", required=True, help="snapshot store directory")
    r.add_argument("--update", action="store_true", help="rewrite the snapshot to the capture")
    r.add_argument("--artifact", default=None, help="artifact to provenance-check before forcing")
    r.add_argument("--sha", default=None, help="SHA under review (required with --artifact)")
    r.add_argument("--allow-dirty", action="store_true", help="permit a dirty-tree build provenance")
    r.set_defaults(fn=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "artifact", None) and not getattr(args, "sha", None):
        print("review.harness: --artifact requires --sha (the SHA under review)", file=sys.stderr)
        return 2
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
