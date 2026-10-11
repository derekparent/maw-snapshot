"""P-256 ECDSA verifier tests — known-answer ACCEPT vectors + a Wycheproof-class
NEGATIVE battery.

The verifier is hand-rolled stdlib (design doc §5a), and the failure mode of a
hand-rolled ECDSA verify is ACCEPTING an invalid signature via curve edge cases.
So the accept side is pinned to RFC 6979 Appendix A.2.5 (the canonical P-256 /
SHA-256 deterministic-ECDSA vectors, verifiable by any independent library) and
the reject side sweeps the edge classes: r/s out of [1, n-1], off-curve /
wrong-curve / infinity / out-of-field public keys, and tamper.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import p256  # noqa: E402

# RFC 6979 A.2.5 — NIST P-256, SHA-256. Public key U = (Ux, Uy).
UX = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
UY = 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299
PUB = (UX, UY)

# (message, r, s) triples for SHA-256, straight from RFC 6979 A.2.5.
VECTORS = {
    b"sample": (
        0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
        0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8,
    ),
    b"test": (
        0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
        0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083,
    ),
}


def _digest(msg: bytes) -> int:
    return int.from_bytes(hashlib.sha256(msg).digest(), "big")


class AcceptVectors(unittest.TestCase):
    def test_rfc6979_sample_verifies(self) -> None:
        r, s = VECTORS[b"sample"]
        self.assertTrue(p256.verify(PUB, _digest(b"sample"), r, s))

    def test_rfc6979_test_verifies(self) -> None:
        r, s = VECTORS[b"test"]
        self.assertTrue(p256.verify(PUB, _digest(b"test"), r, s))

    def test_on_curve_helper_accepts_the_public_key(self) -> None:
        self.assertTrue(p256._on_curve(PUB))
        self.assertTrue(p256._on_curve((p256.GX, p256.GY)))  # the generator


class RejectBattery(unittest.TestCase):
    def setUp(self) -> None:
        self.r, self.s = VECTORS[b"sample"]
        self.d = _digest(b"sample")

    # --- r/s range (the classic malleability / zero-scalar traps) ---
    def test_reject_r_zero(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, 0, self.s))

    def test_reject_s_zero(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, self.r, 0))

    def test_reject_r_equals_n(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, p256.N, self.s))

    def test_reject_s_equals_n(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, self.r, p256.N))

    def test_reject_r_above_n(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, p256.N + 1, self.s))

    def test_reject_negative_s(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, self.r, -1))

    # --- public-key validation ---
    def test_reject_off_curve_pubkey(self) -> None:
        # A point one off the real y is not on the curve.
        self.assertFalse(p256.verify((UX, UY ^ 1), self.d, self.r, self.s))

    def test_reject_point_at_infinity_pubkey(self) -> None:
        self.assertFalse(p256.verify(None, self.d, self.r, self.s))

    def test_reject_out_of_field_pubkey(self) -> None:
        self.assertFalse(p256.verify((p256.P + UX, UY), self.d, self.r, self.s))

    def test_reject_wrong_curve_pubkey(self) -> None:
        # The secp256k1 generator is NOT on P-256.
        k1_gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
        k1_gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
        self.assertFalse(p256._on_curve((k1_gx, k1_gy)))
        self.assertFalse(p256.verify((k1_gx, k1_gy), self.d, self.r, self.s))

    # --- tamper ---
    def test_reject_wrong_digest(self) -> None:
        self.assertFalse(p256.verify(PUB, _digest(b"not the signed message"),
                                     self.r, self.s))

    def test_reject_swapped_r_s(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, self.s, self.r))

    def test_reject_sig_under_other_key(self) -> None:
        # The generator is a valid on-curve point but not the signer's key.
        self.assertFalse(p256.verify((p256.GX, p256.GY), self.d, self.r, self.s))

    def test_reject_flipped_r_bit(self) -> None:
        self.assertFalse(p256.verify(PUB, self.d, self.r ^ 1, self.s))


if __name__ == "__main__":
    unittest.main()
