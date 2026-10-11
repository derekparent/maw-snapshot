"""Build-from-SHA provenance — never verify a stale artifact (backlog item 3).

For interpreted maw code, "is this artifact from this SHA" is trivially true.
For COMPILED targets (iOS) the build can silently lag the source: the advisor's
first Ventusky screenshot was a stale `DerivedData` build showing 4 tabs, not 5
— caught by luck. A reviewer (or a builder screenshot) verifying against a stale
build passes a broken state. Item #2's oracle evidence is only trustworthy if
the artifact under test was actually built from the SHA under review.

This module is the checkable gate:

  - `build(cmd, artifact, repo)` is the controlled path: it captures the repo's
    HEAD SHA and dirty bit BEFORE running the build command, refuses if the
    build fails or the source identity changed while it ran, then stamps the
    artifact with the identity it captured.
  - `stamp(path, repo)` writes the sidecar for an artifact built elsewhere. It
    records the repo's CURRENT state, so it is a claim by whoever runs it: it
    cannot know which source an existing artifact was built from.
  - Either way the sidecar binds the artifact's digest (sha256 of a file, or of
    a directory's paths and contents), so replacing or deleting the artifact
    after stamping fails verification.
  - `verify(artifact, expected_sha)` is the reviewer gate: it FAILS if the
    artifact is missing, its digest changed, it was built from a different SHA
    or a dirty tree, or it has no (or a malformed) provenance sidecar. Run it
    before trusting any UI/behavior capture.

Trust boundary, stated plainly: the builder is trusted. A build command can
read files outside the repo, and anyone who can write the artifact can also
write its sidecar. This gate catches a STALE or swapped artifact reaching a
reviewer by mistake; it is not an attestation against a hostile builder.

Two honest modes:
  - compiled: stamp at build, verify the sidecar.
  - interpreted (maw itself): no build step — `head_sha`/`is_clean` over the
    worktree under test ARE the provenance; verify they equal the SHA under
    review and the tree is clean.

Stdlib-only; git is shelled out via an injectable runner so the logic is
testable offline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROVENANCE_FILENAME = ".maw-build-provenance.json"

# git's own short-SHA floor. A prefix shorter than this is too weak to identify
# a commit; the EMPTY string in particular must never match (advisor: an unset
# env var, `--sha "$UNSET"`, expands to "" and `prov.sha.startswith("")` is
# always True → the gate FALSE-PASSES).
MIN_SHA_LEN = 7

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ProvenanceError(Exception):
    pass


def _normalize_expected_sha(expected_sha: str) -> str | None:
    """Validated lowercase hex SHA (>= MIN_SHA_LEN), or None if unusable.

    Rejecting empty/whitespace/short/non-hex closes the false-pass gate bypass:
    the SHA under review must be a real commit-ish, never nothing.
    """
    exp = expected_sha.strip().lower()
    if len(exp) < MIN_SHA_LEN or any(c not in "0123456789abcdef" for c in exp):
        return None
    return exp


def _run(cmd: list[str], cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)


def head_sha(repo: str | None = None, runner: Runner = _run) -> str:
    cp = runner(["git", "rev-parse", "HEAD"], repo)
    if cp.returncode != 0:
        raise ProvenanceError(f"git rev-parse HEAD failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


def is_clean(repo: str | None = None, runner: Runner = _run) -> bool:
    """True if the worktree has no uncommitted changes (tracked or staged).

    A dirty tree means the SHA does NOT fully describe the artifact — the gate
    treats dirty as not-from-SHA by default.
    """
    cp = runner(["git", "status", "--porcelain"], repo)
    if cp.returncode != 0:
        raise ProvenanceError(f"git status failed: {cp.stderr.strip()}")
    return cp.stdout.strip() == ""


def _is_hex(value: object, length: int) -> bool:
    return (isinstance(value, str) and len(value) == length
            and all(c in "0123456789abcdef" for c in value))


def artifact_digest(path: Path) -> str:
    """sha256 of the artifact. Every failure is a ProvenanceError.

    A top-level symlink is resolved (the artifact is what it points at). A file
    hashes its bytes. A directory hashes every entry under it in sorted order,
    skipping its own sidecar, each record length-prefixed: files by path, size
    and content; directories by path; inner symlinks by their target text and
    NEVER followed (content behind an inner symlinked dir is not covered);
    anything else (FIFO, socket, device) by path and type only. An unreadable
    entry anywhere refuses rather than yield a partial digest."""
    try:
        return _digest(path.resolve(strict=True))
    except ProvenanceError:
        raise
    except (OSError, ValueError) as e:
        raise ProvenanceError(f"cannot digest artifact {path}: {e}") from e


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_file():
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    if not path.is_dir():
        raise ProvenanceError(f"artifact is neither a file nor a directory: {path}")

    def field(data: bytes) -> bytes:
        # length-prefixed, so no bytes can move between a path and a content
        return len(data).to_bytes(8, "big") + data

    def fail(err: OSError) -> None:
        raise ProvenanceError(f"cannot read inside artifact {path}: {err}")

    entries: list[Path] = []
    for root, dirs, files in os.walk(path, onerror=fail, followlinks=False):
        entries += [Path(root) / n for n in dirs + files]
    for entry in sorted(entries, key=lambda p: p.relative_to(path).as_posix()):
        rel = entry.relative_to(path).as_posix()
        if rel == PROVENANCE_FILENAME:
            continue
        mode = entry.lstat().st_mode
        if stat.S_ISLNK(mode):
            h.update(b"L" + field(rel.encode()) + field(os.readlink(entry).encode()))
        elif stat.S_ISDIR(mode):
            h.update(b"D" + field(rel.encode()))
        elif stat.S_ISREG(mode):
            h.update(b"F" + field(rel.encode()) + entry.stat().st_size.to_bytes(8, "big"))
            with open(entry, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        else:
            h.update(b"O" + field(rel.encode()) + stat.S_IFMT(mode).to_bytes(4, "big"))
    return h.hexdigest()


@dataclass
class Provenance:
    sha: str
    dirty: bool
    built_at: str
    artifact_sha256: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"sha": self.sha, "dirty": self.dirty, "built_at": self.built_at,
             "artifact_sha256": self.artifact_sha256},
            indent=2,
        ) + "\n"

    @classmethod
    def from_file(cls, path: Path) -> "Provenance":
        """Strict: every field must have exactly its type. A `"dirty": null`
        or a truncated SHA is malformed, never coerced into something that
        passes."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # ValueError covers bad JSON and bad UTF-8
            raise ProvenanceError(f"unreadable provenance {path}: {e}") from e
        if not isinstance(data, dict):
            raise ProvenanceError(f"malformed provenance {path}: not an object")
        problems = []
        if not _is_hex(data.get("sha"), 40):
            problems.append("sha must be a 40-char lowercase hex SHA")
        if not isinstance(data.get("dirty"), bool):
            problems.append("dirty must be true or false")
        if not isinstance(data.get("built_at"), str):
            problems.append("built_at must be a string")
        if not _is_hex(data.get("artifact_sha256"), 64):
            problems.append("artifact_sha256 must be a 64-char hex digest")
        if problems:
            raise ProvenanceError(f"malformed provenance {path}: " + "; ".join(problems))
        return cls(sha=data["sha"], dirty=data["dirty"], built_at=data["built_at"],
                   artifact_sha256=data["artifact_sha256"])


