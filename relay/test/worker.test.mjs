// node --test relay/test — the Worker's own tests (also run from
// tests/phone_mint/test_relay.py so the canonical Python runner counts them).
import { test } from "node:test";
import assert from "node:assert/strict";
import { handle, renderPage, MAX_BODY_BYTES, MAX_CAPTURES } from "../handler.mjs";

const SECRET = "s".repeat(40);
const env = { RELAY_SECRET: SECRET };
const ORIGIN = "https://relay.example";

function memStore() {
  const m = new Map();
  return {
    m,
    get: async (k) => (m.has(k) ? m.get(k) : null),
    put: async (k, v) => { m.set(k, v); },
    delete: async (k) => { m.delete(k); },
  };
}

function req(method, path, { body, auth = SECRET } = {}) {
  const headers = { "content-type": "application/json" };
  if (auth) headers.authorization = `Bearer ${auth}`;
  return new Request(`${ORIGIN}${path}`, {
    method, headers, body: body === undefined ? undefined : (typeof body === "string" ? body : JSON.stringify(body)),
  });
}

const grant = { grant_id: "g-1", ttl_seconds: 600, scope_class: "goal.stage", target: "t" };

async function post(store, g = grant) {
  const r = await handle(req("POST", "/api/request", { body: { kind: "mint", grant: g } }), env, store);
  return [r.status, r.status === 200 ? await r.json() : await r.text()];
}

test("request needs the bearer secret, and an unconfigured secret is closed", async () => {
  const s = memStore();
  assert.equal((await handle(req("POST", "/api/request", { body: { kind: "mint", grant }, auth: "wrong".repeat(8) }), env, s)).status, 401);
  assert.equal((await handle(req("POST", "/api/request", { body: { kind: "mint", grant } }), { RELAY_SECRET: "" }, s)).status, 401);
  assert.equal((await handle(req("POST", "/api/request", { body: { kind: "mint", grant } }), { RELAY_SECRET: "short" }, s)).status, 401);
  assert.equal(s.m.size, 0);
});

test("a signed, long-lived or malformed grant is refused", async () => {
  const s = memStore();
  for (const g of [{ ...grant, sig: "x" }, { ...grant, webauthn: {} }, { ...grant, ttl_seconds: 901 },
                   { ...grant, ttl_seconds: 0 }, { ...grant, grant_id: "bad id" }, [grant], null]) {
    const [status] = await post(s, g);
    assert.equal(status, 400, JSON.stringify(g));
  }
  assert.equal(s.m.size, 0);
});

test("the page embeds the posted grant and a per-request capture path", async () => {
  const s = memStore();
  const [, { id, url }] = await post(s);
  assert.match(id, /^[0-9a-f]{32}$/);
  assert.equal(url, `${ORIGIN}/r/${id}`);
  const page = await handle(req("GET", `/r/${id}`, { auth: null }), env, s);
  assert.equal(page.status, 200);
  assert.equal(page.headers.get("cache-control"), "no-store");
  const html = await page.text();
  assert.ok(html.includes(`CAPTURE = "/r/${id}/capture"`));
  assert.ok(html.includes('"grant_id":"g-1"'));
});

test("page embedding cannot break out of the script", () => {
  const html = renderPage("mint", { ...grant, target: "</script><script>alert(1)</script>" }, "/r/x/capture");
  assert.ok(!html.includes("</script><script>alert(1)"));
  assert.ok(html.includes("\\u003c/script>"));
});

test("replacement patterns in grant fields are inert ($' $& $`)", () => {
  const g = { ...grant, target: "a$'b$&c$`d$$e" };
  const html = renderPage("mint", g, "/r/x/capture");
  assert.equal(html.split("</script>").length, 2);
  const m = html.match(/GRANT = (.*?), CAPTURE = /s);
  assert.deepEqual(JSON.parse(m[1]), g);
});

