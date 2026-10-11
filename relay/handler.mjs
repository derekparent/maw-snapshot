// maw phone-approval relay (refit R3b): the request handler. worker.mjs is the
// Worker entry and exports ONLY `default` (workerd treats every named export of
// the main module as an entrypoint and refuses a constant); everything else,
// and everything the tests import, lives here.
//
// WHY: the operator's approval page must work with the Mac asleep and no Tailscale on the
// phone (DECISIONS.md 2026-09-23, ruling 5 of the refit goal). The window-only
// page in maw/phone_mint/serve.py needs the Mac awake and on the tailnet.
//
// WHAT THIS RELAY IS TRUSTED WITH — READ THIS (advisor F1 on #239). It holds no
// key, and a swap of the grant in STORAGE or in TRANSIT produces nothing that
// verifies: the Mac verifies against its own copy. But the relay SERVES THE PAGE,
// and the page is what the operator reads. A MODIFIED Worker could display a benign grant
// while deriving the challenge from the real one ("render X, sign Y", Option C's
// N2 moved from the Mac to Cloudflare). The operator would sign, and the Mac would verify.
// So THIS CODE, the Cloudflare account and the deploy path are LAYER-1 TRUST
// MATERIAL on every mint, not only at enrollment. Deploy only from reviewed main.
// What limits it:
//   - the Mac checks clientDataJSON.origin and rpIdHash, so the key signs only on
//     this domain;
//   - the Mac prints the challenge prefix the page must show, which catches a
//     storage or transport swap but not a page that lies.
// It does not stop a malicious Worker on this domain.
//
// AUTH: posting a request and pulling a result need `Authorization: Bearer
// <RELAY_SECRET>` (a Worker secret, `wrangler secret put RELAY_SECRET`; the Mac
// reads its copy from ~/.secrets, never committed). The phone's page and its
// capture POST carry no secret: the page URL holds an unguessable 128-bit id.
//
// STORAGE: a KV namespace bound as RELAY_KV. Each record expires with the grant's
// own TTL. KV is eventually consistent; nothing here is a security property
// (single use is the Mac's ledger, verification is the Mac's, not the relay's).
//
// ENROLLMENT RESIDUAL, UNSPUN: the one-time passkey registration runs on THIS
// origin (a passkey is bound to its rpId), so a relay that is compromised AT
// ENROLLMENT TIME could serve a page that reports its own key. The register page
// shows the key's SHA-256 fingerprint and the Mac prints the fingerprint it
// pulled; comparing them catches a key swapped in storage or in transit, not a
// malicious page. The operator deploys this Worker himself from reviewed source and enrolls
// once; that trust in the deployed code is the residual.

import { PAGE_TMPL } from "./page.mjs";

export const MAX_BODY_BYTES = 64 * 1024;
export const MAX_TTL_SECONDS = 900;
export const REGISTER_TTL_SECONDS = 300;
const ID_RE = /^[0-9a-f]{32}$/;
const B64URL_RE = /^[A-Za-z0-9_-]{1,8192}$/;
const GRANT_ID_RE = /^[A-Za-z0-9._-]{1,80}$/;
// Anyone holding the page link can POST a capture. So the first POST must not lock
// the request: junk would then block the operator's real assertion. Keep a few; the Mac
// tries each against ITS OWN copy of the grant and accepts only one that verifies.
export const MAX_CAPTURES = 4;

const PAGE_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "no-store",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
  "x-content-type-options": "nosniff",
};

function text(status, body) {
  return new Response(body, { status, headers: { "content-type": "text/plain", "cache-control": "no-store" } });
}

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status, headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

// The served template's fingerprint: sha256 (hex) of PAGE_TMPL as UTF-8. The Mac
// compares it with its own generated page before minting a grant only the new
// page can describe (batch-arm R20). The Worker vouches for its own page: this
// catches a stale deploy, not a lying one.
let pageShaMemo = null;
export async function pageSha256() {
  if (pageShaMemo === null) {
    const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(PAGE_TMPL));
    pageShaMemo = Array.from(new Uint8Array(d), (x) => x.toString(16).padStart(2, "0")).join("");
  }
  return pageShaMemo;
}