def _sidecar_for(path: Path) -> Path:
    """Provenance sidecar location for an artifact path.

    For a directory artifact (e.g. an .app bundle) the sidecar lives inside it;
    for a file it sits beside it.
    """
    if path.is_dir():
        return path / PROVENANCE_FILENAME
    return path.with_name(path.name + PROVENANCE_FILENAME)


def stamp(
    artifact: str,
    repo: str | None = None,
    *,
    now: str | None = None,
    runner: Runner = _run,
) -> Provenance:
    """Record the repo's CURRENT HEAD SHA + dirty bit, and the artifact's digest,
    next to an artifact built elsewhere. Prefer `build()`, which captures the
    identity before building."""
    return _write_stamp(Path(artifact), head_sha(repo, runner),
                        not is_clean(repo, runner), now)


def _write_stamp(p: Path, sha: str, dirty: bool, now: str | None) -> Provenance:
    if not p.exists():
        raise ProvenanceError(f"artifact does not exist: {p}")
    prov = Provenance(
        sha=sha,
        dirty=dirty,
        # caller passes `now` (the runtime forbids argless clock calls in some
        # contexts); default to wall-clock UTC when run as a normal build step.
        built_at=now if now is not None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        artifact_sha256=artifact_digest(p),
    )
    try:
        _sidecar_for(p).write_text(prov.to_json(), encoding="utf-8")
    except OSError as e:
        raise ProvenanceError(f"cannot write provenance for {p}: {e}") from e
    return prov


