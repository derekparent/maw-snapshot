"""Window-only capture harness — the phone-mint v2 (Option A) BUILD GATE tool.

Serves ONE page on http://localhost that proves, with a REAL passkey assertion,
that Option A's core bet holds (design §11 build-gate): the WebAuthn `challenge`
can be bound to the digest of the DISPLAYED grant (§9.5), the assertion carries a
real user-verification (UV flag), and it verifies with the stdlib alone.

The page:
  1. displays the full sample grant JSON,
  2. derives canonical_bytes ON-DEVICE (JS) matching guards.human_grant.canonical_bytes,
  3. computes challenge = base64url(sha256(canonical_bytes))  [§9.5 — never Mac-supplied],
  4. registers an iCloud-synced passkey (create), then asserts (get) with
     userVerification:'required',
  5. POSTs the assertion back; it is stdlib-verified by phone_mint.webauthn_verify.

This is a GATE tool and the SEED of lane 3's mint page — it is NOT wired into any
seat command or hook, runs only when invoked, and binds to 127.0.0.1 (N5: no
always-on service). http://localhost is a WebAuthn secure context, so no TLS cert
is needed for the on-Mac capture. The lane-3 product page must instead be
reachable phone-ABSENT (Tailscale), which requires HTTPS — out of scope here.

Run:  PYTHONPATH=<plugin-root> python3 -m phone_mint.capture_gate [port]
Then open the printed URL, click the button, and do Touch ID / iPhone twice.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OUT = Path.cwd() / "capture.json"

# A representative merge.approval grant. ASCII values only: canonical_bytes uses
# json.dumps (ensure_ascii default) while the phone's canon() is JS JSON.stringify
# (raw UTF-8), so the two agree only on ASCII. Fixed-format fields are ASCII by
# construction (hashes, enum, ISO ts, int, hex nonce); the free-text session
# fields of session.arm / scope.amend are ENFORCED ASCII by
# human_grant.session_shape_error (refit R3a), not assumed. `target` is nested to exercise recursive
# key-sort parity between the JS deriver and Python canonical_bytes.
SAMPLE_GRANT = {
    "grant_id": "g-2026-07-20-phonemint-capture-0001",
    "instruction_sha256": "3b1f2c9a8d7e6f5041a2b3c4d5e6f70819202122232425262728292a2b2c2d2e",
    "scope_class": "merge.approval",
    "target": {"repo": "maw", "pr": 999, "head_sha": "deadbeefcafebabe0123456789abcdef01234567"},
    "issued_at": "2026-07-20T05:00:00Z",
    "ttl_seconds": 900,
    "nonce": "a1b2c3d4e5f60718",
    "pubkey_id": "phone-passkey-example",
    "sig": "PLACEHOLDER-not-covered-by-canonical_bytes",
}

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>phone-mint v2 — capture gate</title>
<style>
body{font:15px -apple-system,system-ui,sans-serif;max-width:680px;margin:24px auto;padding:0 16px;color:#111}
pre{background:#f4f4f4;padding:12px;border-radius:8px;overflow:auto;font-size:12px}
button{font-size:17px;padding:12px 20px;border-radius:10px;border:0;background:#0a63c9;color:#fff;margin:8px 0}
.ok{color:#0a7d28}.err{color:#c0261a;white-space:pre-wrap}
#log{font-size:13px}h2{font-size:16px;margin-top:24px}
.chal{font-family:ui-monospace,monospace;font-size:12px;word-break:break-all;background:#fffbe6;padding:8px;border-radius:6px}
</style></head><body>
<h1>phone-mint v2 — WebAuthn capture gate</h1>
<p>This is the grant you are authorizing. The challenge below is computed
<b>on this device</b> from exactly these bytes (§9.5) — the Mac never supplies it.</p>
<pre id="grant"></pre>
<h2>Derived challenge = base64url(sha256(canonical_bytes))</h2>
<div class="chal" id="challenge">computing…</div>
<button id="go">Register passkey + authorize (Touch ID / iPhone)</button>
<div id="log"></div>
<script>
const GRANT = __GRANT_JSON__;
const log = (m, cls) => { const d=document.createElement('div'); if(cls)d.className=cls; d.textContent=m; document.getElementById('log').appendChild(d); };
function canon(v){
  if(v===null) return 'null';
  if(Array.isArray(v)) return '['+v.map(canon).join(',')+']';
  if(typeof v==='object') return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}';
  return JSON.stringify(v);
}
function b64url(bytes){ let s=btoa(String.fromCharCode(...new Uint8Array(bytes))); return s.replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''); }
async function sha256(buf){ return new Uint8Array(await crypto.subtle.digest('SHA-256', buf)); }
document.getElementById('grant').textContent = JSON.stringify(GRANT, null, 2);
let CHALLENGE_BYTES=null, CANON_STR=null;
(async()=>{
  const unsigned = {...GRANT}; delete unsigned.sig;
  CANON_STR = canon(unsigned);
  CHALLENGE_BYTES = await sha256(new TextEncoder().encode(CANON_STR));
  document.getElementById('challenge').textContent = b64url(CHALLENGE_BYTES);
})();
document.getElementById('go').onclick = async () => {
  try {
    if(!window.PublicKeyCredential){ log('WebAuthn unavailable in this browser', 'err'); return; }
    const userId = crypto.getRandomValues(new Uint8Array(16));
    const cred = await navigator.credentials.create({ publicKey: {
      rp: { name: 'maw phone-mint gate' },
      user: { id: userId, name: 'maw-operator', displayName: 'operator phone-mint' },
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      pubKeyCredParams: [{ type:'public-key', alg:-7 }],
      authenticatorSelection: { userVerification:'required', residentKey:'preferred' },
      timeout: 120000,
    }});
    const spki = cred.response.getPublicKey();
    if(!spki){ log('getPublicKey() returned null — need a P-256/ES256 key', 'err'); return; }
    log('registered passkey ✓ (touch #1 done)', 'ok');
    const assertion = await navigator.credentials.get({ publicKey: {
      challenge: CHALLENGE_BYTES,
      allowCredentials: [{ type:'public-key', id: new Uint8Array(cred.rawId) }],
      userVerification: 'required',
      timeout: 120000,
    }});
    log('authorized ✓ (touch #2 done) — posting capture', 'ok');
    const r = assertion.response;
    const payload = {
      grant: GRANT,
      canonical_str_js: CANON_STR,
      challenge_b64url_js: b64url(CHALLENGE_BYTES),
      spki_pubkey_b64url: b64url(spki),
      clientDataJSON_b64url: b64url(r.clientDataJSON),
      authenticatorData_b64url: b64url(r.authenticatorData),
      signature_b64url: b64url(r.signature),
    };
    const resp = await fetch('/capture', { method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(payload) });
    log(resp.ok ? 'capture written — you can close this tab' : 'POST failed', resp.ok ? 'ok':'err');
  } catch(e) { log('WebAuthn error: '+ (e && e.message ? e.message : String(e)), 'err'); }
};
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the gate quiet
        pass

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = PAGE.replace("__GRANT_JSON__", json.dumps(SAMPLE_GRANT)).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/capture":
            self.send_error(404)
            return
        n = int(self.headers.get("content-length", "0"))
        data = json.loads(self.rfile.read(n).decode("utf-8"))
        OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\n[capture] wrote {OUT}  (grant_id={data['grant']['grant_id']})", flush=True)
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def main(argv: "list[str] | None" = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else 8765
    srv = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"capture gate on http://localhost:{port}  (Ctrl-C to stop)", flush=True)
    print("  → open it, click the button, do Touch ID / iPhone twice", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
