#!/usr/bin/env bash
# Deploy the maw approval relay (refit R3b). Run from a clean main checkout:
#
#   relay/deploy.sh
#
# The operator's permission (2026-09-23, relayed by the advisor): the ADVISOR may run
# `wrangler login` and this deploy. The passkey enrollment afterwards is the operator's own
# ceremony, never a seat's (README, "the operator's gates").
#
# Steps, all idempotent:
#   1. the post/pull secret: create ~/.secrets/maw-relay-secret (random, 0600) if absent
#   2. npm ci (wrangler pinned in package-lock.json)
#   3. wrangler login, only if `wrangler whoami` says we are not logged in
#   4. the KV namespace: create it once; its id lives in ~/.maw/relay/kv-id, and
#      the deploy uses a generated, gitignored .deploy.wrangler.toml, so the
#      tracked wrangler.toml never changes
#   5. `wrangler secret put RELAY_SECRET` with the secret on STDIN (never argv)
#   6. `wrangler deploy` to the default workers.dev route
#   7. record the deployed URL in ~/.maw/relay/url for the Mac side
set -euo pipefail
cd "$(dirname "$0")"

SECRET_FILE="${MAW_RELAY_SECRET_FILE:-$HOME/.secrets/maw-relay-secret}"
STATE_DIR="$HOME/.maw/relay"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

if [ ! -s "$SECRET_FILE" ]; then
  mkdir -p "$(dirname "$SECRET_FILE")"
  ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$SECRET_FILE" )
  echo "created the relay secret at $SECRET_FILE (0600)"
fi
chmod 600 "$SECRET_FILE"

# MAW_RELAY_WRANGLER swaps in a stub for tests (tests/phone_mint/test_relay.py).
if [ -z "${MAW_RELAY_WRANGLER:-}" ]; then
  npm ci --no-audit --no-fund --loglevel=error
fi
WR="${MAW_RELAY_WRANGLER:-./node_modules/.bin/wrangler}"

if ! "$WR" whoami 2>/dev/null | grep -q "You are logged in"; then
  "$WR" login
fi

KV_FILE="$STATE_DIR/kv-id"
if [ ! -s "$KV_FILE" ]; then
  out="$("$WR" kv namespace create RELAY_KV)"
  id="$(printf '%s\n' "$out" | grep -Eo '[0-9a-f]{32}' | head -1)"
  if [ -z "$id" ]; then
    printf 'could not read the KV namespace id from:\n%s\n' "$out" >&2
    exit 1
  fi
  printf '%s\n' "$id" > "$KV_FILE"
fi
KV_ID="$(cat "$KV_FILE")"
sed "s/REPLACE_WITH_THE_KV_NAMESPACE_ID_AT_DEPLOY/$KV_ID/" wrangler.toml > .deploy.wrangler.toml

# wrangler stores stdin VERBATIM (no trim) and the Mac strips the file, so feed
# the stripped secret with no trailing newline (advisor F7). printf is a shell
# builtin: the secret never reaches any argv.
printf '%s' "$(cat "$SECRET_FILE")" | "$WR" secret put RELAY_SECRET --config .deploy.wrangler.toml

out="$("$WR" deploy --config .deploy.wrangler.toml)"
printf '%s\n' "$out"
url="$(printf '%s\n' "$out" | grep -Eo 'https://[a-z0-9.-]+\.workers\.dev' | head -1)"
if [ -z "$url" ]; then
  echo "deployed, but no workers.dev URL in the output; set MAW_RELAY_URL by hand" >&2
  exit 1
fi
printf '%s\n' "$url" > "$STATE_DIR/url"
host="${url#https://}"
echo
echo "relay live at $url (recorded in $STATE_DIR/url)"
echo "WebAuthn RP ID for the passkey enrollment: $host"
echo "Next (the operator only): enroll the relay key (relay_pubkey_id in ~/.maw/operator.toml) per relay/README.md."