def build(
    cmd: list[str],
    artifact: str,
    repo: str | None = None,
    *,
    now: str | None = None,
    runner: Runner = _run,
) -> Provenance:
    """Run `cmd` as the build, then stamp `artifact` with the source identity
    captured BEFORE it ran. Refuses (ProvenanceError, nothing stamped) if the
    build fails, or if HEAD or the dirty bit changed while it ran: then the
    artifact's source is not the SHA we would be recording."""
    if not cmd:
        raise ProvenanceError("no build command given")
    sha_before, dirty_before = head_sha(repo, runner), not is_clean(repo, runner)
    try:
        done = subprocess.run(cmd, cwd=repo)
    except OSError as e:
        raise ProvenanceError(f"build command failed to start: {e}") from e
    if done.returncode != 0:
        raise ProvenanceError(f"build failed (exit {done.returncode}); not stamped")
    sha_after, dirty_after = head_sha(repo, runner), not is_clean(repo, runner)
    if (sha_after, dirty_after) != (sha_before, dirty_before):
        raise ProvenanceError(
            f"source changed during the build ({sha_before[:12]}"
            f"{' dirty' if dirty_before else ''} -> {sha_after[:12]}"
            f"{' dirty' if dirty_after else ''}); not stamped")
    return _write_stamp(Path(artifact), sha_before, dirty_before, now)


@dataclass
class ProvenanceResult:
    ok: bool
    reason: str
    provenance: Provenance | None


