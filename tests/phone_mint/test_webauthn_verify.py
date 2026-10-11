"""phone_mint.webauthn_verify tests — the Option A build-gate proof, in CI.

The headline case verifies a REAL WebAuthn assertion the operator produced on 2026-07-20
(iCloud-synced passkey, Touch ID, UV set) against the committed fixture — the
gate's presence-of-effect, reproducible offline with the stdlib alone. Every
reject class from the threat-model docstring gets a case that BITES: presence-
only (UV off), the §9.5 challenge-substitution split (tampered grant), a wrong
signature, and malformed DER / SPKI / authenticatorData.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards.human_grant import canonical_bytes  # noqa: E402
from phone_mint import webauthn_verify as wv  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "webauthn_assertion_real.json"
# The recorded real passkey assertion is the operator's and stays out of the
# public export, so these tests skip where it is absent.
_NO_REAL = unittest.skipUnless(FIXTURE.is_file(), "recorded real passkey assertion not shipped")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _der_encode(r: int, s: int) -> bytes:
    """Minimal short-form DER for SEQUENCE{INTEGER r, INTEGER s} — test helper."""
    def enc_int(x: int) -> bytes:
        b = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    body = enc_int(r) + enc_int(s)
    return b"\x30" + bytes([len(body)]) + body


@_NO_REAL
class RealAssertionGate(unittest.TestCase):
    """Load the operator's real captured assertion and drive verify_assertion the way lane 2
    will: derive the challenge digest from the grant object, not from the client."""

    def setUp(self):
        self.cap = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.grant = self.cap["grant"]
        self.digest = hashlib.sha256(canonical_bytes(self.grant)).digest()
        self.cdj = wv.b64url_decode(self.cap["clientDataJSON_b64url"])
        self.authdata = wv.b64url_decode(self.cap["authenticatorData_b64url"])
        self.sig = wv.b64url_decode(self.cap["signature_b64url"])
        self.pub = wv.spki_p256_point(wv.b64url_decode(self.cap["spki_pubkey_b64url"]))

    def _verify(self, **over):
        kw = dict(
            challenge_digest=self.digest,
            client_data_json=self.cdj,
            authenticator_data=self.authdata,
            signature_der=self.sig,
            pubkey_point=self.pub,
        )
        kw.update(over)
        return wv.verify_assertion(**kw)

    def test_real_assertion_verifies(self):
        """THE GATE: a real UV passkey assertion bound to the displayed grant."""
        self.assertIsNotNone(self.pub, "SPKI parse must yield a P-256 point")
        res = self._verify()
        self.assertTrue(res.ok, f"real assertion must verify, got: {res.reason}")

    def test_captured_flags_are_up_and_uv(self):
        flags = self.authdata[32]
        self.assertTrue(flags & wv.FLAG_UP, "UP must be set in the real capture")
        self.assertTrue(flags & wv.FLAG_UV, "UV must be set — this was a real Face/Touch ID")

    def test_uv_off_is_rejected(self):
        """Presence-only (UV cleared) must fail N2, even with a valid signature shape."""
        ad = bytearray(self.authdata)
        ad[32] &= ~wv.FLAG_UV  # clear UV, keep UP
        res = self._verify(authenticator_data=bytes(ad))
        self.assertFalse(res.ok)
        self.assertIn("UV", res.reason)

    def test_uv_off_allowed_when_not_required(self):
        ad = bytearray(self.authdata)
        ad[32] &= ~wv.FLAG_UV
        # UV cleared changes the signed bytes, so the signature no longer matches;
        # the point here is only that the UV *gate* is what require_uv=False lifts.
        res = self._verify(authenticator_data=bytes(ad), require_uv=False)
        self.assertNotIn("UV", res.reason)  # not rejected FOR UV

    def test_tampered_grant_breaks_challenge_bind(self):
        """§9.5: change the grant the verifier holds → digest changes → the signed
        challenge no longer matches. This is the render-X-sign-Y defense."""
        tampered = json.loads(json.dumps(self.grant))
        tampered["target"]["pr"] = 1  # The operator authorized pr 999, not 1
        other_digest = hashlib.sha256(canonical_bytes(tampered)).digest()
        res = self._verify(challenge_digest=other_digest)
        self.assertFalse(res.ok)
        self.assertIn("challenge", res.reason)

    def test_wrong_signature_is_rejected(self):
        """A structurally valid DER sig that is cryptographically wrong must fail."""
        rs = wv.der_decode_ecdsa_sig(self.sig)
        self.assertIsNotNone(rs)
        from guards import p256
        wrong = _der_encode(rs[0], (rs[1] + 1) % p256.N)  # perturb s
        res = self._verify(signature_der=wrong)
        self.assertFalse(res.ok)
        self.assertIn("signature", res.reason)

    def test_wrong_key_is_rejected(self):
        from guards import p256
        other = p256._mul(2, self.pub)  # a different valid on-curve point
        res = self._verify(pubkey_point=other)
        self.assertFalse(res.ok)
        self.assertIn("signature", res.reason)


class Helpers(unittest.TestCase):
    def test_der_decode_rejects_non_sequence(self):
        self.assertIsNone(wv.der_decode_ecdsa_sig(b"\x02\x01\x01"))

    def test_der_decode_rejects_truncated(self):
        self.assertIsNone(wv.der_decode_ecdsa_sig(b"\x30\x06\x02\x01"))

    def test_der_decode_rejects_bad_length(self):
        # declared SEQUENCE length does not cover the body
        self.assertIsNone(wv.der_decode_ecdsa_sig(b"\x30\x10\x02\x01\x01\x02\x01\x01"))

    def test_der_roundtrip(self):
        der = _der_encode(0x01, 0xFF)  # 0xFF needs a leading 0x00 (high bit)
        self.assertEqual(wv.der_decode_ecdsa_sig(der), (0x01, 0xFF))

    def test_der_rejects_non_minimal_integer(self):
        # r = 00 01: the leading 0x00 is unnecessary (0x01 has no high bit) — T1.
        self.assertIsNone(wv.der_decode_ecdsa_sig(bytes.fromhex("3008" "020200 01" "020101".replace(" ", ""))))

    def test_der_rejects_negative_high_bit(self):
        # r = 80: high bit set with no leading 0x00 → a negative INTEGER — T1.
        self.assertIsNone(wv.der_decode_ecdsa_sig(bytes.fromhex("3006" "020180" "020101")))

    def test_der_uint_helper(self):
        self.assertEqual(wv._der_uint(b"\x7f"), 0x7F)
        self.assertEqual(wv._der_uint(b"\x00\x80"), 0x80)  # minimal (0x00 needed)
        self.assertIsNone(wv._der_uint(b"\x00\x01"))       # non-minimal
        self.assertIsNone(wv._der_uint(b"\x80"))           # negative
        self.assertIsNone(wv._der_uint(b""))               # empty

    def test_spki_rejects_short(self):
        self.assertIsNone(wv.spki_p256_point(b"\x04" * 10))

    def test_spki_rejects_no_uncompressed_marker(self):
        self.assertIsNone(wv.spki_p256_point(b"\x00" * 91))

    def test_short_authdata_rejected(self):
        res = wv.verify_assertion(
            challenge_digest=b"\x00" * 32,
            client_data_json=json.dumps({"type": "webauthn.get", "challenge": _b64u(b"\x00" * 32)}).encode(),
            authenticator_data=b"\x00" * 20,  # < 37
            signature_der=_der_encode(1, 1),
            pubkey_point=(1, 2),
        )
        self.assertFalse(res.ok)
        self.assertIn("authenticatorData", res.reason)

    def test_non_webauthn_get_type_rejected(self):
        res = wv.verify_assertion(
            challenge_digest=b"\x00" * 32,
            client_data_json=json.dumps({"type": "webauthn.create", "challenge": _b64u(b"\x00" * 32)}).encode(),
            authenticator_data=b"\x00" * 37,
            signature_der=_der_encode(1, 1),
            pubkey_point=(1, 2),
        )
        self.assertFalse(res.ok)
        self.assertIn("webauthn.get", res.reason)

    def test_malformed_client_data_rejected(self):
        res = wv.verify_assertion(
            challenge_digest=b"\x00" * 32,
            client_data_json=b"\xff\xfenot json",
            authenticator_data=b"\x00" * 37,
            signature_der=_der_encode(1, 1),
            pubkey_point=(1, 2),
        )
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main()