export function jsEmbed(v) {
  // serve.py's _js_embed, byte for byte: compact JSON, non-ASCII escaped to \uXXXX
  // (Python's ensure_ascii), and '<' escaped so no value can close the script.
  return JSON.stringify(v)
    .replace(/[\u0080-\uffff]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"))
    .replace(/</g, "\\u003c");
}

export function renderPage(kind, grant, capture) {
  const title = kind === "register" ? "phone-mint v2 — register passkey" : "phone-mint v2 — authorize grant";
  // FUNCTION replacements, never string ones: a replacement STRING interprets
  // `$'`, `$&` and `` $` ``, so a grant field holding `$'` would splice the rest of
  // the template (with its `</script>`) into the page. Python's str.replace in
  // serve.py has no such patterns; this keeps the two hosts byte-identical.
  // replaceAll (a placeholder can appear more than once: __TITLE__ does), and
  // GRANT LAST, so a grant field holding a placeholder token stays inert.
  return PAGE_TMPL
    .replaceAll("__KIND__", () => jsEmbed(kind))
    .replaceAll("__CAPTURE__", () => jsEmbed(capture))
    .replaceAll("__TITLE__", () => title)
    .replaceAll("__GRANT_JSON__", () => (grant === null ? "null" : jsEmbed(grant)));
}

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

function authorized(request, env) {
  const secret = env.RELAY_SECRET;
  if (typeof secret !== "string" || secret.length < 32) return false; // unconfigured = closed
  const h = request.headers.get("authorization") || "";
  return h.startsWith("Bearer ") && safeEqual(h.slice(7), secret);
}

async function readJson(request) {
  const len = Number(request.headers.get("content-length") || "0");
  if (len > MAX_BODY_BYTES) return [null, "body too large"];
  const buf = await request.arrayBuffer();
  if (buf.byteLength === 0 || buf.byteLength > MAX_BODY_BYTES) return [null, "bad length"];
  const raw = new TextDecoder().decode(buf);
  try {
    const v = JSON.parse(raw);
    if (v === null || typeof v !== "object" || Array.isArray(v)) return [null, "not an object"];
    return [v, ""];
  } catch {
    return [null, "not JSON"];
  }
}

function validGrant(g) {
  if (g === null || typeof g !== "object" || Array.isArray(g)) return "grant is not an object";
  if ("sig" in g || "webauthn" in g) return "grant already carries a signature";
  if (typeof g.grant_id !== "string" || !GRANT_ID_RE.test(g.grant_id)) return "bad grant_id";
  const t = g.ttl_seconds;
  if (!Number.isInteger(t) || t <= 0 || t > MAX_TTL_SECONDS) return "ttl_seconds out of bounds";
  return "";
}

function newId() {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

// `store` is {get(key) -> string|null, put(key, value, {expirationTtl}), delete(key)}:
// KV in production, an in-memory map in tests and the dev server.
export async function handle(request, env, store) {
  const url = new URL(request.url);
  const path = url.pathname;
  const m = path.match(/^\/r\/([0-9a-f]{32})(\/capture)?$/);

  if (request.method === "POST" && path === "/api/request") {
    if (!authorized(request, env)) return text(401, "unauthorized");
    const [body, why] = await readJson(request);
    if (!body) return text(400, why);
    let rec;
    if (body.kind === "mint") {
      const bad = validGrant(body.grant);
      if (bad) return text(400, bad);
      rec = { kind: "mint", grant: body.grant, captures: [] };
    } else if (body.kind === "register") {
      rec = { kind: "register", grant: null, captures: [] };
    } else {
      return text(400, "kind must be mint or register");
    }
    const ttl = rec.kind === "mint" ? body.grant.ttl_seconds : REGISTER_TTL_SECONDS;
    const id = newId();
    await store.put(`req:${id}`, JSON.stringify(rec), { expirationTtl: Math.max(60, ttl) });
    return json(200, { id, url: `${url.origin}/r/${id}` });
  }

  if (m && request.method === "GET" && !m[2]) {
    const raw = await store.get(`req:${m[1]}`);
    if (!raw) return text(404, "not found or expired");
    const rec = JSON.parse(raw);
    return new Response(renderPage(rec.kind, rec.grant, `/r/${m[1]}/capture`), {
      status: 200, headers: { ...PAGE_HEADERS, "x-page-sha256": await pageSha256() },
    });
  }

  if (request.method === "GET" && path === "/api/page-sha") {
    if (!authorized(request, env)) return text(401, "unauthorized");
    return json(200, { page_sha256: await pageSha256() });
  }

  if (m && request.method === "POST" && m[2]) {
    const raw = await store.get(`req:${m[1]}`);
    if (!raw) return text(404, "not found or expired");
    const rec = JSON.parse(raw);
    if (rec.captures.length >= MAX_CAPTURES) return text(409, "capture limit reached");
    const [body, why] = await readJson(request);
    if (!body) return text(400, why);
    let capture;
    if (rec.kind === "mint") {
      const f = ["client_data_json_b64", "authenticator_data_b64", "signature_b64"];
      if (Object.keys(body).length !== 3 || !f.every((k) => typeof body[k] === "string" && B64URL_RE.test(body[k]))) {
        return text(400, "rejected");
      }
      capture = Object.fromEntries(f.map((k) => [k, body[k]]));
    } else {
      if (Object.keys(body).length !== 1 || typeof body.spki_pubkey_b64url !== "string"
          || !B64URL_RE.test(body.spki_pubkey_b64url)) {
        return text(400, "rejected");
      }
      capture = { spki_pubkey_b64url: body.spki_pubkey_b64url };
    }
    rec.captures.push(capture);
    await store.put(`req:${m[1]}`, JSON.stringify(rec), { expirationTtl: 60 * 15 });
    return text(200, "ok");
  }

  const r = path.match(/^\/api\/result\/([0-9a-f]{32})$/);
  if (r && request.method === "GET") {
    if (!authorized(request, env)) return text(401, "unauthorized");
    const raw = await store.get(`req:${r[1]}`);
    if (!raw) return text(404, "not found or expired");
    const rec = JSON.parse(raw);
    if (rec.captures.length === 0) return text(202, "not captured yet");
    // NOT deleted here: a junk capture that lands first must not burn the
    // request before the operator's real one arrives. The Mac DELETEs it after it has
    // verified a capture against its own copy (single use is the Mac's ledger).
    return json(200, { kind: rec.kind, captures: rec.captures });
  }

  if (r && request.method === "DELETE") {
    if (!authorized(request, env)) return text(401, "unauthorized");
    await store.delete(`req:${r[1]}`);
    return text(200, "deleted");
  }

  return text(404, "not found");
}

export function kvStore(kv) {
  return {
    get: (k) => kv.get(k),
    put: (k, v, opts) => kv.put(k, v, opts),
    delete: (k) => kv.delete(k),
  };
}

export { ID_RE };