def verify(
    artifact: str,
    expected_sha: str,
    *,
    require_clean: bool = True,
) -> ProvenanceResult:
    """Reviewer gate: does the artifact's provenance match the SHA under review?

    Fails (ok=False) when: no provenance sidecar, the recorded SHA differs from
    expected, or (by default) the build came from a dirty tree. A short-SHA
    `expected_sha` matches by prefix so the reviewer can pass `bad2602`.
    """
    exp = _normalize_expected_sha(expected_sha)
    if exp is None:
        return ProvenanceResult(
            False,
            f"INVALID SHA under review {expected_sha!r}: need >= {MIN_SHA_LEN} hex "
            f"chars — an empty/unset --sha must NOT pass the gate",
            None,
        )
    art = Path(artifact)
    if not art.exists():
        return ProvenanceResult(
            False, f"NO artifact: {artifact} does not exist — nothing to verify", None)
    sidecar = _sidecar_for(art)
    if not sidecar.exists():
        return ProvenanceResult(
            False,
            f"NO provenance: {sidecar} missing — artifact not stamped; "
            f"rebuild from a clean worktree at {exp[:12]} and stamp it",
            None,
        )
    prov = Provenance.from_file(sidecar)
    if artifact_digest(art) != prov.artifact_sha256:
        return ProvenanceResult(
            False,
            f"CHANGED artifact: its digest no longer matches the one stamped at "
            f"build time — it was replaced or modified after stamping; rebuild",
            prov,
        )
    # prov.sha is a full lowercase 40-char SHA from git. Match only as
    # "recorded SHA begins with the (validated) expected prefix". The reverse
    # branch (exp.startswith(prov.sha)) is dropped: it let a longer/garbage exp
    # pass against a shorter recorded value.
    if not prov.sha.startswith(exp):
        return ProvenanceResult(
            False,
            f"STALE artifact: built from {prov.sha[:12]} but the SHA under "
            f"review is {exp[:12]} — rebuild before trusting any capture",
            prov,
        )
    if require_clean and prov.dirty:
        return ProvenanceResult(
            False,
            f"DIRTY build: artifact built from {prov.sha[:12]} with uncommitted "
            f"changes — the SHA does not fully describe it; rebuild clean",
            prov,
        )
    state = "clean" if not prov.dirty else "dirty, allowed"
    return ProvenanceResult(True, f"artifact built from {exp[:12]} ({state})", prov)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_stamp(args: argparse.Namespace) -> int:
    try:
        prov = stamp(args.artifact, args.repo)
    except ProvenanceError as e:
        print(f"stamp failed: {e}", file=sys.stderr)
        return 1
    flag = " (DIRTY tree)" if prov.dirty else ""
    print(f"stamped {args.artifact} ← {prov.sha[:12]}{flag}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    cmd = list(args.cmd)
    if not cmd:
        print("build: give the build command after `--`", file=sys.stderr)
        return 2
    try:
        prov = build(cmd, args.artifact, args.repo)
    except ProvenanceError as e:
        print(f"build: {e}", file=sys.stderr)
        return 1
    flag = " (DIRTY tree)" if prov.dirty else ""
    print(f"built + stamped {args.artifact} ← {prov.sha[:12]}{flag}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        res = verify(args.artifact, args.sha, require_clean=not args.allow_dirty)
    except ProvenanceError as e:
        print(f"MALFORMED provenance: {e}")
        return 2
    print(res.reason)
    return 0 if res.ok else 2


def cmd_head(args: argparse.Namespace) -> int:
    # interpreted-target convenience: the worktree under test IS the artifact.
    exp = _normalize_expected_sha(args.sha)
    if exp is None:
        print(
            f"head: INVALID --sha {args.sha!r}: need >= {MIN_SHA_LEN} hex chars "
            f"(an empty/unset --sha must not pass)",
            file=sys.stderr,
        )
        return 2
    try:
        sha = head_sha(args.repo)
        clean = is_clean(args.repo)
    except ProvenanceError as e:
        print(f"head failed: {e}", file=sys.stderr)
        return 1
    match = sha.startswith(exp)
    ok = match and clean
    state = "clean" if clean else "DIRTY"
    print(f"{sha[:12]} ({state}) {'==' if match else '!='} {exp[:12]}")
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review.provenance", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("stamp", help="record build provenance next to an artifact")
    st.add_argument("artifact")
    st.add_argument("--repo", default=None)
    st.set_defaults(fn=cmd_stamp)

    bu = sub.add_parser("build", help="run a build and stamp its artifact with the "
                                      "source identity captured before it ran")
    bu.add_argument("artifact")
    bu.add_argument("--repo", default=None)
    bu.set_defaults(fn=cmd_build, cmd=[])

    ve = sub.add_parser("verify", help="gate: artifact built from the SHA under review?")
    ve.add_argument("artifact")
    ve.add_argument("--sha", required=True, help="SHA under review")
    ve.add_argument("--allow-dirty", action="store_true")
    ve.set_defaults(fn=cmd_verify)

    hd = sub.add_parser("head", help="interpreted target: worktree SHA vs the SHA under review")
    hd.add_argument("--sha", required=True)
    hd.add_argument("--repo", default=None)
    hd.set_defaults(fn=cmd_head)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `build <artifact> [--repo R] -- <cmd...>`: everything after the first `--`
    # is the build command, so it is split off before argparse sees it.
    tail: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if args.command == "build":
        args.cmd = tail
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
