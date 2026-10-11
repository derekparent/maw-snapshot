"""Pure-stdlib verification of a WebAuthn (passkey) assertion — phone-mint v2, Option A.

This is the one security-critical new verify path v2 adds (design §4, middle
shape; §5). It answers exactly one question, offline, with the stdlib alone:

  Is this a REAL user-verified passkey assertion whose signed challenge equals a
  digest the caller derived — under a public key the caller trusts?

It deliberately does NOT know about the grant schema. The caller (guards, in
lane 2) computes `challenge_digest = sha256(canonical_bytes(grant))` from the
grant IT holds and passes it here; equality against the assertion's signed
`clientDataJSON.challenge` is the §9.5 derive-what-you-display bind — the phone
signed a challenge derived on-device from the displayed grant, and the verifier
re-derives the same digest from the grant it received. If a compromised Mac
rendered text X to the phone while the grant object says Y, the digests differ
and this returns not-ok. Keeping the grant binding in the caller keeps this
module a clean, testable crypto primitive.

Threat model (N5 precedent — design §5, v1 §10):
  - Trusts: guards.p256 (the audited ECDSA verify), the caller-supplied pubkey
    (a Layer-1 committed key, N6), and the caller-supplied challenge digest.
  - Defends: presence-only assertions (UV flag REQUIRED — a tap is not Face ID,
    N2), challenge substitution (equality check), signature forgery (P-256
    verify over authenticatorData || sha256(clientDataJSON), the WebAuthn
    signing base), and malformed input (fail-closed: every parse problem →
    not-ok, never a raise to the caller).
  - Does NOT check: rpId hash, sign counter, origin allow-listing — those are
    the mint page's concern (lane 3), not the offline grant verifier's. A note
    in `verify_assertion` marks where a caller may layer them.

VERIFY ONLY. No signing, no key generation. Signing is the passkey's; key
trust is the Layer-1 keyset's.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from guards import p256

# WebAuthn authenticatorData flag bits (WebAuthn L3, §6.1).
FLAG_UP = 0x01  # User Present (a tap)
FLAG_UV = 0x04  # User Verified (Face ID / Touch ID / PIN) — N2 requires this


@dataclass(frozen=True)
class WebAuthnResult:
    ok: bool
    reason: str = ""


def _reject(reason: str) -> WebAuthnResult:
    return WebAuthnResult(False, reason)


def b64url_decode(s: str) -> bytes:
    """Decode base64url WITHOUT requiring padding (WebAuthn/JSON omit it)."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _der_uint(b: bytes) -> "int | None":
    """Decode a DER INTEGER's content bytes as a non-negative, MINIMALLY-encoded
    integer, or None. Rejects negatives (high bit set) and non-minimal padding
    (a leading 0x00 that isn't needed to keep the value positive) — the two
    non-canonical encodings that would otherwise decode to an accepted value
    (advisor T1). r and s are always positive, so a negative encoding is invalid."""
    if not b:
        return None
    if b[0] & 0x80:
        return None  # negative — not a valid ECDSA r/s
    if len(b) > 1 and b[0] == 0x00 and not (b[1] & 0x80):
        return None  # non-minimal: the leading 0x00 was unnecessary
    return int.from_bytes(b, "big")


def der_decode_ecdsa_sig(der: bytes) -> "tuple[int, int] | None":
    """Decode an ASN.1 DER ECDSA signature: SEQUENCE { INTEGER r, INTEGER s }.

    WebAuthn ES256 signatures are DER, not the raw r||s of a Mac SE signature —
    this is the one structural difference from the guards.human_grant path.
    Fail-closed: any malformed structure → None (never a raise). INTEGERs must be
    non-negative and minimally encoded (see _der_uint)."""
    try:
        if len(der) < 8 or der[0] != 0x30:
            return None
        if der[1] & 0x80:  # long-form length; P-256 sigs are always short-form
            return None
        seq_len = der[1]
        if seq_len != len(der) - 2:  # declared length must cover exactly the rest
            return None
        i = 2
        if der[i] != 0x02:
            return None
        rlen = der[i + 1]
        if rlen == 0 or i + 2 + rlen > len(der):
            return None
        r = _der_uint(der[i + 2:i + 2 + rlen])
        j = i + 2 + rlen
        if j + 2 > len(der) or der[j] != 0x02:
            return None
        slen = der[j + 1]
        if slen == 0 or j + 2 + slen != len(der):
            return None
        s = _der_uint(der[j + 2:j + 2 + slen])
        if r is None or s is None:
            return None
        return r, s
    except (IndexError, ValueError):
        return None


