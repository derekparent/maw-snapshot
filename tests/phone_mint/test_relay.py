"""The hosted approval relay (refit R3b): relay/worker.mjs + phone_mint.relay_client.

The Worker runs for real: relay/dev_server.mjs serves its handler over localhost
with an in-memory store (the stand-in for `wrangler dev`). Its own `node --test`
suite also runs from here, so the canonical Python runner counts it. Both need
node; they skip when it is absent. The page.mjs parity check needs nothing.

The "phone" is test scaffolding: it GETs the page the relay serves, parses the
grant the page embeds, derives the challenge from exactly those bytes (as the
real page does), and signs with a fixed TEST key, never an operator key.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "maw"))

from guards import human_grant as hg  # noqa: E402
from guards import p256  # noqa: E402
from phone_mint import mint_session as ms  # noqa: E402
from phone_mint import relay_client as rc  # noqa: E402

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

NODE = shutil.which("node")
PHONE_D = 0x00A1B2C3D4E5F60718293A4B5C6D7E8F90112233445566778899AABBCCDDEEFF01
PHONE_PUB = p256._mul(PHONE_D, (p256.GX, p256.GY))
SECRET = "relay-test-secret-" + "x" * 30


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _der(r: int, s: int) -> bytes:
    def enc(x: int) -> bytes:
        b = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
        return b"\x02" + bytes([len(b) + (b[0] >> 7)]) + (b"\x00" if b[0] & 0x80 else b"") + b
    body = enc(r) + enc(s)
    return b"\x30" + bytes([len(body)]) + body


def phone_sign(displayed_grant: dict, origin: str = "https://relay.example") -> dict:
    """What the real page does: challenge = sha256(canon(displayed grant)), made
    on the page's origin, with rpIdHash = sha256(the origin's hostname)."""
    from urllib.parse import urlsplit
    challenge = _b64u(hashlib.sha256(hg.canonical_bytes(displayed_grant)).digest())
    cdj = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin}).encode()
    host = urlsplit(origin).hostname or ""
    authdata = hashlib.sha256(host.encode()).digest() + bytes([0x05]) + (7).to_bytes(4, "big")
    digest = int.from_bytes(hashlib.sha256(authdata + hashlib.sha256(cdj).digest()).digest(), "big")
    k = 0x0FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA90FEDCBA9
    R = p256._mul(k, (p256.GX, p256.GY))
    r = R[0] % p256.N
    s = (pow(k, -1, p256.N) * (digest + r * PHONE_D)) % p256.N
    return {"client_data_json_b64": _b64u(cdj), "authenticator_data_b64": _b64u(authdata),
            "signature_b64": _b64u(_der(r, s))}


class RelayUrl(unittest.TestCase):
    def test_only_https_or_a_true_localhost_is_accepted(self) -> None:
        for ok in ("https://relay.example", "https://relay.example/", "http://127.0.0.1:8787",
                   "http://localhost:9"):
            self.assertTrue(rc.relay_url(ok))
        for bad in ("http://127.0.0.1.evil.example", "http://localhost.evil.example",
                    "http://relay.example", "ftp://relay.example", "https://u:p@relay.example",
                    "https://relay.example/?x=1", "https://"):
            with self.subTest(bad), self.assertRaises(rc.RelayError):
                rc.relay_url(bad)


class RelayUrlConfig(unittest.TestCase):
    def test_url_comes_from_the_deploy_record_when_not_given(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "url"
            f.write_text("https://maw-approval-relay.example.workers.dev\n", encoding="utf-8")
            clean = {k: v for k, v in os.environ.items() if k != "MAW_RELAY_URL"}
            with mock.patch.dict(os.environ, {**clean, "MAW_RELAY_URL_FILE": str(f)}, clear=True):
                self.assertEqual(rc.relay_url(), "https://maw-approval-relay.example.workers.dev")
                f.unlink()
                with self.assertRaisesRegex(rc.RelayError, "relay/deploy.sh"):
                    rc.relay_url()


class DeployScript(unittest.TestCase):
    """relay/deploy.sh end to end against a stub wrangler (no account, no network)."""

    def test_secret_put_receives_exactly_the_secret_the_mac_sends(self) -> None:
        self.addCleanup(lambda: (ROOT / "relay" / ".deploy.wrangler.toml").unlink(missing_ok=True))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            got = root / "stdin.bin"
            stub = root / "wrangler"
            stub.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  whoami) echo "You are logged in with an OAuth Token";;\n'
                '  kv) echo \'id = "0123456789abcdef0123456789abcdef"\';;\n'
                f'  secret) cat > "{got}";;\n'
                '  deploy) echo "Deployed https://maw-approval-relay.example.workers.dev";;\n'
                "esac\n", encoding="utf-8")
            os.chmod(stub, 0o755)
            env = {**os.environ, "HOME": str(home), "MAW_RELAY_WRANGLER": str(stub)}
            env.pop("MAW_RELAY_SECRET_FILE", None)
            cp = subprocess.run(["bash", str(ROOT / "relay" / "deploy.sh")], env=env,
                                capture_output=True, text=True, timeout=60)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            secret_file = home / ".secrets" / "maw-relay-secret"
            self.assertEqual(oct(secret_file.stat().st_mode & 0o777), "0o600")
            with mock.patch.dict(os.environ, {"MAW_RELAY_SECRET_FILE": str(secret_file)}):
                secret = rc.read_secret()
            self.assertEqual(got.read_bytes(), secret.encode("utf-8"))
            self.assertNotIn(secret, cp.stdout + cp.stderr)  # never echoed
            self.assertEqual((home / ".maw" / "relay" / "url").read_text().strip(),
                             "https://maw-approval-relay.example.workers.dev")


class ClientUserAgent(unittest.TestCase):
    def test_every_call_sends_the_maw_user_agent(self) -> None:
        seen = []

        class Resp:
            status = 202
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header("User-agent"))
            return Resp()

        with mock.patch.object(rc.urllib.request, "urlopen", fake_urlopen):
            rc._call("GET", "https://relay.example/api/result/" + "0" * 32, "s" * 40)
            rc._call("POST", "https://relay.example/api/request", "s" * 40, {"kind": "register"})
            rc._call("DELETE", "https://relay.example/api/result/" + "0" * 32, "s" * 40)
        self.assertEqual(seen, ["maw-relay-client/1"] * 3)


class PageParity(unittest.TestCase):
    @unittest.skipIf(NODE is None, "node not installed")
    def test_relay_render_is_byte_identical_to_serve_py(self) -> None:
        from phone_mint import serve
        g = {"grant_id": "g-1", "ttl_seconds": 600, "scope_class": "goal.stage",
             "target": "a$'b__TITLE__c__GRANT_JSON__</script>\u00e9", "n": [1, 2], "none": None}
        cases = [("mint", g, "/r/abc/capture"), ("register", None, "/r/abc/capture")]
        script = ("import('" + (ROOT / "relay" / "handler.mjs").as_uri() + "').then(m => {"
                  "const cs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
                  "process.stdout.write(JSON.stringify(cs.map(c => m.renderPage(c[0], c[1], c[2]))));})")
        out = subprocess.run([NODE, "-e", script], input=json.dumps(cases), capture_output=True,
                             text=True, timeout=60, check=True).stdout
        for (kind, grant, cap), js_html in zip(cases, json.loads(out)):
            with self.subTest(kind):
                self.assertEqual(js_html, serve._page(kind, grant, cap).decode("utf-8"))
        self.assertNotIn("__TITLE__", json.loads(out)[0].split("<script>")[0])

    def test_challenge_prefix_is_the_page_challenge(self) -> None:
        g = {"grant_id": "g-1", "ttl_seconds": 600}
        self.assertEqual(rc.challenge_prefix(g), _b64u(ms.challenge_digest(g))[:12])

    @unittest.skipIf(NODE is None, "node not installed")
    def test_page_fingerprint_matches_the_relays_own(self) -> None:
        # batch-arm R20: mint-batch compares these two, so they must agree on the
        # same template (UTF-8, the em dash in the title included).
        script = ("import('" + (ROOT / "relay" / "handler.mjs").as_uri() + "')"
                  ".then(m => m.pageSha256()).then(h => process.stdout.write(h))")
        out = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                             timeout=60, check=True).stdout
        self.assertEqual(out, rc.page_fingerprint())

    def test_deployed_page_sha_is_empty_when_the_relay_has_none(self) -> None:
        with mock.patch.object(rc, "_call", return_value=(404, b"not found")):
            self.assertEqual(rc.deployed_page_sha(relay="https://r.example", secret="s" * 40), "")
        with mock.patch.object(rc, "_call", return_value=(200, b'{"page_sha256": "nope"}')):
            self.assertEqual(rc.deployed_page_sha(relay="https://r.example", secret="s" * 40), "")
        good = "a" * 64
        with mock.patch.object(rc, "_call", return_value=(200, json.dumps({"page_sha256": good}).encode())):
            self.assertEqual(rc.deployed_page_sha(relay="https://r.example", secret="s" * 40), good)

    def test_committed_page_mjs_is_generated_from_serve_py(self) -> None:
        self.assertEqual((ROOT / "relay" / "page.mjs").read_text(encoding="utf-8"),
                         rc.page_module_source(),
                         "relay/page.mjs drifted: run `python3 -m phone_mint.relay_client gen-page`")


@unittest.skipIf(NODE is None, "node not installed")
class WorkerNodeSuite(unittest.TestCase):
    def test_worker_node_tests_pass(self) -> None:
        cp = subprocess.run([NODE, "--test", str(ROOT / "relay" / "test" / "worker.test.mjs")],
                            capture_output=True, text=True, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout[-2000:] + cp.stderr[-2000:])
        self.assertRegex(cp.stdout, r"ℹ fail 0")


@unittest.skipIf(NODE is None, "node not installed")
class RelayEndToEnd(unittest.TestCase):
    tamper = False

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "keys").mkdir()
        (root / "keys" / f"{rc.relay_pubkey_id()}.pub").write_text(json.dumps({
            "pubkey_id": rc.relay_pubkey_id(), "x": f"{PHONE_PUB[0]:x}",
            "y": f"{PHONE_PUB[1]:x}", "not_before": "2020-01-01T00:00:00Z",
            "not_after": "2099-01-01T00:00:00Z"}), encoding="utf-8")
        self.secret_file = root / "secret"
        self.secret_file.write_text(SECRET, encoding="utf-8")
        os.chmod(self.secret_file, 0o600)
        env = mock.patch.dict(os.environ, {
            "MAW_GRANT_KEYS_DIR": str(root / "keys"), "MAW_GRANTS_DIR": str(root / "grants"),
            "MAW_RELAY_PENDING_DIR": str(root / "pending"),
            "MAW_RELAY_SECRET_FILE": str(self.secret_file)})
        env.start()
        self.addCleanup(env.stop)
        hg.ack_keyset()
        self.relay = self._start()

    def _start(self) -> str:
        args = [NODE, str(ROOT / "relay" / "dev_server.mjs"), "--port", "0"]
        if self.tamper:
            args.append("--tamper")
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env={**os.environ, "RELAY_SECRET": SECRET})
        self.addCleanup(self._stop)
        line = self.proc.stdout.readline()
        m = re.match(r"listening (\d+)", line)
        self.assertIsNotNone(m, f"dev server did not start: {line!r} {self.proc.stderr.read() if self.proc.poll() is not None else ''}")
        return f"http://127.0.0.1:{m.group(1)}"

    def _stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def unsigned(self) -> dict:
        session = {"repo": "example-org/maw", "seat": "advisor", "session_id": "advisor-s1",
                   "scope": "maw refit run: R3b relay then the tail lanes"}
        return ms.assemble_unsigned_grant(
            instruction=hg.session_instruction("session.arm", session), scope_class="session.arm",
            target="advisor", ttl_seconds=600, now_iso=time.strftime(hg._TS_FMT, time.gmtime()),
            pubkey_id=rc.relay_pubkey_id(), session=session)

    def phone_open(self, url: str) -> "tuple[dict, str]":
        with urllib.request.urlopen(url, timeout=10) as r:
            html = r.read().decode("utf-8")
        m = re.search(r"GRANT = (.*?), CAPTURE = (\".*?\");\n", html, re.S)
        self.assertIsNotNone(m, "page does not embed GRANT/CAPTURE")
        return json.loads(m.group(1)), json.loads(m.group(2))

    def phone_capture(self, capture_path: str, payload: dict) -> int:
        req = urllib.request.Request(self.relay + capture_path, data=json.dumps(payload).encode(),
                                     method="POST", headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_mac_post_phone_sign_mac_pull_verifies_offline(self) -> None:
        g = self.unsigned()
        rid, url = rc.post("mint", g, relay=self.relay, secret=SECRET)
        self.assertIsNone(rc.pull(rid, secret=SECRET))  # not approved yet: one pull, no wait
        shown, cap = self.phone_open(url)
        self.assertEqual(shown, g)
        self.assertEqual(cap, f"/r/{rid}/capture")
        self.assertEqual(self.phone_capture(cap, phone_sign(shown, self.relay)), 200)
        done = rc.pull(rid, secret=SECRET)
        self.assertIn("webauthn", done)
        self.assertTrue(hg.verify(done, hg.session_instruction("session.arm", g["session"]),
                                  time.strftime(hg._TS_FMT, time.gmtime()),
                                  ceremony=True).ok)
        with self.assertRaisesRegex(rc.RelayError, "no local copy"):
            rc.pull(rid, secret=SECRET)  # pull once

    def test_an_assertion_from_another_origin_is_refused(self) -> None:
        g = self.unsigned()
        rid, url = rc.post("mint", g, relay=self.relay, secret=SECRET)
        shown, cap = self.phone_open(url)
        self.assertEqual(self.phone_capture(cap, phone_sign(shown, "https://evil.example")), 200)
        with self.assertRaisesRegex(rc.RelayError, "clientDataJSON.origin"):
            rc.pull(rid, secret=SECRET)

    def test_a_pull_before_dp_signs_does_not_burn_the_request(self) -> None:
        g = self.unsigned()
        rid, url = rc.post("mint", g, relay=self.relay, secret=SECRET)
        shown, cap = self.phone_open(url)
        self.assertEqual(self.phone_capture(cap, phone_sign(dict(shown, target="junk"), self.relay)), 200)
        with self.assertRaises(rc.RelayError):
            rc.pull(rid, secret=SECRET)  # only junk so far: refused, record kept
        self.assertEqual(self.phone_capture(cap, phone_sign(shown, self.relay)), 200)
        self.assertIn("webauthn", rc.pull(rid, secret=SECRET))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(url, timeout=10)  # the Mac deleted it after verifying

    def test_link_is_built_on_the_mac_and_posts_are_canon_safe(self) -> None:
        rid, url = rc.post("mint", self.unsigned(), relay=self.relay, secret=SECRET)
        self.assertEqual(url, f"{self.relay}/r/{rid}")
        for bad in ({"target": "caf\u00e9"}, {"ttl_seconds": 600.0}):
            with self.subTest(bad), self.assertRaisesRegex(rc.RelayError, "refusing to post"):
                rc.post("mint", dict(self.unsigned(), **bad), relay=self.relay, secret=SECRET)

    def test_junk_capture_first_does_not_lock_out_the_real_one(self) -> None:
        g = self.unsigned()
        rid, url = rc.post("mint", g, relay=self.relay, secret=SECRET)
        shown, cap = self.phone_open(url)
        junk = phone_sign(dict(shown, target="someone-else"), self.relay)
        self.assertEqual(self.phone_capture(cap, junk), 200)
        self.assertEqual(self.phone_capture(cap, phone_sign(shown, self.relay)), 200)
        self.assertIn("webauthn", rc.pull(rid, secret=SECRET))

    def test_replayed_assertion_for_another_grant_does_not_verify(self) -> None:
        first = self.unsigned()
        rid1, url1 = rc.post("mint", first, relay=self.relay, secret=SECRET)
        shown1, cap1 = self.phone_open(url1)
        old = phone_sign(shown1, self.relay)
        rid2, url2 = rc.post("mint", self.unsigned(), relay=self.relay, secret=SECRET)
        _, cap2 = self.phone_open(url2)
        self.assertEqual(self.phone_capture(cap2, old), 200)  # the relay replays it
        with self.assertRaisesRegex(rc.RelayError, "no captured assertion verifies"):
            rc.pull(rid2, secret=SECRET)

    def test_wrong_secret_and_insecure_secret_file_refused(self) -> None:
        with self.assertRaisesRegex(rc.RelayError, "401"):
            rc.post("mint", self.unsigned(), relay=self.relay, secret="w" * 40)
        os.chmod(self.secret_file, 0o644)
        with self.assertRaisesRegex(rc.RelayError, "readable by others"):
            rc.read_secret()

    def test_register_pull_returns_record_with_fingerprint_and_refuses_two_keys(self) -> None:
        rid, url = rc.post("register", None, relay=self.relay, secret=SECRET)
        _, cap = self.phone_open(url)
        spki = _b64u(bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
                     + b"\x04" + PHONE_PUB[0].to_bytes(32, "big") + PHONE_PUB[1].to_bytes(32, "big"))
        self.assertEqual(self.phone_capture(cap, {"spki_pubkey_b64url": spki}), 200)
        rec = rc.pull(rid, secret=SECRET)
        self.assertEqual(rec["pubkey_id"], "test-relay-key")  # the configured relay id
        self.assertEqual(rec["x"], f"{PHONE_PUB[0]:x}")
        self.assertEqual(rec["_fingerprint"], hashlib.sha256(base64.urlsafe_b64decode(spki + "==")).hexdigest())
        rid2, url2 = rc.post("register", None, relay=self.relay, secret=SECRET)
        _, cap2 = self.phone_open(url2)
        self.phone_capture(cap2, {"spki_pubkey_b64url": spki})
        self.phone_capture(cap2, {"spki_pubkey_b64url": spki})
        with self.assertRaisesRegex(rc.RelayError, "possible interference"):
            rc.pull(rid2, secret=SECRET)


WRANGLER = ROOT / "relay" / "node_modules" / ".bin" / "wrangler"


@unittest.skipIf(NODE is None or not WRANGLER.exists(),
                 "relay/node_modules not installed (cd relay && npm ci)")
class WranglerDevEndToEnd(RelayEndToEnd):
    """The same end-to-end tests with the Worker booted in workerd by the pinned
    `wrangler dev --local`: the real Workers runtime, a local KV, no account and
    no network. (It caught a bug the node harness could not: workerd refuses
    non-function named exports on the main module.)"""

    def _start(self) -> str:
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "WRANGLER_SEND_METRICS": "false", "CI": "1"}
        self.proc = subprocess.Popen(
            [str(WRANGLER), "dev", "--local", "--ip", "127.0.0.1", "--port", str(port),
             "--persist-to", str(Path(self.tmp.name) / "wrangler-state"),
             "--var", f"RELAY_SECRET:{SECRET}"],
            cwd=str(ROOT / "relay"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        self.addCleanup(self._stop_wrangler)
        seen = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line and self.proc.poll() is not None:
                break
            seen.append(line)
            if "Ready on" in line:
                return f"http://127.0.0.1:{port}"
        self.fail("wrangler dev did not become ready:\n" + "".join(seen[-30:]))

    def _stop_wrangler(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()

    def _stop(self) -> None:  # the base class registers this for dev_server only
        pass


class TamperingRelay(RelayEndToEnd):
    """The relay shows the phone a DIFFERENT grant than the Mac posted."""

    tamper = True

    def test_mac_post_phone_sign_mac_pull_verifies_offline(self) -> None:
        g = self.unsigned()
        rid, url = rc.post("mint", g, relay=self.relay, secret=SECRET)
        shown, cap = self.phone_open(url)
        self.assertEqual(shown["target"], "tampered-by-relay")
        self.assertEqual(self.phone_capture(cap, phone_sign(shown, self.relay)), 200)
        with self.assertRaisesRegex(rc.RelayError, "does not bind this grant|no captured assertion verifies"):
            rc.pull(rid, secret=SECRET)

    test_junk_capture_first_does_not_lock_out_the_real_one = None
    test_a_pull_before_dp_signs_does_not_burn_the_request = None
    test_an_assertion_from_another_origin_is_refused = None
    test_link_is_built_on_the_mac_and_posts_are_canon_safe = None
    test_replayed_assertion_for_another_grant_does_not_verify = None
    test_wrong_secret_and_insecure_secret_file_refused = None
    test_register_pull_returns_record_with_fingerprint_and_refuses_two_keys = None


if __name__ == "__main__":
    unittest.main()
