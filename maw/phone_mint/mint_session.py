"""phone-mint v2 mint session — assemble a phone-minted grant (Option A, §9).

The phone equivalent of guards.mint's ceremony. It produces the SAME grant
object the seat verifier and the CoS relay already consume (§3, N8) — only the
signature leg differs: a `webauthn` assertion instead of a Mac-SE `sig`.

Two functions, split so the whole thing is CI-testable without a phone:

  assemble_unsigned_grant() — validate + build the grant WITHOUT a signature.
    The TTL clock starts HERE, at the mint REQUEST (§9.2): issued_at = now_iso.
    A ceremony answered after issued_at + ttl is already expired to the verifier;
    attach_assertion NEVER re-stamps issued_at.

  attach_assertion() — take the assertion the page collected, VALIDATE the body
    (never trust the POST payload — advisor carry-forward from #151), SELF-VERIFY
    it binds to this grant via phone_mint.webauthn_verify (fail before relaying a
    dud), attach the `webauthn` object, ledger, and return the relayable grant.

The actual window-only HTTPS page that serves a grant to the operator's phone over
Tailscale (phone-ABSENT — the operator 2026-07-20) and the one-time passkey registration
that produces the committed `<phone_pubkey_id>.pub` are the LIVE integration
(lane 3b) and the operator's Layer-1 trusted-path commit respectively — not this module.
§9.3 (Mac-mint default when present) is a CoS routing policy, not code here (the
design forbids magic presence detection): the CoS picks the surface, or the operator does.
"""
from __future__ import annotations

import hashlib
import uuid

import opconf
from guards import human_grant as hg
from phone_mint import webauthn_verify as wv


def phone_pubkey_id() -> str:
    """The phone key's pubkey_id — a DISTINCT committed key (§9.4), NOT the Mac SE
    key. The operator's own id (opconf); unset raises NotConfigured (a ValueError)."""
    return opconf.require("phone_pubkey_id")


def assemble_unsigned_grant(
    *,
    instruction: str,
    scope_class: str,
    target: str,
    ttl_seconds: int,
    now_iso: str,
    pubkey_id: "str | None" = None,
    merge: "dict | None" = None,
    session: "dict | None" = None,
    batch: "list | None" = None,
) -> dict:
    """Build the unsigned grant. TTL clock starts now (§9.2). Raises ValueError on
    a malformed request BEFORE anything is shown or signed — the same bounds the
    verifier enforces (SCOPE_CLASSES, MAX_TTL_SECONDS, the merge shape), so a mint
    can never produce a grant that won't verify.

    The field set is IDENTICAL to guards.mint's grant minus the signature; a
    parity test pins that against drift."""
    if pubkey_id is None:
        pubkey_id = phone_pubkey_id()
    if not isinstance(instruction, str) or not instruction:
        raise ValueError("instruction must be a non-empty string")
    if scope_class not in hg.SCOPE_CLASSES:
        raise ValueError(f"unknown scope_class: {scope_class!r}")
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
    extra: dict = {}
    if scope_class == hg.BATCH_CLASS or batch is not None:
        extra["batch"] = batch
        why = hg.batch_shape_error(scope_class, extra)
        if why is not None:
            raise ValueError(why)
        if instruction != hg.batch_instruction(batch):
            raise ValueError("a batch grant's instruction must be batch_instruction(batch)")

    return {
        "grant_id": str(uuid.uuid4()),
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "scope_class": scope_class,
        "target": target,
        "issued_at": now_iso,          # <-- the TTL clock starts at the REQUEST (§9.2)
        "ttl_seconds": ttl_seconds,
        "session_token_ref": None,
        "nonce": str(uuid.uuid4()),
        "pubkey_id": pubkey_id,
        "merge": merge,
        "session": session,
        **extra,
    }


def challenge_digest(grant: dict) -> bytes:
    """The digest the phone page must sign as its WebAuthn challenge — derived
    from the grant's own bytes (§9.5). Exposed so the serving layer computes the
    SAME value the verifier will, never a caller-supplied one."""
    return hashlib.sha256(hg.canonical_bytes(grant)).digest()


def attach_assertion(
    grant: dict,
    *,
    client_data_json_b64: str,
    authenticator_data_b64: str,
    signature_b64: str,
    pubkey_point: "tuple[int, int]",
    record: bool = True,
) -> dict:
    """Validate + self-verify the assertion the page collected, attach it as the
    grant's `webauthn` signature, ledger the grant, and return it for relay.

    The three b64url fields come off an HTTP POST — treat them as untrusted:
    type-check and decode inside a guard, and refuse a grant that already carries
    a signature. Then self-verify against `pubkey_point` (the committed phone key,
    read from the keyset by the caller) so a dud assertion is caught HERE, not at
    the seat after it was relayed. Raises ValueError on any failure."""
    if "sig" in grant or "webauthn" in grant:
        raise ValueError("grant already carries a signature")
    for name, val in (("client_data_json", client_data_json_b64),
                      ("authenticator_data", authenticator_data_b64),
                      ("signature", signature_b64)):
        if not isinstance(val, str) or not val:
            raise ValueError(f"{name} must be a non-empty base64url string")
    try:
        cdj = wv.b64url_decode(client_data_json_b64)
        authdata = wv.b64url_decode(authenticator_data_b64)
        sig_der = wv.b64url_decode(signature_b64)
    except (ValueError, TypeError) as e:
        raise ValueError(f"assertion field is not valid base64url: {e}") from e

    res = wv.verify_assertion(
        challenge_digest=challenge_digest(grant),
        client_data_json=cdj,
        authenticator_data=authdata,
        signature_der=sig_der,
        pubkey_point=pubkey_point,
        require_uv=True,
    )
    if not res.ok:
        raise ValueError(f"assertion does not verify against this grant: {res.reason}")

    grant["webauthn"] = {
        "client_data_json": client_data_json_b64,
        "authenticator_data": authenticator_data_b64,
        "signature": signature_b64,
    }
    if record:
        hg.record_issued(grant)
    return grant
