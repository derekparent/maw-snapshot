"""guards.mint — the human_grant MINT ceremony (design §9, the operator use case 4).

The flow the operator asked for: the CoS drafts the instructions, and the operator authorizes each
one as his own after reading it. So the ceremony, in order:

  1. The CoS composes the exact instruction text + scope_class + target.
  2. mint() displays the FULL LITERAL instruction text (never a hash or a summary
     — the report names truncated display as how a human is tricked into signing
     something they didn't mean), plus scope/target/TTL.
  3. The operator reads it and Touch-ID-signs: the Secure-Enclave helper signs the grant
     digest, the biometry ACL forces Touch ID. One instruction = one signature.
  4. The grant is appended to the ledger (allow-list) and returned for the CoS to
     attach to its relay.

MINT RUNS ONLY ON THE OPERATOR'S MAC (Touch ID is local; phone/WebAuthn is v2). The signing
itself is `SeSigner`, which shells out to the Secure-Enclave Swift helper. That
native leg cannot run in CI (no SE hardware, no Touch ID), so the ceremony takes a
`signer` callable — production passes SeSigner; tests pass a fake — and everything
EXCEPT the SE call is covered.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

import opconf
from guards import human_grant as hg

# signer(digest_hex, reason) -> base64(raw 64-byte r||s). The one seam between
# the testable ceremony and the un-CI-testable Secure Enclave. `reason` is the
# short what-am-I-signing line the SE helper surfaces IN the Touch ID dialog
# (S2: the biometric prompt itself names scope+target, like session_auth's arm).
Signer = Callable[[str, str], str]

_HELPER_DIR = Path(__file__).resolve().parent / "human_grant_helper"
_HELPER_SRC = _HELPER_DIR / "sign.swift"
_HELPER_BIN = _HELPER_DIR / "human_grant_sign"


def _ensure_helper_built() -> Path:
    """Compile the SE helper from committed source if the binary is absent (never
    a checked-in binary — design §5a option A). Raises on a build failure.

    No -framework flags and no codesign: the CryptoKit build auto-links, and an
    UNSIGNED binary drives the enclave fine (verified live 2026-07-18 — the
    keychain path needed an entitlement; the CryptoKit blob path does not).
    The key persists as an enclave-encrypted blob at
    <grants_dir>/se-key.blob: theft is useless off this Mac, plaintext export
    does not exist, biometry gates every signature, and deletion is a DoS only
    (re-key + the Layer-1 re-commit ceremony)."""
    if _HELPER_BIN.exists():
        return _HELPER_BIN
    cp = subprocess.run(
        ["swiftc", "-O", "-o", str(_HELPER_BIN), str(_HELPER_SRC)],
        capture_output=True, text=True, timeout=120,
    )
    if cp.returncode != 0 or not _HELPER_BIN.exists():
        raise RuntimeError(f"SE helper build failed: {cp.stderr.strip()}")
    return _HELPER_BIN


class SeSigner:
    """Production signer: the Secure-Enclave Swift helper. NOT exercised in CI."""

    def __call__(self, digest_hex: str, reason: str) -> str:
        binary = _ensure_helper_built()
        cp = subprocess.run([str(binary), "sign", digest_hex, reason],
                            capture_output=True, text=True, timeout=120)
        if cp.returncode != 0 or not cp.stdout.strip():
            raise RuntimeError(f"SE signing failed or refused: {cp.stderr.strip()}")
        return cp.stdout.strip()

    @staticmethod
    def pubkey() -> "tuple[str, str]":
        """(x_hex, y_hex) of the SE public key — the values for a committed .pub."""
        binary = _ensure_helper_built()
        cp = subprocess.run([str(binary), "pubkey"],
                            capture_output=True, text=True, timeout=120)
        if cp.returncode != 0 or ":" not in cp.stdout:
            raise RuntimeError(f"SE pubkey read failed: {cp.stderr.strip()}")
        x, y = cp.stdout.strip().split(":", 1)
        return (x, y)


def _render_ceremony(grant: dict, instruction: str) -> str:
    """The FULL-literal-text prompt the operator reads before signing. No hash, no summary."""
    lines = [
        "=" * 68,
        "SIGN THE OPERATOR GRANT — read the FULL instruction below before Touch ID.",
        "=" * 68,
        f"scope_class : {grant['scope_class']}",
        f"target      : {grant['target']}",
        f"TTL         : {grant['ttl_seconds']}s",
    ]
    if grant.get("merge"):
        m = grant["merge"]
        lines.append(f"merge       : {m['repo']} PR #{m['pr']} @ {m['head_sha']}")
    if grant.get("session"):
        for k, v in sorted(grant["session"].items()):
            lines.append(f"session.{k:<11}: {v}")
    lines += ["-" * 68, "INSTRUCTION (verbatim — this exact text is what you sign):",
              "", instruction, "", "=" * 68]
    return "\n".join(lines)


def mint(
    instruction: str,
    scope_class: str,
    target: str,
    ttl_seconds: int,
    pubkey_id: str,
    now_iso: str,
    signer: Signer,
    *,
    merge: "dict | None" = None,
    session: "dict | None" = None,
    show: Callable[[str], None] = print,
    record: bool = True,
) -> dict:
    """Assemble, show-for-approval, sign, ledger, and return a grant.

    Validated against the SAME bounds the verifier enforces (SCOPE_CLASSES,
    MAX_TTL_SECONDS, the merge shape) so a mint can never produce a grant that
    won't verify. Raises ValueError on a malformed request BEFORE showing or
    signing anything.
    """
    if scope_class not in hg.SCOPE_CLASSES:
        raise ValueError(f"unknown scope_class: {scope_class!r}")
    if scope_class == hg.BATCH_CLASS:
        # One signature arming N seats needs the phone page's one-card-per-repo
        # render; the Touch ID sheet cannot show a manifest (batch-arm design §3).
        raise ValueError(f"{hg.BATCH_CLASS} is phone-only: mint it with "
                         f"phone_mint.relay_client mint-batch")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) \
            or not (0 < ttl_seconds <= hg.MAX_TTL_SECONDS):
        raise ValueError("ttl_seconds out of bounds")
    if scope_class == "merge.approval":
        if not (isinstance(merge, dict) and isinstance(merge.get("repo"), str)
                and isinstance(merge.get("pr"), int) and not isinstance(merge.get("pr"), bool)
                and isinstance(merge.get("head_sha"), str) and len(merge["head_sha"]) == 40):
            raise ValueError("merge.approval requires a {repo, pr, head_sha(40)} object")
    elif merge is not None:
        raise ValueError("merge is only for scope_class merge.approval")
    why = hg.session_shape_error(scope_class, session)
    if why is not None:
        raise ValueError(why)

    grant = {
        "grant_id": str(uuid.uuid4()),
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "scope_class": scope_class,
        "target": target,
        "issued_at": now_iso,
        "ttl_seconds": ttl_seconds,
        "session_token_ref": None,
        "nonce": str(uuid.uuid4()),
        "pubkey_id": pubkey_id,
        "merge": merge,
        "session": session,
    }
    # Show the FULL literal text BEFORE signing — the ceremony's honesty.
    show(_render_ceremony(grant, instruction))
    digest = hashlib.sha256(hg.canonical_bytes(grant)).hexdigest()
    # The Touch ID dialog itself names what is being signed (S2).
    reason = f"sign the operator grant: {scope_class} -> {target}"
    grant["sig"] = signer(digest, reason)  # SE helper: forces Touch ID
    if record:
        hg.record_issued(grant)
    return grant


# --- CLI (the stable operator-facing mint surface) -------------------------------
# The CoS relay flow (handoff 2026-07-19) needs a SINGLE copy-paste command, not
# a `python3 -c` over the API. This verb is that surface. Two disciplines it
# enforces at the boundary the raw API left to the caller:
#   - the instruction is read from a FILE, never argv: the body IS the hashed
#     instruction, so a shell mangling it (backtick execution, quote-eating —
#     the 2026-07-18 commit-message bite) would silently break verification.
#   - the ceremony text prints to STDERR (the operator reads it in his pane); STDOUT
#     carries only the grant JSON, so `--out` / a pipe gets clean machine data.
# `now_iso` is stamped here in human_grant's exact _TS_FMT so the grant verifies.

_OK = 0
_ERR = 1


def _read_session_file(path: str, scope_class: str) -> "tuple[dict | None, str]":
    """The session object for a session-class mint, from `session_auth
    grant-request` output or the bare object. Returns (session, "") or (None, why).

    The instruction is never taken from the file: mint() renders it from the
    session, as the verifying seat will. A request whose own `instruction` line
    disagrees with that rendering was edited after grant-request, so it refuses
    rather than show the operator one text and sign another."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"cannot read --session-file: {e}"
    if isinstance(data, dict) and set(data) == {"session", "instruction"}:
        session = data["session"]
        why = hg.session_shape_error(scope_class, session)
        if why:
            return None, why
        if data["instruction"] != hg.session_instruction(scope_class, session):
            return None, ("--session-file's instruction does not match its session "
                          f"object for {scope_class} (edited, or a request for the "
                          "other class); re-run grant-request")
        return session, ""
    return data, ""


