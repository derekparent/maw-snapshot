"""SE helper integration — the no-double-hash pin, run against the REAL Swift.

An injected-signer test cannot catch a double-hash inside the Swift (advisor,
#130 sequence): CryptoKit's DataProtocol signature overload HASHES its input, so
signing the precomputed digest through the wrong overload would produce
signatures that verify under nothing. The helper's `selftest` verb signs a given
digest with an EPHEMERAL SOFTWARE key through the exact same RawSHA256Digest
path the enclave verb uses — no SE hardware, no Touch ID, no blob touched — and
this test cross-verifies the output under the stdlib guards.p256 verifier.

Build-time-optional: skipped when swiftc is absent (CI without Xcode CLT). The
enclave + biometry leg itself remains the operator-present live-mint ceremony.
"""
from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import p256  # noqa: E402

SRC = Path(__file__).resolve().parents[2] / "maw" / "guards" / "human_grant_helper" / "sign.swift"


@unittest.skipUnless(shutil.which("swiftc"), "swiftc not available")
class SeHelperSelftest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary = str(Path(cls.tmp.name) / "human_grant_sign")
        cp = subprocess.run(
            ["swiftc", "-O", "-o", cls.binary, str(SRC)],
            capture_output=True, text=True, timeout=300,
        )
        if cp.returncode != 0:
            raise unittest.SkipTest(f"helper build failed: {cp.stderr[:200]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _selftest(self, digest_hex: str) -> tuple[tuple[int, int], int, int]:
        cp = subprocess.run([self.binary, "selftest", digest_hex],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        xy, sig_b64 = cp.stdout.split()
        x, y = (int(h, 16) for h in xy.split(":"))
        raw = base64.b64decode(sig_b64)
        self.assertEqual(len(raw), 64)  # raw r||s, never DER
        return (x, y), int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")

    def test_helper_signs_the_exact_digest_not_a_rehash(self) -> None:
        digest_hex = hashlib.sha256(b"cross-check payload").hexdigest()
        pub, r, s = self._selftest(digest_hex)
        digest = int(digest_hex, 16)
        # THE PIN: verifies under guards.p256 against the SAME digest...
        self.assertTrue(p256.verify(pub, digest, r, s))
        # ...and NOT against the double-hashed digest (the wrong-overload world).
        double = int.from_bytes(
            hashlib.sha256(bytes.fromhex(digest_hex)).digest(), "big")
        self.assertFalse(p256.verify(pub, double, r, s))

    def test_malformed_digest_fails_closed(self) -> None:
        cp = subprocess.run([self.binary, "selftest", "zz" * 32],
                            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout, "")


if __name__ == "__main__":
    unittest.main()
