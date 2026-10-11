"""guards.human_grant — the phone-minted (WebAuthn) grant path (phone-mint v2, §9.5).

Mirrors the sig-path tests, but the signature carrier is a `webauthn` assertion
instead of a raw `sig`. The signer here is TEST SCAFFOLDING (ECDSA over p256's
curve math, DER-encoded to match a real ES256 assertion) — it never ships.

The load-bearing case is T3 (advisor gate 13): guards DERIVES the challenge
digest from the grant it will ACT ON. Tampering any signed field after the
assertion breaks the bind — proving render-X-sign-Y cannot slip through the
guards API even though webauthn_verify itself is grant-schema-agnostic.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_grant as hg  # noqa: E402
from guards import p256  # noqa: E402

# A fixed test phone keypair (NOT a real the operator key — test scaffolding).
PHONE_D = 0x00A1B2C3D4E5F60718293A4B5C6D7E8F90112233445566778899AABBCCDDEEFF01
PHONE_PUB = p256._mul(PHONE_D, (p256.GX, p256.GY))


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _der(r: int, s: int) -> bytes:
    def enc(x: int) -> bytes:
        b = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    body = enc(r) + enc(s)
    return b"\x30" + bytes([len(body)]) + body


def _sign_der(digest_int: int, k: int = 0x0FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA9) -> bytes:
    """Test-only ECDSA-P256 sign of a pre-hashed digest → DER."""
    R = p256._mul(k, (p256.GX, p256.GY))
    r = R[0] % p256.N
    s = (pow(k, -1, p256.N) * (digest_int + r * PHONE_D)) % p256.N
    assert r != 0 and s != 0
    return _der(r, s)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _attach_webauthn(grant: dict, *, uv: bool = True, origin: str = "https://mint.local") -> dict:
    """Sign `grant` the way the phone page will: challenge = the on-device digest
    of canonical_bytes(grant) (sig/webauthn excluded), assertion over
    authData || sha256(clientDataJSON)."""
    canon = hg.canonical_bytes(grant)
    challenge = _b64u(hashlib.sha256(canon).digest())
    cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin}).encode()
    flags = 0x05 if uv else 0x01  # UP | (UV)
    authdata = hashlib.sha256(b"mint.local").digest() + bytes([flags]) + (7).to_bytes(4, "big")
    signing_base = authdata + hashlib.sha256(cdj).digest()
    sig_der = _sign_der(int.from_bytes(hashlib.sha256(signing_base).digest(), "big"))
    grant["webauthn"] = {
        "client_data_json": _b64u(cdj),
        "authenticator_data": _b64u(authdata),
        "signature": _b64u(sig_der),
    }
    return grant


class WebAuthnGrantBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = Path(self.tmp.name) / "keys"
        self.keys.mkdir()
        self.grants = Path(self.tmp.name) / "grants"
        env = mock.patch.dict(os.environ, {
            "MAW_GRANT_KEYS_DIR": str(self.keys),
            "MAW_GRANTS_DIR": str(self.grants),
        })
        env.start()
        self.addCleanup(env.stop)
        # A DISTINCT phone pubkey_id (§9.4), committed like any Layer-1 key.
        (self.keys / "test-phone-key.pub").write_text(json.dumps({
            "pubkey_id": "test-phone-key",
            "x": f"{PHONE_PUB[0]:x}", "y": f"{PHONE_PUB[1]:x}",
            "not_before": "2020-01-01T00:00:00Z",
            "not_after": "2099-01-01T00:00:00Z",
        }), encoding="utf-8")

    def make_webauthn_grant(self, instruction="stand down, seat maw-executor",
                            *, record=True, uv=True, **overrides):
        g = {
            "grant_id": overrides.pop("grant_id", "gw-0001"),
            "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
            "scope_class": "seat.standdown",
            "target": "maw-executor",
            "issued_at": _now(),
            "ttl_seconds": 600,
            "nonce": "nw-0001",
            "pubkey_id": "test-phone-key",
            "merge": None,
        }
        g.update(overrides)
        if "instruction_sha256" not in overrides:
            g["instruction_sha256"] = hashlib.sha256(instruction.encode()).hexdigest()
        _attach_webauthn(g, uv=uv)
        if record:
            hg.record_issued(g)
        return g, instruction


class WebAuthnAcceptPath(WebAuthnGrantBase):
    def test_valid_webauthn_grant_verifies(self):
        g, instr = self.make_webauthn_grant()
        res = hg.verify(g, instr, _now())
        self.assertTrue(res.ok, res.reason)

    def test_canonical_bytes_identical_with_or_without_webauthn(self):
        """Excluding `webauthn` must not change the bytes for a grant that also
        has no `sig` — and must match the Mac-path exclusion."""
        g, _ = self.make_webauthn_grant(record=False)
        without = {k: v for k, v in g.items() if k != "webauthn"}
        self.assertEqual(hg.canonical_bytes(g), hg.canonical_bytes(without))

    def test_webauthn_consume_then_replay_rejected(self):
        g, instr = self.make_webauthn_grant()
        self.assertTrue(hg.verify(g, instr, _now()).ok)
        self.assertTrue(hg.consume(g["grant_id"], g["nonce"]))
        self.assertFalse(hg.verify(g, instr, _now()).ok)  # single-use holds for phone grants


class WebAuthnRejectPath(WebAuthnGrantBase):
    def test_t3_tampered_field_breaks_bind(self):
        """T3/§9.5 at the guards level: change a signed field AFTER the assertion,
        and guards' derived digest no longer matches the signed challenge."""
        g, instr = self.make_webauthn_grant()
        g["ttl_seconds"] = 900  # was 600 when the passkey signed
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("webauthn assertion", res.reason)

    def test_uv_off_rejected(self):
        g, instr = self.make_webauthn_grant(uv=False)
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("UV", res.reason)

    def test_both_carriers_rejected(self):
        g, instr = self.make_webauthn_grant()
        g["sig"] = base64.b64encode(b"\x01" * 64).decode()
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("both", res.reason)

    def test_both_carriers_rejected_even_if_other_is_null(self):
        # N1: PRESENCE decides — a null `sig` key alongside webauthn is still an
        # uncovered writable channel and is rejected, not ignored.
        g, instr = self.make_webauthn_grant()
        g["sig"] = None
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("both", res.reason)

    def test_neither_carrier_rejected(self):
        g, instr = self.make_webauthn_grant()
        del g["webauthn"]
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("no signature", res.reason)

    def test_extra_webauthn_key_rejected(self):
        # N1: the webauthn container is strict-keyed to exactly the three fields.
        g, instr = self.make_webauthn_grant()
        g["webauthn"]["evil"] = "riding along"
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("unexpected keys", res.reason)

    def test_unknown_phone_key_rejected(self):
        g, instr = self.make_webauthn_grant(pubkey_id="test-phone-key-UNKNOWN")
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("unknown pubkey_id", res.reason)

    def test_malformed_webauthn_object_rejected(self):
        g, instr = self.make_webauthn_grant()
        g["webauthn"] = "not-an-object"
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)

    def test_malformed_webauthn_encoding_rejected(self):
        g, instr = self.make_webauthn_grant()
        g["webauthn"] = {"client_data_json": "@@@", "authenticator_data": "@@@", "signature": "@@@"}
        res = hg.verify(g, instr, _now())
        self.assertFalse(res.ok)

    def test_instruction_mismatch_still_rejected(self):
        g, _ = self.make_webauthn_grant()
        res = hg.verify(g, "a different instruction", _now())
        self.assertFalse(res.ok)
        self.assertIn("instruction", res.reason)


class WebAuthnMergeApproval(WebAuthnGrantBase):
    def _merge_grant(self, **over):
        head = "a" * 40
        return self.make_webauthn_grant(
            instruction="merge PR 42",
            scope_class="merge.approval",
            target="maw#42",
            merge={"repo": "maw", "pr": 42, "head_sha": head},
            **over,
        )

    def test_merge_approval_webauthn_requires_live_head_and_verdict(self):
        g, instr = self._merge_grant()
        # missing live context → refused even with a valid assertion
        self.assertFalse(hg.verify(g, instr, _now()).ok)
        # SHA drift → refused
        res = hg.verify(g, instr, _now(), live_head_sha="b" * 40, verdict_present=True)
        self.assertFalse(res.ok)
        self.assertIn("SHA-drift", res.reason)
        # no verdict → refused
        res = hg.verify(g, instr, _now(), live_head_sha="a" * 40, verdict_present=False)
        self.assertFalse(res.ok)
        # head matches + verdict present → the grant is the operator's presence, not the review
        res = hg.verify(g, instr, _now(), live_head_sha="a" * 40, verdict_present=True)
        self.assertTrue(res.ok, res.reason)


if __name__ == "__main__":
    unittest.main()
