"""phone-mint v2 lane 3b — the window-only mint page + server (Option A, §5/§9).

Serves the passkey ceremony to the operator's phone over his tailnet (`tailscale serve`
fronts this with a ts.net cert; this process binds 127.0.0.1 only). Two flows:

  register  — ONE-TIME: the operator's phone creates a passkey; the page returns its public
              key (SPKI), which becomes the committed `<phone_pubkey_id>.pub`
              (a Layer-1 trust-root commit, N6 — the operator trusted-path, never an
              autonomous merge).
  mint      — REPEATED: given an unsigned grant (mint_session.assemble_unsigned_grant),
              the page displays the FULL grant (N1), derives the challenge ON the
              phone from the displayed bytes (§9.5), and Face-ID-asserts. The
              assertion is attached via mint_session.attach_assertion and the
              completed grant is written for the CoS to relay unchanged.

Disciplines the review named:
  - N5 no always-on service: the server runs a BOUNDED window and stops after the
    first successful capture (or the timeout). No daemon.
  - N4 / body cap: POST bodies are capped (an assertion is a few hundred bytes);
    an over-limit or non-JSON body is refused before parsing.
  - zero trust from the transport: every POSTed field is validated; the mint
    self-verifies the assertion against the COMMITTED key before writing, so a
    forged or wrong-grant assertion never becomes a relayable grant.
  - phone-ABSENT is the target (the operator 2026-07-20): the page is reachable over the
    tailnet with the Mac merely serving; nothing here assumes localhost in prod.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from guards import human_grant as hg
from phone_mint import mint_session as ms
from phone_mint import webauthn_verify as wv

MAX_BODY_BYTES = 64 * 1024  # an assertion is ~1KB; anything larger is refused (N4)


# --- registration: SPKI public key -> committed .pub record ------------------

def build_pubkey_record(spki_b64url: str, *, pubkey_id: str, not_before: str,
                        not_after: str) -> dict:
    """Turn a WebAuthn getPublicKey() SPKI (base64url) into the JSON a committed
    `<pubkey_id>.pub` holds: {pubkey_id, x, y (hex), not_before, not_after}.
    Raises ValueError if the SPKI is not a P-256 key."""
    if not isinstance(spki_b64url, str) or not spki_b64url:
        raise ValueError("spki must be a non-empty base64url string")
    point = wv.spki_p256_point(wv.b64url_decode(spki_b64url))
    if point is None:
        raise ValueError("SPKI is not a P-256 (ES256) public key")
    # M1: reject an off-curve point AT CAPTURE, so a garbage registration fails in
    # The operator's window rather than becoming a dud .pub that only fails at the e2e mint.
    from guards import p256
    if not p256._on_curve(point):
        raise ValueError("SPKI public key point is not on the P-256 curve")
    x, y = point
    return {
        "pubkey_id": pubkey_id,
        "x": f"{x:x}",
        "y": f"{y:x}",
        "not_before": not_before,
        "not_after": not_after,
    }


# --- the served pages --------------------------------------------------------

def _js_embed(obj) -> str:
    """JSON for embedding inside an inline <script>. `json.dumps` does NOT escape
    '<', so a grant string field like '</script>...' would break out of the script
    context and run attacker JS — and a grant-author who controls the page can
    display grant X while deriving the challenge from grant Y, i.e. render-X-sign-Y
    through the front door, defeating the very §9.5 bind this page enforces.
    Escaping '<' to its \\u003c form is JSON-identical (same parsed value, so the
    on-device canon() is unchanged) but the HTML parser never sees a tag. (U+2028/
    U+2029 are already \\u-escaped by ensure_ascii, json's default.) Compact
    separators: the relay's jsEmbed (relay/handler.mjs) produces the SAME bytes,
    so the two hosts render byte-identical pages (a test pins it)."""
    return json.dumps(obj, separators=(",", ":")).replace("<", "\\u003c")


CAPTURE_PATHS = {"register": "/register-capture", "mint": "/mint-capture"}


def _page(kind: str, grant: "dict | None", capture: "str | None" = None) -> bytes:
    """Render the register or mint page. For mint, the grant is embedded so the
    phone derives the challenge from exactly what it displays (§9.5). Embedded
    values are escaped against script-context breakout (see _js_embed).

    `capture` is the path the page POSTs its result to. This window server uses
    the fixed CAPTURE_PATHS; the hosted relay (relay/worker.mjs, refit R3b) fills
    the SAME template with a per-request path — one page, two hosts."""
    grant_json = _js_embed(grant) if grant is not None else "null"
    title = "phone-mint v2 — register passkey" if kind == "register" else "phone-mint v2 — authorize grant"
    # GRANT is substituted LAST, so a grant field that happens to contain another
    # placeholder token stays inert (the relay does the same).
    return (_PAGE_TMPL
            .replace("__KIND__", _js_embed(kind))
            .replace("__CAPTURE__", _js_embed(capture or CAPTURE_PATHS[kind]))
            .replace("__TITLE__", title)
            .replace("__GRANT_JSON__", grant_json)).encode("utf-8")


_PAGE_TMPL = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--mute:#555;--card:#f4f5f7;--line:#dde0e4;--code:#fffbe6;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mute:#aaa;--card:#1e2126;--line:#33373d;--code:#3a3320;--warn:#f0c060}}
body{font:16px -apple-system,system-ui,sans-serif;max-width:680px;margin:20px auto;padding:0 16px;color:var(--fg);background:var(--bg)}
pre{background:var(--card);padding:12px;border-radius:8px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}
button{font-size:18px;padding:14px 22px;border-radius:12px;border:0;background:#0a63c9;color:#fff;margin:10px 0;width:100%}
.ok{color:#0a7d28}.err{color:#c0261a;white-space:pre-wrap}
.chal{font-family:ui-monospace,monospace;font-size:12px;word-break:break-all;background:var(--code);padding:8px;border-radius:6px}
.card{background:var(--card);border-radius:12px;padding:14px 16px;margin:12px 0}
.what{font-size:22px;font-weight:600;line-height:1.3;margin-bottom:6px;overflow-wrap:anywhere}
.row{border-top:1px solid var(--line);padding:8px 0}.k{font-size:13px;color:var(--mute)}
.v{font-size:17px;white-space:pre-wrap;overflow-wrap:anywhere}.mono{font-family:ui-monospace,monospace;font-size:14px}
.warn{color:var(--warn);font-weight:600}.note{color:var(--mute);font-size:14px}
.ctitle{font-size:18px;font-weight:600;overflow-wrap:anywhere}
.code{font-family:ui-monospace,monospace;font-size:24px;letter-spacing:1px;text-align:center;background:var(--code);padding:10px;border-radius:10px;overflow-wrap:anywhere}
details{margin-top:16px}summary{color:var(--mute);font-size:15px}
h1{font-size:20px}h2{font-size:15px;color:var(--mute)}
</style></head><body>
<h1>__TITLE__</h1>
<div id="body"></div><div id="log"></div>
<script>
const KIND = __KIND__, GRANT = __GRANT_JSON__, CAPTURE = __CAPTURE__;
const log=(m,c)=>{const d=document.createElement('div');if(c)d.className=c;d.textContent=m;document.getElementById('log').appendChild(d);};
function canon(v){if(v===null)return 'null';if(Array.isArray(v))return '['+v.map(canon).join(',')+']';if(typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canon(v[k])).join(',')+'}';return JSON.stringify(v);}
function b64url(b){let s=btoa(String.fromCharCode(...new Uint8Array(b)));return s.replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
async function sha256(buf){return new Uint8Array(await crypto.subtle.digest('SHA-256',buf));}
async function post(path,payload){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});return r.ok;}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
// <summarize> The plain-language view. PURE: it reads only its argument, and the
// page passes the SAME object the challenge is computed over, so the summary
// cannot say something the signed bytes do not. Anything it does not fully
// describe (an unknown class, a missing or unexpected field) returns what:null
// and the page shows the raw grant instead of a guessed label.
function summarize(g){
  const KEYS=['grant_id','instruction_sha256','scope_class','target','issued_at','ttl_seconds','nonce','pubkey_id','sig','webauthn','session_token_ref','merge','session','batch'];
  const BATCH='session.arm.batch', LINE=['repo','seat','session_id','scope','goal'];
  const SESSION={'session.arm':['repo','seat','session_id','scope'],'scope.amend':['repo','seat','session_id','from_scope','scope']};
  const PLAIN={'seat.standdown':'Stand down the seat ','goal.stage':'Stage the next goal for ','prompt.relay':'Relay a prompt to ','unblock.instruct':'Send an unblock instruction to '};
  const isObj=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
  const isStr=v=>typeof v==='string'&&v!=='';
  const ascii=v=>isStr(v)&&/^[\x20-\x7e]+$/.test(v)&&v===v.trim()&&!/  /.test(v);
  if(!isObj(g)||!isStr(g.scope_class)||!isStr(g.target))return {what:null,rows:[]};
  const c=g.scope_class;
  if(Object.keys(g).some(k=>!KEYS.includes(k))||g.session_token_ref!=null)return {what:null,rows:[]};
  // merge only on merge.approval, session only on the session classes (else null).
  if((g.merge!=null)!==(c==='merge.approval')||(g.session!=null)!==!!SESSION[c])return {what:null,rows:[]};
  // batch only on the batch class, where it must be present (absent, not null, elsewhere).
  if(('batch' in g)!==(c===BATCH))return {what:null,rows:[]};
  const rows=[], add=(k,v,mono)=>rows.push([k,String(v),!!mono]);
  let what, cards;
  if(c===BATCH){
    // One card per line, every field whole. A line it cannot fully describe
    // fails the WHOLE page to raw: a partial manifest view is a blind grant.
    const b=g.batch;
    if(!Array.isArray(b)||b.length<1||b.length>8)return {what:null,rows:[]};
    cards=[];
    for(const s of b){
      if(!isObj(s)||!LINE.every(k=>k in s)||Object.keys(s).some(k=>!LINE.includes(k)&&k!=='prs'))return {what:null,rows:[]};
      // The verifier's own string rule (single-spaced printable ASCII) and caps:
      // a newline in a bullet could otherwise draw a fake row on this page.
      if(!['repo','seat','session_id','scope'].every(k=>ascii(s[k])))return {what:null,rows:[]};
      if(s.scope.length>200||s.seat!=='advisor')return {what:null,rows:[]};
      if(!Array.isArray(s.goal)||s.goal.length<1||s.goal.length>3||!s.goal.every(x=>ascii(x)&&x.length<=120))return {what:null,rows:[]};
      if('prs' in s&&!(ascii(s.prs)&&/^#[1-9][0-9]*(,#[1-9][0-9]*)*$/.test(s.prs)))return {what:null,rows:[]};
      const cr=[], cadd=(k,v,mono)=>cr.push([k,String(v),!!mono]);
      cadd('Goal',s.goal.map(x=>'\u2022 '+x).join('\n'));
      cadd('What it may merge',s.scope);
      if('prs' in s)cadd('Only these PRs',s.prs);
      cadd('Seat',s.seat);
      cadd('Session',s.session_id,true);
      cards.push({title:s.repo,rows:cr});
    }
    // repo and session_id unique across lines, as the verifier requires.
    for(const k of ['repo','session_id'])if(new Set(b.map(x=>x[k])).size!==b.length)return {what:null,rows:[]};
    what='Arm '+b.length+' maw advisor'+(b.length===1?'':'s')+' to merge for up to 6 hours';
  }else if(SESSION[c]){
    const s=g.session, need=SESSION[c];
    if(!isObj(s)||!need.every(k=>isStr(s[k]))||Object.keys(s).some(k=>!need.includes(k)&&k!=='prs'))return {what:null,rows:[]};
    if(c==='session.arm'){what='Let the maw '+s.seat+' merge in '+s.repo+' for up to 6 hours';add('What it may merge',s.scope);}
    else{what='Change what the maw '+s.seat+' may merge in '+s.repo+' (the expiry does not change)';add('From',s.from_scope);add('To',s.scope);}
    if('prs' in s){if(!isStr(s.prs))return {what:null,rows:[]};add('Only these PRs',s.prs);}
    add('Session',s.session_id,true);
  }else if(c==='merge.approval'){
    const m=g.merge;
    if(!isObj(m)||!isStr(m.repo)||!Number.isInteger(m.pr)||!isStr(m.head_sha)||Object.keys(m).length!==3)return {what:null,rows:[]};
    what='Merge PR #'+m.pr+' in '+m.repo;
    add('Exact commit',m.head_sha,true);
  }else if(PLAIN[c]){
    what=PLAIN[c]+g.target;
    add('Instruction','Not shown here: only its hash is signed. Approve only if you asked for this.');
  }else return {what:null,rows:[]};
  add('Requested for',g.target);
  const t=Date.parse(g.issued_at), ttl=g.ttl_seconds;
  if(!isNaN(t)&&Number.isInteger(ttl)){
    const hm=d=>new Date(d).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
    add('This link expires',hm(t+ttl*1000)+' (valid '+Math.round(ttl/60)+' min from '+hm(t)+')');
  }
  return cards?{what,rows,cards}:{what,rows};
}
// </summarize>

const body=document.getElementById('body');
if(KIND==='register'){
  body.innerHTML='<p>Create your phone-mint passkey. This is a ONE-TIME step; its public key becomes the committed trust root.</p><button id="go">Register passkey (Face ID)</button>';
  document.getElementById('go').onclick=async()=>{try{
    const cred=await navigator.credentials.create({publicKey:{
      rp:{name:'maw phone-mint'},
      user:{id:crypto.getRandomValues(new Uint8Array(16)),name:'maw-operator',displayName:'operator phone-mint'},
      challenge:crypto.getRandomValues(new Uint8Array(32)),
      pubKeyCredParams:[{type:'public-key',alg:-7}],
      authenticatorSelection:{userVerification:'required',residentKey:'required'},
      timeout:120000}});
    const spki=cred.response.getPublicKey();
    if(!spki){log('getPublicKey() null — need an ES256 key','err');return;}
    const fp=Array.from(await sha256(spki),x=>x.toString(16).padStart(2,'0')).join('');
    const f=document.createElement('div');f.className='chal';f.textContent='key fingerprint (SHA-256 of the public key): '+fp;document.getElementById('log').appendChild(f);
    const ok=await post(CAPTURE,{spki_pubkey_b64url:b64url(spki)});
    log(ok?'passkey registered — public key captured. Before committing it, check that the Mac prints this SAME fingerprint.':'capture FAILED — ABORT this enrollment and start a new one; do not commit any key from it.',ok?'ok':'err');
  }catch(e){log('register error: '+(e&&e.message||e),'err');}};
}else{
  // textContent only below: every value is operator/agent text, never markup.
  const S=summarize(GRANT);
  const card=el('div','card');
  if(S.what===null){
    card.appendChild(el('div','what warn','This page cannot describe this request.'));
    card.appendChild(el('div','v','Read the technical details below before you approve.'));
  }else{
    card.appendChild(el('div','what',S.what));
    for(const [k,v,mono] of S.rows){const r=el('div','row');r.appendChild(el('div','k',k));r.appendChild(el('div',mono?'v mono':'v',v));card.appendChild(r);}
  }
  body.appendChild(card);
  for(const C of (S.cards||[])){
    const cc=el('div','card');cc.appendChild(el('div','ctitle',C.title));
    for(const [k,v,mono] of C.rows){const r=el('div','row');r.appendChild(el('div','k',k));r.appendChild(el('div',mono?'v mono':'v',v));cc.appendChild(r);}
    body.appendChild(cc);
  }
  body.appendChild(el('div','k','Check code: the Mac printed the same first 12 characters'));
  const code=el('div','code','computing…');body.appendChild(code);
  const btn=el('button','','Authorize (Face ID)');body.appendChild(btn);
  const det=el('details');if(S.what===null)det.open=true;
  det.appendChild(el('summary','','Technical details'));
  det.appendChild(el('p','note','The challenge is computed on THIS device from these exact bytes (§9.5):'));
  det.appendChild(el('pre','',JSON.stringify(GRANT,null,2)));
  const ch=el('div','chal','computing…');det.appendChild(ch);
  body.appendChild(det);
  let CHAL=null;
  (async()=>{const u={...GRANT};delete u.sig;delete u.webauthn;CHAL=await sha256(new TextEncoder().encode(canon(u)));const c=b64url(CHAL);ch.textContent=c;code.textContent=c.slice(0,12);})();
  btn.onclick=async()=>{try{
    const a=await navigator.credentials.get({publicKey:{challenge:CHAL,userVerification:'required',timeout:120000}});
    const r=a.response;
    const ok=await post(CAPTURE,{
      client_data_json_b64:b64url(r.clientDataJSON),
      authenticator_data_b64:b64url(r.authenticatorData),
      signature_b64:b64url(r.signature)});
    log(ok?'authorized — grant signed and captured. You can close this tab.':'capture rejected (see the seat log)',ok?'ok':'err');
  }catch(e){log('authorize error: '+(e&&e.message||e),'err');}};
}
</script></body></html>"""


# --- the window-only server --------------------------------------------------

class MintServer:
    """A bounded-window HTTP server for one ceremony. Stops after the first
    successful capture or when the window elapses (N5 — no daemon)."""

    def __init__(self, *, kind: str, grant: "dict | None" = None,
                 pubkey_point: "tuple[int, int] | None" = None,
                 window_seconds: int = 300, port: int = 8799):
        if kind not in ("register", "mint"):
            raise ValueError("kind must be 'register' or 'mint'")
        if kind == "mint" and (grant is None or pubkey_point is None):
            raise ValueError("mint requires grant + pubkey_point")
        # Resolved up front: an unconfigured key id fails before the window opens.
        self.register_pubkey_id = ms.phone_pubkey_id() if kind == "register" else None
        self.kind = kind
        self.grant = grant
        self.pubkey_point = pubkey_point
        self.window_seconds = window_seconds
        self.port = port
        self.result: "dict | None" = None      # the .pub record or the completed grant
        self.error: "str | None" = None
        self._done = threading.Event()
        # Set once the socket is bound; self.port is then the real port (the OS
        # picks one when port=0, which the tests use so parallel runs never
        # collide).
        self.bound = threading.Event()

    def _handle_register(self, payload: dict) -> None:
        self.result = build_pubkey_record(
            payload.get("spki_pubkey_b64url"),
            pubkey_id=self.register_pubkey_id,
            not_before=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            not_after="2099-01-01T00:00:00Z",
        )

    def _handle_mint(self, payload: dict) -> None:
        grant = dict(self.grant)  # a fresh copy per attempt; attach mutates
        self.result = ms.attach_assertion(
            grant,
            client_data_json_b64=payload.get("client_data_json_b64"),
            authenticator_data_b64=payload.get("authenticator_data_b64"),
            signature_b64=payload.get("signature_b64"),
            pubkey_point=self.pubkey_point,
        )

    def _dispatch(self, payload: dict) -> None:
        if self.kind == "register":
            self._handle_register(payload)
        else:
            self._handle_mint(payload)

    def serve(self) -> "dict | None":
        srv = _build_httpd(self)
        self.port = srv.server_address[1]
        self.bound.set()
        srv.timeout = 1
        start = time.monotonic()
        while not self._done.is_set() and time.monotonic() - start < self.window_seconds:
            srv.handle_request()
        srv.server_close()
        return self.result


def _build_httpd(server: MintServer) -> HTTPServer:
    kind, grant = server.kind, server.grant

    class Handler(BaseHTTPRequestHandler):
        # M2: bound an ESTABLISHED connection's reads, not just the accept wait —
        # a client that opens a socket and stalls mid-body would otherwise hold the
        # window open past window_seconds. BaseHTTPRequestHandler applies this to
        # the connection socket.
        timeout = 15

        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self._send(200, _page(kind, grant), "text/html; charset=utf-8")

        def do_POST(self):
            want = CAPTURE_PATHS[kind]
            if self.path != want:
                self.send_error(404)
                return
            n = int(self.headers.get("content-length", "0") or "0")
            if n <= 0 or n > MAX_BODY_BYTES:  # N4: refuse missing/oversize bodies
                self._send(413, b"bad length", "text/plain")
                return
            raw = self.rfile.read(n)
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("payload not an object")
                server._dispatch(payload)
            except (ValueError, TypeError, KeyError) as e:
                server.error = str(e)
                self._send(400, b"rejected", "text/plain")
                return
            server._done.set()
            self._send(200, b"ok", "text/plain")

    return HTTPServer(("127.0.0.1", server.port), Handler)


# --- CLI ---------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    import argparse
    args = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="phone_mint.serve",
                                description="window-only passkey mint/register server")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="one-time: capture the phone passkey public key")
    r.add_argument("--out", required=True, help="write the .pub record here")
    r.add_argument("--port", type=int, default=8799)
    r.add_argument("--window", type=int, default=300)
    m = sub.add_parser("mint", help="serve an unsigned grant for phone authorization")
    m.add_argument("--grant-file", required=True, help="unsigned grant JSON (assemble first)")
    m.add_argument("--out", required=True, help="write the completed (signed) grant here")
    m.add_argument("--port", type=int, default=8799)
    m.add_argument("--window", type=int, default=300)
    ns = p.parse_args(args)

    if ns.cmd == "register":
        srv = MintServer(kind="register", window_seconds=ns.window, port=ns.port)
        _announce(ns.port, "register")
        rec = srv.serve()
        if rec is None:
            print(f"no capture within the window ({srv.error or 'timeout'})", file=sys.stderr)
            return 1
        Path(ns.out).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        print(f"public key captured -> {ns.out} (pubkey_id={rec['pubkey_id']}). "
              f"COMMIT IT AS A LAYER-1 KEY VIA THE OPERATOR'S TRUSTED PATH.", file=sys.stderr)
        return 0

    grant = json.loads(Path(ns.grant_file).read_text(encoding="utf-8"))
    key = hg.load_key(grant["pubkey_id"])
    if key is None:
        print(f"{hg.missing_key_reason(grant['pubkey_id'])} — commit the phone key first",
              file=sys.stderr)
        return 1
    srv = MintServer(kind="mint", grant=grant, pubkey_point=key[0],
                     window_seconds=ns.window, port=ns.port)
    _announce(ns.port, "mint")
    done = srv.serve()
    if done is None:
        print(f"no valid assertion within the window ({srv.error or 'timeout'})", file=sys.stderr)
        return 1
    Path(ns.out).write_text(json.dumps(done, indent=2), encoding="utf-8")
    print(f"grant signed -> {ns.out} (relay it unchanged)", file=sys.stderr)
    return 0


def _announce(port: int, kind: str) -> None:
    print(f"phone-mint {kind}: serving 127.0.0.1:{port} for the ceremony window.\n"
          f"  front it with: tailscale serve --bg https / http://127.0.0.1:{port}\n"
          f"  then open https://<this-host>.ts.net/ on the phone.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