def spki_p256_point(spki: bytes) -> "tuple[int, int] | None":
    """Extract the (x, y) affine point from a P-256 SPKI DER public key.

    A WebAuthn `getPublicKey()` for an ES256 credential returns SPKI DER whose
    trailing 65 bytes are the uncompressed EC point 0x04 || X[32] || Y[32].
    Validate the marker rather than trust a fixed header length. Fail-closed."""
    if len(spki) < 65 or spki[-65] != 0x04:
        return None
    pt = spki[-65:]
    return int.from_bytes(pt[1:33], "big"), int.from_bytes(pt[33:65], "big")


def verify_assertion(
    *,
    challenge_digest: bytes,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature_der: bytes,
    pubkey_point: "tuple[int, int]",
    require_uv: bool = True,
) -> WebAuthnResult:
    """Verify a WebAuthn assertion binds a user-verified passkey signature to
    `challenge_digest`.

    Args (all raw bytes as produced by the authenticator, except the point):
      challenge_digest: the bytes the challenge must equal — the caller derives
        this from the content it displayed (§9.5), e.g.
        sha256(canonical_bytes(grant)).
      client_data_json: assertion.response.clientDataJSON (raw bytes; the sig
        covers these bytes' sha256, so they are used verbatim — never re-serialized).
      authenticator_data: assertion.response.authenticatorData.
      signature_der: assertion.response.signature (DER ECDSA).
      pubkey_point: the credential's P-256 public key as (x, y).
      require_uv: if True (default, N2), the UV flag MUST be set.

    A caller that also needs origin / rpIdHash / sign-counter checks layers them
    on top of an ok result — this function intentionally covers only the
    challenge bind + UV + signature, the offline-verifiable core.
    """
    # --- clientDataJSON: type + challenge bind (§9.5) ---
    try:
        cdj = json.loads(client_data_json.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _reject("clientDataJSON is not valid UTF-8 JSON")
    if not isinstance(cdj, dict):
        return _reject("clientDataJSON is not a JSON object")
    if cdj.get("type") != "webauthn.get":
        return _reject(f"clientData.type is {cdj.get('type')!r}, not 'webauthn.get'")
    expected = b64url_nopad(challenge_digest)
    signed = cdj.get("challenge")
    if not isinstance(signed, str):
        return _reject("clientData.challenge missing or not a string")
    # Constant-time-ish equality is unnecessary (both sides public), but normalize
    # any padding the client may have emitted before comparing.
    if signed.rstrip("=") != expected:
        return _reject("challenge does not match the derived digest (§9.5 bind broken)")

    # --- authenticatorData flags: UP always, UV when required (N2) ---
    if len(authenticator_data) < 37:  # 32 rpIdHash + 1 flags + 4 counter
        return _reject("authenticatorData too short")
    flags = authenticator_data[32]
    if not (flags & FLAG_UP):
        return _reject("UP (user-present) flag not set")
    if require_uv and not (flags & FLAG_UV):
        return _reject("UV (user-verified) flag not set — presence-only, N2 unmet")

    # --- signature: P-256 over authenticatorData || sha256(clientDataJSON) ---
    if not (isinstance(pubkey_point, tuple) and len(pubkey_point) == 2):
        return _reject("pubkey_point is not an (x, y) tuple")
    rs = der_decode_ecdsa_sig(signature_der)
    if rs is None:
        return _reject("signature is not a well-formed DER ECDSA value")
    signing_base = authenticator_data + hashlib.sha256(client_data_json).digest()
    digest_int = int.from_bytes(hashlib.sha256(signing_base).digest(), "big")
    if not p256.verify(pubkey_point, digest_int, rs[0], rs[1]):
        return _reject("P-256 signature verification failed")

    return WebAuthnResult(True, "")
