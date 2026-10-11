# maw approval relay

A Cloudflare Worker at one stable URL. The operator approves a grant from his phone with
the Mac asleep and no Tailscale (refit goal R3, 2026-09-23).

**Trust, stated honestly (advisor F1 on #239):**
- The relay holds no key.
- A grant swapped in storage or in transit produces nothing that verifies, because
  the Mac checks the signature against its own copy.
- But the relay SERVES THE PAGE the operator reads. A modified Worker could show him one grant
  while the page signs another, and that WOULD verify.
- So `relay/*.mjs`, the Cloudflare account and the deploy path are **Layer-1 trust
  material on every mint**. Deploy only from reviewed `main`.
- The Mac narrows the exposure. It checks the assertion's origin and rpIdHash, so
  the key signs only on this domain. It also prints the challenge prefix the page
  must show, which catches a swap on the way, not a lying page.

## Files

- `worker.mjs`: the Worker entry. It exports ONLY `default`, because workerd refuses
  non-function named exports on the main module.
- `handler.mjs`: the routes, auth and storage logic, imported by the entry and the tests.
- `deploy.sh`: the one-command deploy (see below).
- `package.json` / `package-lock.json`: wrangler, pinned. Install with `npm ci`.
- `page.mjs`: GENERATED from `maw/phone_mint/serve.py`, so the relay serves the same
  page as the window server. Regenerate with
  `PYTHONPATH=maw python3 -m phone_mint.relay_client gen-page`. A test fails on drift.
- `dev_server.mjs`: a local stand-in for `wrangler dev` (node, in-memory store).
- `test/worker.test.mjs`: the Worker's own tests.

## Tests

- `node --test relay/test/*.mjs` runs the Worker tests (Node 25 refuses a bare directory).
- `python3 tests/run_all.py` runs them too (through `tests/phone_mint/test_relay.py`),
  plus the Mac-side end-to-end tests against the real Worker code via `dev_server.mjs`.
- Both need node, and skip without it.
- With `relay/node_modules` installed (`cd relay && npm ci`), the same end-to-end
  tests also run against the Worker booted in **workerd** by the pinned
  `wrangler dev --local`. That is the real runtime, with a local KV and no account.
  It caught a bug the node harness could not.
- None of them use Cloudflare or deploy anything.

## The Mac side

```bash
# post an unsigned grant (or --scope-class/--target/--session-file to assemble one)
PYTHONPATH=maw python3 -m phone_mint.relay_client mint --grant-file unsigned.json
# ... push the printed link to the operator ...
# ONE pull (exit 3 = not approved yet; pull again later, never poll):
PYTHONPATH=maw python3 -m phone_mint.relay_client pull --id <id> --out signed.json
```

The relay URL comes from `MAW_RELAY_URL`, and the post/pull secret from
`~/.secrets/maw-relay-secret` (chmod 600, never committed).

## Deploy (one command; the operator's permission covers the advisor running it)

```bash
relay/deploy.sh
```

From a clean main checkout, idempotent:
1. Creates `~/.secrets/maw-relay-secret` (random, 0600) if absent.
2. `npm ci`.
3. `wrangler login` only if not logged in.
4. Creates the `RELAY_KV` namespace once. Its id lives in `~/.maw/relay/kv-id`, and the
   deploy uses a gitignored `.deploy.wrangler.toml`, so the tracked `wrangler.toml`
   never changes.
5. `wrangler secret put RELAY_SECRET` with the secret on stdin, never argv.
6. `wrangler deploy` to the default **workers.dev** route.
7. Records the URL in `~/.maw/relay/url`, which the Mac side reads.

**RP ID:** the page uses the browser's default rpId, the page's hostname:
`maw-approval-relay.<account-subdomain>.workers.dev`. `workers.dev` is on the Public
Suffix List, so that hostname is its own registrable domain and a valid WebAuthn RP
ID. The passkey the operator enrolls binds to it. Changing the Worker name or route later
means enrolling a new key.

## The operator's gate: enroll the relay passkey (a Layer-1 key ceremony, never a seat)

A passkey is bound to its domain, so the relay needs its own key. Pick its id first and
set it as `relay_pubkey_id` in `~/.maw/operator.toml` (for example `phone-relay-v1`).
1. Run `PYTHONPATH=maw python3 -m phone_mint.relay_client register` and open the link on the phone.
   The phone shows the key fingerprint even if its capture fails. **If the page
   says the capture failed, abort that enrollment and start a new one.**
2. Run `relay_client pull --id <id> --out <relay-key-id>.pub`.
3. **Compare the fingerprint the phone shows with the one the Mac prints.** If they
   differ, do not commit the key.
4. Commit the `.pub` under `maw/guards/keys/` on the trusted path.

Residual, unspun: enrollment runs on the relay's own origin, so a relay compromised
at enrollment time could serve a page that reports its own key. The fingerprint
check catches a key swapped in storage or on the way to the Mac, but not a
malicious page. Deploying from reviewed source and enrolling once is the trust.
