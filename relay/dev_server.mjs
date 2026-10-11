// Local stand-in for `wrangler dev` (refit R3b): serves relay/worker.mjs's handler
// over plain HTTP with an in-memory store, so the Python client and its tests can
// exercise the REAL Worker code without Cloudflare, wrangler or the network.
//
//   RELAY_SECRET=<32+ chars> node relay/dev_server.mjs [--port N] [--tamper]
//
// Prints "listening <port>" on stdout once ready. --tamper makes every served page
// show a DIFFERENT grant than the one posted (the byte-swap adversary for tests).
import http from "node:http";
import { handle } from "./handler.mjs";

const args = process.argv.slice(2);
const port = Number(args[args.indexOf("--port") + 1] || 0) || 0;
const tamper = args.includes("--tamper");

const mem = new Map();
const store = {
  async get(k) {
    const e = mem.get(k);
    if (!e) return null;
    if (e.exp && Date.now() > e.exp) { mem.delete(k); return null; }
    if (tamper && k.startsWith("req:")) {
      const rec = JSON.parse(e.v);
      if (rec.grant && rec.captures.length === 0) rec.grant = { ...rec.grant, target: "tampered-by-relay" };
      return JSON.stringify(rec);
    }
    return e.v;
  },
  async put(k, v, opts) {
    const ttl = opts && opts.expirationTtl;
    mem.set(k, { v, exp: ttl ? Date.now() + ttl * 1000 : 0 });
  },
  async delete(k) { mem.delete(k); },
};
const env = { RELAY_SECRET: process.env.RELAY_SECRET || "" };

const server = http.createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const body = Buffer.concat(chunks);
  const request = new Request(`http://127.0.0.1:${server.address().port}${req.url}`, {
    method: req.method,
    headers: req.headers,
    body: ["GET", "HEAD"].includes(req.method) ? undefined : body,
  });
  const resp = await handle(request, env, store);
  res.writeHead(resp.status, Object.fromEntries(resp.headers));
  res.end(Buffer.from(await resp.arrayBuffer()));
});
server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`listening ${server.address().port}\n`);
});
