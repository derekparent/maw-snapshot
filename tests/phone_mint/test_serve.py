"""phone_mint.serve — registration + mint server logic and a live localhost round-trip.

The transport (tailscale serve / ts.net cert) is not testable in CI, but every
piece that runs ON this host is: SPKI→.pub extraction, the body-size cap (N4),
zero-trust payload validation, the window-only lifecycle (N5), and a real
127.0.0.1 round-trip that drives register + mint through actual HTTP and asserts
the produced grant verifies under guards.human_grant.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_grant as hg  # noqa: E402
from guards import p256  # noqa: E402
from phone_mint import mint_session as ms  # noqa: E402
from phone_mint import serve  # noqa: E402

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

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "webauthn_assertion_real.json"
# The recorded real passkey assertion is the operator's and stays out of the
# public export, so these tests skip where it is absent.
_NO_REAL = unittest.skipUnless(FIXTURE.is_file(), "recorded real passkey assertion not shipped")

# Test phone keypair (curve-math scaffolding; not a real passkey).
PHONE_D = 0x00A1B2C3D4E5F60718293A4B5C6D7E8F90112233445566778899AABBCCDDEEFF01
PHONE_PUB = p256._mul(PHONE_D, (p256.GX, p256.GY))


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _der(r: int, s: int) -> bytes:
    def enc(x):
        b = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    body = enc(r) + enc(s)
    return b"\x30" + bytes([len(body)]) + body


def _sign_assertion(grant: dict, *, uv: bool = True):
    challenge = _b64u(ms.challenge_digest(grant))
    cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": "https://x.ts.net"}).encode()
    authdata = hashlib.sha256(b"x.ts.net").digest() + bytes([0x05 if uv else 0x01]) + (1).to_bytes(4, "big")
    digest = int.from_bytes(hashlib.sha256(authdata + hashlib.sha256(cdj).digest()).digest(), "big")
    k = 0x0FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA9
    R = p256._mul(k, (p256.GX, p256.GY))
    r = R[0] % p256.N
    s = (pow(k, -1, p256.N) * (digest + r * PHONE_D)) % p256.N
    return _b64u(cdj), _b64u(authdata), _b64u(_der(r, s))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _post(port: int, path: str, obj: dict, raw: "bytes | None" = None):
    data = raw if raw is not None else json.dumps(obj).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


class BuildPubkeyRecord(unittest.TestCase):
    @_NO_REAL
    def test_extracts_point_from_real_spki(self):
        cap = json.loads(FIXTURE.read_text(encoding="utf-8"))
        rec = serve.build_pubkey_record(cap["spki_pubkey_b64url"],
                                        pubkey_id="test-phone-key",
                                        not_before="2026-01-01T00:00:00Z",
                                        not_after="2099-01-01T00:00:00Z")
        self.assertEqual(rec["pubkey_id"], "test-phone-key")
        # the record loads back as a usable P-256 point
        self.assertEqual(int(rec["x"], 16).bit_length() > 0, True)
        self.assertTrue(p256._on_curve((int(rec["x"], 16), int(rec["y"], 16))))

    def test_rejects_non_p256_spki(self):
        with self.assertRaises(ValueError):
            serve.build_pubkey_record(_b64u(b"\x00" * 40), pubkey_id="x",
                                      not_before="a", not_after="b")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            serve.build_pubkey_record("", pubkey_id="x", not_before="a", not_after="b")

    def test_rejects_off_curve_point(self):
        # M1: an SPKI whose trailing point (0x04||x||y) is off-curve must be
        # rejected at capture, not become a dud .pub. (1,1) is not on P-256.
        spki = b"\x00" * 26 + b"\x04" + (1).to_bytes(32, "big") + (1).to_bytes(32, "big")
        self.assertFalse(p256._on_curve((1, 1)))
        with self.assertRaises(ValueError):
            serve.build_pubkey_record(_b64u(spki), pubkey_id="x",
                                      not_before="a", not_after="b")


class PageEscaping(unittest.TestCase):
    """MAJOR: a grant field must not break out of the inline <script> and hand the
    grant author page-script control (render-X-sign-Y through the front door)."""

    def _grant(self, target):
        return {"grant_id": "g", "instruction_sha256": "x", "scope_class": "seat.standdown",
                "target": target, "issued_at": "t", "ttl_seconds": 600,
                "session_token_ref": None, "nonce": "n",
                "pubkey_id": "test-phone-key", "merge": None}

    def test_script_breakout_neutralized(self):
        g = self._grant("</script><img src=x onerror=alert(1)>")
        page = serve._page("mint", g).decode()
        after_first = page[page.find("<script>"):]
        # exactly ONE literal </script> — the real terminator; the injected one is escaped
        self.assertEqual(after_first.count("</script>"), 1)
        self.assertNotIn("</script><img", page)
        self.assertIn("\\u003c/script", page)  # the escaped form is present

    def test_escape_is_json_identical_canon_preserved(self):
        # The escaped embedding must parse back to the SAME grant, so the phone's
        # on-device canon() (and thus the §9.5 challenge) is byte-for-byte unchanged.
        g = self._grant("</script>weird & <b>text</b>")
        page = serve._page("mint", g).decode()
        m = re.search(r"const KIND = .*?, GRANT = (.*?), CAPTURE = .*?;\n", page, re.S)
        self.assertIsNotNone(m)
        embedded = json.loads(m.group(1))
        self.assertEqual(embedded, g)  # round-trips to the original grant
        # therefore canonical_bytes(embedded) == canonical_bytes(g)
        self.assertEqual(hg.canonical_bytes(embedded), hg.canonical_bytes(g))


class ServerRoundTrip(unittest.TestCase):
    """Drive the actual HTTP server on 127.0.0.1 in a thread."""

    def _run_server(self, srv: serve.MintServer):
        t = threading.Thread(target=srv.serve, daemon=True)
        t.start()
        self.assertTrue(srv.bound.wait(timeout=5), "server never bound")
        return t

    @_NO_REAL
    def test_register_roundtrip(self):
        cap = json.loads(FIXTURE.read_text(encoding="utf-8"))
        srv = serve.MintServer(kind="register", window_seconds=5, port=0)
        t = self._run_server(srv)
        code, _ = _post(srv.port, "/register-capture", {"spki_pubkey_b64url": cap["spki_pubkey_b64url"]})
        t.join(timeout=3)
        self.assertEqual(code, 200)
        self.assertIsNotNone(srv.result)
        self.assertEqual(srv.result["pubkey_id"], "test-phone-key")

    def test_mint_roundtrip_produces_verifiable_grant(self):
        with tempfile_keys() as _:
            instr = "stand down, seat maw-executor"
            grant = ms.assemble_unsigned_grant(instruction=instr, scope_class="seat.standdown",
                                               target="maw-executor", ttl_seconds=600, now_iso=_now())
            cdj, ad, sig = _sign_assertion(grant)
            srv = serve.MintServer(kind="mint", grant=grant, pubkey_point=PHONE_PUB,
                                   window_seconds=5, port=0)
            t = self._run_server(srv)
            code, _ = _post(srv.port, "/mint-capture", {
                "client_data_json_b64": cdj, "authenticator_data_b64": ad, "signature_b64": sig})
            t.join(timeout=3)
            self.assertEqual(code, 200)
            self.assertIsNotNone(srv.result)
            # the produced grant verifies under guards, single-use ledger and all
            self.assertTrue(hg.verify(srv.result, instr, _now()).ok)

    def test_oversize_body_refused(self):
        srv = serve.MintServer(kind="register", window_seconds=3, port=0)
        t = self._run_server(srv)
        big = b"{" + b"a" * (serve.MAX_BODY_BYTES + 10)
        code, _ = _post(srv.port, "/register-capture", {}, raw=big)
        self.assertEqual(code, 413)
        srv._done.set(); t.join(timeout=3)

    def test_bad_payload_refused_and_recorded(self):
        srv = serve.MintServer(kind="register", window_seconds=3, port=0)
        t = self._run_server(srv)
        code, _ = _post(srv.port, "/register-capture", {"spki_pubkey_b64url": "not-a-key"})
        self.assertEqual(code, 400)
        self.assertIsNotNone(srv.error)
        srv._done.set(); t.join(timeout=3)

    def test_wrong_path_404(self):
        srv = serve.MintServer(kind="register", window_seconds=3, port=0)
        t = self._run_server(srv)
        code, _ = _post(srv.port, "/nope", {"x": 1})
        self.assertEqual(code, 404)
        srv._done.set(); t.join(timeout=3)


class Lifecycle(unittest.TestCase):
    def test_window_elapses_without_capture(self):
        """N5: no capture within the window → serve() returns None and stops."""
        srv = serve.MintServer(kind="register", window_seconds=1, port=0)
        start = time.monotonic()
        out = srv.serve()
        self.assertIsNone(out)
        self.assertLess(time.monotonic() - start, 4)  # actually stopped, not hung

    def test_mint_requires_grant_and_key(self):
        with self.assertRaises(ValueError):
            serve.MintServer(kind="mint", window_seconds=1, port=0)

    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            serve.MintServer(kind="bogus", window_seconds=1, port=0)


class _tempfile_keys:
    """Context manager: an isolated keyset + ledger with the test phone key."""

    def __enter__(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        keys = Path(self.tmp.name) / "keys"
        keys.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "MAW_GRANT_KEYS_DIR": str(keys),
            "MAW_GRANTS_DIR": str(Path(self.tmp.name) / "grants"),
        })
        self.env.start()
        (keys / "test-phone-key.pub").write_text(json.dumps({
            "pubkey_id": "test-phone-key",
            "x": f"{PHONE_PUB[0]:x}", "y": f"{PHONE_PUB[1]:x}",
            "not_before": "2020-01-01T00:00:00Z", "not_after": "2099-01-01T00:00:00Z",
        }), encoding="utf-8")
        return self

    def __exit__(self, *a):
        self.env.stop()
        self.tmp.cleanup()
        return False


def tempfile_keys():
    return _tempfile_keys()


if __name__ == "__main__":
    unittest.main()


class EphemeralPort(unittest.TestCase):
    def test_port_zero_binds_a_free_port_and_reports_it(self):
        """Fixed test ports collided when two suites ran at once (a review
        worktree beside a seat's own run). port=0 lets the OS choose, and
        serve() reports the port it actually bound."""
        a = serve.MintServer(kind="register", window_seconds=2, port=0)
        b = serve.MintServer(kind="register", window_seconds=2, port=0)
        ta = threading.Thread(target=a.serve, daemon=True)
        tb = threading.Thread(target=b.serve, daemon=True)
        ta.start(); tb.start()
        self.assertTrue(a.bound.wait(5) and b.bound.wait(5))
        self.assertNotEqual(a.port, 0)
        self.assertNotEqual(a.port, b.port)
        a._done.set(); b._done.set()
        ta.join(timeout=3); tb.join(timeout=3)