def main(argv: "list[str] | None" = None, *, signer: "Signer | None" = None) -> int:
    """`python3 -m guards.mint` — mint one operator grant. Returns 0 on success.

    `signer` is injectable for tests (the SE hardware leg cannot run in CI); in
    production it defaults to SeSigner(), which forces Touch ID.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="guards.mint",
        description="Mint one operator grant: show the full literal instruction, "
                    "Touch-ID-sign, ledger, and emit the grant JSON.",
    )
    parser.add_argument("--instruction-file", default=None,
                        help="path to the EXACT instruction text (the body is the "
                             "hashed instruction — never inline it on argv). Not "
                             "for session.arm/scope.amend: their instruction is "
                             "rendered from --session-file")
    parser.add_argument("--session-file", default=None,
                        help="session.arm/scope.amend only: the `session_auth "
                             "grant-request` output (or its bare `session` object)")
    parser.add_argument("--scope", required=True,
                        help="scope_class: " + " | ".join(sorted(hg.SCOPE_CLASSES)))
    parser.add_argument("--target", required=True, help="the target seat/name")
    parser.add_argument("--ttl", type=int, required=True,
                        help=f"ttl seconds (0 < ttl <= {hg.MAX_TTL_SECONDS})")
    parser.add_argument("--pubkey-id", default=None,
                        help="signing key id (default: grant_pubkey_id from opconf)")
    parser.add_argument("--merge-repo", default=None,
                        help="merge.approval only: target repo slug")
    parser.add_argument("--merge-pr", type=int, default=None,
                        help="merge.approval only: PR number")
    parser.add_argument("--merge-head-sha", default=None,
                        help="merge.approval only: exact 40-hex head SHA")
    parser.add_argument("--out", default=None,
                        help="write the grant JSON to this path (for net send "
                             "--grant-file); it is always printed to stdout too")
    args = parser.parse_args(argv)
    if not args.pubkey_id:
        try:
            args.pubkey_id = opconf.require("grant_pubkey_id")
        except opconf.NotConfigured as e:
            print(f"mint: refused — {e}", file=sys.stderr)
            return _ERR

    session = None
    if args.scope in hg.SESSION_CLASSES:
        if args.instruction_file:
            print(f"mint: refused — {args.scope} renders its instruction from "
                  "--session-file; drop --instruction-file", file=sys.stderr)
            return _ERR
        if not args.session_file:
            print(f"mint: refused — {args.scope} needs --session-file (the "
                  "`session_auth grant-request` output)", file=sys.stderr)
            return _ERR
        session, why = _read_session_file(args.session_file, args.scope)
        if why:
            print(f"mint: refused — {why}", file=sys.stderr)
            return _ERR
        instruction = hg.session_instruction(args.scope, session)
    else:
        if args.session_file:
            print(f"mint: refused — --session-file is only for "
                  f"{' / '.join(sorted(hg.SESSION_CLASSES))}", file=sys.stderr)
            return _ERR
        if not args.instruction_file:
            print("mint: refused — --instruction-file is required", file=sys.stderr)
            return _ERR
        try:
            instruction = Path(args.instruction_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"mint: cannot read --instruction-file: {e}", file=sys.stderr)
            return _ERR

    merge = None
    if any(v is not None for v in (args.merge_repo, args.merge_pr, args.merge_head_sha)):
        merge = {"repo": args.merge_repo, "pr": args.merge_pr,
                 "head_sha": args.merge_head_sha}

    if signer is None:
        signer = SeSigner()

    try:
        grant = mint(
            instruction=instruction,
            scope_class=args.scope,
            target=args.target,
            ttl_seconds=args.ttl,
            pubkey_id=args.pubkey_id,
            now_iso=time.strftime(hg._TS_FMT, time.gmtime()),
            signer=signer,
            merge=merge,
            session=session,
            show=lambda s: print(s, file=sys.stderr),
        )
    except ValueError as e:
        print(f"mint: refused — {e}", file=sys.stderr)
        return _ERR
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"mint: signing failed — {e}", file=sys.stderr)
        return _ERR

    payload = json.dumps(grant, indent=2, sort_keys=True)
    if args.out:
        # The signature is already spent (one Touch ID). An unwritable --out must
        # NOT traceback and swallow the grant — warn and fall through to stdout,
        # from where the CoS can still recover it (also on the issued ledger).
        try:
            Path(args.out).write_text(payload + "\n", encoding="utf-8")
            print(f"mint: grant written to {args.out}", file=sys.stderr)
        except OSError as e:
            print(f"mint: WARN could not write --out {args.out} ({e}); "
                  "the grant follows on stdout", file=sys.stderr)
    print(payload)
    return _OK


if __name__ == "__main__":
    raise SystemExit(main())
