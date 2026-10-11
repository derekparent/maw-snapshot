"""phone_mint.mint_session — assemble + attach a phone-minted grant (§9.2/§9.5).

Covers the CI-testable core of lane 3: the grant produced is shape-identical to
guards.mint's (drift-pinned), the TTL clock starts at the mint request, and an
assertion the page collected round-trips through attach_assertion into a grant
guards.human_grant.verify() accepts. The signer is test scaffolding (curve math),
not a real passkey; the real-assertion crypto proof is #151's fixture.
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
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_grant as hg  # noqa: E402
from guards import mint as gmint  # noqa: E402
from guards import p256  # noqa: E402
from phone_mint import mint_session as ms  # noqa: E402

# Neutral key ids: the suite never reads the operator's ~/.maw/operator.toml
# (tests/run_all.py isolates it), so the minting tools get theirs from here.
TEST_KEY_IDS = {"MAW_GRANT_PUBKEY_ID": "test-grant-key",
                "MAW_PHONE_PUBKEY_ID": "test-phone-key",
                "MAW_RELAY_PUBKEY_ID": "test-relay-key"}
_KEY_ID_ENV = mock.patch.dict(os.environ, TEST_KEY_IDS)


def setUpModule() -> None:
    _KEY_ID_ENV.start()


def tearDownModule() -> None:
    _KEY_ID_ENV.stop()

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


def _sign_assertion(grant: dict, *, uv: bool = True):
    """Produce (client_data_json_b64, authenticator_data_b64, signature_b64) the
    way the phone page will: challenge = the on-device digest of THIS grant."""
    challenge = _b64u(ms.challenge_digest(grant))
    cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": "https://mint.ts.net"}).encode()
    flags = 0x05 if uv else 0x01
    authdata = hashlib.sha256(b"mint.ts.net").digest() + bytes([flags]) + (3).to_bytes(4, "big")
    base = authdata + hashlib.sha256(cdj).digest()
    digest = int.from_bytes(hashlib.sha256(base).digest(), "big")
    k = 0x0FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA9
    R = p256._mul(k, (p256.GX, p256.GY))
    r = R[0] % p256.N
    s = (pow(k, -1, p256.N) * (digest + r * PHONE_D)) % p256.N
    return _b64u(cdj), _b64u(authdata), _b64u(_der(r, s))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Assemble(unittest.TestCase):
    def test_shape_parity_with_guards_mint(self):
        """The phone grant's field set == guards.mint's grant minus the signature.
        Pins drift: if mint() gains/loses a field, this fails."""
        g = ms.assemble_unsigned_grant(
            instruction="stand down", scope_class="seat.standdown",
            target="maw-executor", ttl_seconds=600, now_iso=_now())
        mac = gmint.mint(
            "stand down", "seat.standdown", "maw-executor", 600,
            "test-grant-key", _now(), signer=lambda d, r: base64.b64encode(b"\x01" * 64).decode(),
            show=lambda *_: None, record=False)
        self.assertEqual(set(g.keys()), set(mac.keys()) - {"sig"})

    def test_ttl_clock_is_request_time(self):
        t = "2026-07-20T05:00:00Z"
        g = ms.assemble_unsigned_grant(
            instruction="x", scope_class="seat.standdown", target="s",
            ttl_seconds=600, now_iso=t)
        self.assertEqual(g["issued_at"], t)  # NOT signature time

    def test_rejects_bad_scope(self):
        with self.assertRaises(ValueError):
            ms.assemble_unsigned_grant(instruction="x", scope_class="nope",
                                       target="s", ttl_seconds=600, now_iso=_now())

    def test_rejects_bad_ttl(self):
        for bad in (0, -1, hg.MAX_TTL_SECONDS + 1, True):
            with self.assertRaises(ValueError):
                ms.assemble_unsigned_grant(instruction="x", scope_class="seat.standdown",
                                           target="s", ttl_seconds=bad, now_iso=_now())

    def test_rejects_empty_instruction(self):
        with self.assertRaises(ValueError):
            ms.assemble_unsigned_grant(instruction="", scope_class="seat.standdown",
                                       target="s", ttl_seconds=600, now_iso=_now())

    def test_merge_shape_enforced(self):
        with self.assertRaises(ValueError):  # merge.approval needs a merge object
            ms.assemble_unsigned_grant(instruction="m", scope_class="merge.approval",
                                       target="t", ttl_seconds=600, now_iso=_now())
        with self.assertRaises(ValueError):  # merge on a non-merge scope
            ms.assemble_unsigned_grant(instruction="m", scope_class="seat.standdown",
                                       target="t", ttl_seconds=600, now_iso=_now(),
                                       merge={"repo": "maw", "pr": 1, "head_sha": "a" * 40})


class AttachBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        keys = Path(self.tmp.name) / "keys"
        keys.mkdir()
        env = mock.patch.dict(os.environ, {
            "MAW_GRANT_KEYS_DIR": str(keys),
            "MAW_GRANTS_DIR": str(Path(self.tmp.name) / "grants"),
        })
        env.start()
        self.addCleanup(env.stop)
        (keys / "test-phone-key.pub").write_text(json.dumps({
            "pubkey_id": "test-phone-key",
            "x": f"{PHONE_PUB[0]:x}", "y": f"{PHONE_PUB[1]:x}",
            "not_before": "2020-01-01T00:00:00Z", "not_after": "2099-01-01T00:00:00Z",
        }), encoding="utf-8")

    def _assemble(self, **over):
        kw = dict(instruction="stand down, seat maw-executor",
                  scope_class="seat.standdown", target="maw-executor",
                  ttl_seconds=600, now_iso=_now())
        kw.update(over)
        return ms.assemble_unsigned_grant(**kw)


class AttachRoundTrip(AttachBase):
    def test_assemble_attach_then_guards_verify_accepts(self):
        instr = "stand down, seat maw-executor"
        g = self._assemble(instruction=instr)
        cdj, ad, sig = _sign_assertion(g)
        out = ms.attach_assertion(g, client_data_json_b64=cdj, authenticator_data_b64=ad,
                                  signature_b64=sig, pubkey_point=PHONE_PUB)
        self.assertIn("webauthn", out)
        self.assertTrue(hg.verify(out, instr, _now()).ok)

    def test_ttl_at_request_expires_regardless_of_attach_time(self):
        """§9.2: a grant assembled at T expires at T+ttl even if the assertion is
        attached and verified later. Assemble in the past → already expired."""
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 700))
        instr = "late ceremony"
        g = self._assemble(instruction=instr, ttl_seconds=600, now_iso=past)
        cdj, ad, sig = _sign_assertion(g)
        out = ms.attach_assertion(g, client_data_json_b64=cdj, authenticator_data_b64=ad,
                                  signature_b64=sig, pubkey_point=PHONE_PUB)
        res = hg.verify(out, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("expired", res.reason)


class AttachReject(AttachBase):
    def test_double_sign_rejected(self):
        g = self._assemble()
        cdj, ad, sig = _sign_assertion(g)
        ms.attach_assertion(g, client_data_json_b64=cdj, authenticator_data_b64=ad,
                            signature_b64=sig, pubkey_point=PHONE_PUB, record=False)
        with self.assertRaises(ValueError):  # already carries webauthn
            ms.attach_assertion(g, client_data_json_b64=cdj, authenticator_data_b64=ad,
                                signature_b64=sig, pubkey_point=PHONE_PUB, record=False)

    def test_non_string_field_rejected(self):
        g = self._assemble()
        cdj, ad, sig = _sign_assertion(g)
        with self.assertRaises(ValueError):
            ms.attach_assertion(g, client_data_json_b64=None, authenticator_data_b64=ad,
                                signature_b64=sig, pubkey_point=PHONE_PUB)

    def test_bad_base64_rejected(self):
        g = self._assemble()
        cdj, ad, sig = _sign_assertion(g)
        with self.assertRaises(ValueError):
            ms.attach_assertion(g, client_data_json_b64="!!!not b64!!!",
                                authenticator_data_b64=ad, signature_b64=sig,
                                pubkey_point=PHONE_PUB)

    def test_uv_off_assertion_rejected(self):
        g = self._assemble()
        cdj, ad, sig = _sign_assertion(g, uv=False)
        with self.assertRaises(ValueError) as cm:
            ms.attach_assertion(g, client_data_json_b64=cdj, authenticator_data_b64=ad,
                                signature_b64=sig, pubkey_point=PHONE_PUB)
        self.assertIn("UV", str(cm.exception))

    def test_wrong_key_rejected(self):
        g = self._assemble()
        cdj, ad, sig = _sign_assertion(g)
        other = p256._mul(2, PHONE_PUB)
        with self.assertRaises(ValueError):
            ms.attach_assertion(g, client_data_json_b64=cdj, authenticator_data_b64=ad,
                                signature_b64=sig, pubkey_point=other)

    def test_assertion_for_different_grant_rejected(self):
        """An assertion signed over grant A cannot be attached to grant B (the
        challenge digest differs) — the mint-side §9.5 self-check."""
        g_a = self._assemble()
        cdj, ad, sig = _sign_assertion(g_a)
        g_b = self._assemble()  # fresh grant_id/nonce → different canonical bytes
        with self.assertRaises(ValueError):
            ms.attach_assertion(g_b, client_data_json_b64=cdj, authenticator_data_b64=ad,
                                signature_b64=sig, pubkey_point=PHONE_PUB)


if __name__ == "__main__":
    unittest.main()