test("capture: strict fields, once only; result: bearer, 202 until captured, pull once", async () => {
  const s = memStore();
  const [, { id }] = await post(s);
  const pending = await handle(req("GET", `/api/result/${id}`), env, s);
  assert.equal(pending.status, 202);
  assert.equal((await handle(req("GET", `/api/result/${id}`, { auth: null }), env, s)).status, 401);
  const good = { client_data_json_b64: "YQ", authenticator_data_b64: "Yg", signature_b64: "Yw" };
  for (const bad of [{ ...good, extra: "x" }, { ...good, signature_b64: "not b64url!" }, { client_data_json_b64: "YQ" }]) {
    assert.equal((await handle(req("POST", `/r/${id}/capture`, { body: bad, auth: null }), env, s)).status, 400);
  }
  // a junk capture first must not lock out the real one
  const junk = { client_data_json_b64: "AA", authenticator_data_b64: "AA", signature_b64: "AA" };
  assert.equal((await handle(req("POST", `/r/${id}/capture`, { body: junk, auth: null }), env, s)).status, 200);
  assert.equal((await handle(req("POST", `/r/${id}/capture`, { body: good, auth: null }), env, s)).status, 200);
  for (let i = 2; i < MAX_CAPTURES; i++) {
    assert.equal((await handle(req("POST", `/r/${id}/capture`, { body: junk, auth: null }), env, s)).status, 200);
  }
  assert.equal((await handle(req("POST", `/r/${id}/capture`, { body: good, auth: null }), env, s)).status, 409);
  const res = await handle(req("GET", `/api/result/${id}`), env, s);
  assert.equal(res.status, 200);
  const caps = (await res.json()).captures;
  assert.equal(caps.length, MAX_CAPTURES);
  assert.deepEqual(caps[1], good);
  // not burned by the pull: the Mac deletes only after it verified one
  assert.equal((await handle(req("GET", `/api/result/${id}`), env, s)).status, 200);
  assert.equal((await handle(req("DELETE", `/api/result/${id}`, { auth: null }), env, s)).status, 401);
  assert.equal((await handle(req("DELETE", `/api/result/${id}`), env, s)).status, 200);
  assert.equal((await handle(req("GET", `/api/result/${id}`), env, s)).status, 404);
  assert.equal((await handle(req("GET", `/r/${id}`, { auth: null }), env, s)).status, 404);
});

test("the body cap counts BYTES, not characters", async () => {
  const s = memStore();
  const multibyte = JSON.stringify({ kind: "mint", grant, pad: "\u00e9".repeat(MAX_BODY_BYTES / 2 + 10) });
  assert.ok(multibyte.length < MAX_BODY_BYTES);
  assert.equal((await handle(req("POST", "/api/request", { body: multibyte }), env, s)).status, 400);
});

test("oversized and non-JSON bodies are refused before parsing", async () => {
  const s = memStore();
  const big = JSON.stringify({ kind: "mint", grant, pad: "x".repeat(MAX_BODY_BYTES) });
  assert.equal((await handle(req("POST", "/api/request", { body: big }), env, s)).status, 400);
  assert.equal((await handle(req("POST", "/api/request", { body: "not json" }), env, s)).status, 400);
});

test("unknown ids and routes are 404", async () => {
  const s = memStore();
  assert.equal((await handle(req("GET", `/r/${"0".repeat(32)}`, { auth: null }), env, s)).status, 404);
  assert.equal((await handle(req("GET", "/r/../api/request", { auth: null }), env, s)).status, 404);
  assert.equal((await handle(req("GET", "/", { auth: null }), env, s)).status, 404);
});

test("register: a request, the SPKI capture, then pull once", async () => {
  const s = memStore();
  const r = await handle(req("POST", "/api/request", { body: { kind: "register" } }), env, s);
  const { id } = await r.json();
  const html = await (await handle(req("GET", `/r/${id}`, { auth: null }), env, s)).text();
  assert.ok(html.includes('KIND = "register"'));
  assert.equal((await handle(req("POST", `/r/${id}/capture`, { body: { spki_pubkey_b64url: "AAAA" }, auth: null }), env, s)).status, 200);
  const res = await (await handle(req("GET", `/api/result/${id}`), env, s)).json();
  assert.deepEqual(res, { kind: "register", captures: [{ spki_pubkey_b64url: "AAAA" }] });
});

test("the page fingerprint is served behind the secret and on every page", async () => {
  const { pageSha256 } = await import("../handler.mjs");
  const s = memStore();
  const sha = await pageSha256();
  assert.match(sha, /^[0-9a-f]{64}$/);
  assert.equal((await handle(req("GET", "/api/page-sha", { auth: null }), env, s)).status, 401);
  const r = await handle(req("GET", "/api/page-sha"), env, s);
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { page_sha256: sha });
  const [, { id }] = await post(s);
  const page = await handle(req("GET", `/r/${id}`, { auth: null }), env, s);
  assert.equal(page.headers.get("x-page-sha256"), sha);
});
