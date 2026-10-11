# The operator grant public keys — the trust root for `guards.human_grant`

Committed public keys the seat side verifies grant signatures against, OFFLINE
(the key set is the root of trust: a grant is accepted only if its signature
verifies against a key committed here). One file per key: `<pubkey_id>.pub`, JSON:

```json
{
  "pubkey_id":  "operator-grant-2026-07",
  "x":          "<hex of the P-256 public point X>",
  "y":          "<hex of the P-256 public point Y>",
  "not_before": "2026-07-01T00:00:00Z",
  "not_after":  "2027-07-01T00:00:00Z"
}
```

The private key never lives here (or anywhere in the tree): it is non-exportable
in the operator's Mac Secure Enclave, used only by the signing helper, or inside a
passkey on the operator's phone. This dir holds public keys only.

Which id a NEW grant or enrollment claims is the operator's setting, not maw's:
`grant_pubkey_id` (Mac Secure Enclave mints), `phone_pubkey_id` (tailnet phone-mint)
and `relay_pubkey_id` (approval relay) in `~/.maw/operator.toml`, or their
`MAW_*_PUBKEY_ID` env vars. Unset, the minting tools refuse. The verifier never
reads them: it takes the id from the grant and trusts only the files here.

## THIS IS A LAYER-1 SURFACE (do not treat it as ordinary code)

The pubkey ships via the same repo the advisor merges autonomously inside an armed
window, so a bad merge could rotate in a forged key and every seat would then
verify forged grants against it. Therefore:

- **Any change under `maw/guards/keys/` — add, rotate, or remove a key — is a
  Layer-1 change: the operator trusted-path approval required, and it MUST NOT ride the
  autonomous advisor-merge path.** Treat it exactly like widening the merge scope.
- The verifier **alarms loudly** when the committed key set changes since a seat
  last read it (`human_grant.keyset_fingerprint`) — a rotated key is news, never
  ambient.

## Enrollment

Every step is run by **the operator**, never by a seat (an agent session). Each
key kind produces one `<pubkey_id>.pub` file in the JSON shape above. The file's
`pubkey_id` must equal its filename stem, or it is ignored. Set the id first
(`~/.maw/operator.toml`, or the env var), then run the steps. Unset, the tools
refuse. All commands run from the repo root.

Committing any of these files is a **Layer-1 change**: it needs the operator's
trusted-path approval and must not ride the autonomous advisor-merge path. When
the file is merged, run the acknowledge step at the end of this section.

### 1. Mac Secure Enclave key (`grant_pubkey_id`)

1. In `~/.maw/operator.toml` set `grant_pubkey_id = "<your-grant-key-id>"`
   (or export `MAW_GRANT_PUBKEY_ID`).
2. On the Mac that will sign, print the key's public point. The first call
   compiles the Swift helper from `maw/guards/human_grant_helper/sign.swift`
   (needs `swiftc`) and creates the non-exportable key:
   `cd maw && python3 -c "from guards.mint import SeSigner; print(*SeSigner.pubkey(), sep=chr(10))"`
   The two lines are `x` and `y` in hex. The key persists as an
   enclave-encrypted blob in the grants directory (`se-key.blob`); it is useless
   off that Mac.
3. Write `maw/guards/keys/<your-grant-key-id>.pub` by hand in the JSON shape
   above, using those `x` and `y`. You choose `not_before` and `not_after` (UTC,
   `YYYY-MM-DDTHH:MM:SSZ`). No command writes this file for you.
4. Commit it as a Layer-1 change, then acknowledge the key set (below).

### 2. Tailnet phone-mint passkey (`phone_pubkey_id`)

1. Set `phone_pubkey_id = "<your-phone-key-id>"` in `~/.maw/operator.toml`
   (or `MAW_PHONE_PUBKEY_ID`). This must differ from the Secure Enclave id.
2. On the Mac: `PYTHONPATH=maw python3 -m phone_mint.serve register --out <your-phone-key-id>.pub`
   (options: `--port`, default 8799; `--window` seconds, default 300). It serves
   on `127.0.0.1` for the window only, and prints the `tailscale serve` command to
   front it. Run that command; it needs Tailscale on the Mac and the phone.
3. On the phone, open the `https://<this-host>.<your-tailnet>.ts.net/` page and
   create the passkey. The command writes the `.pub` record to `--out`
   (`not_before` is the capture time, `not_after` is `2099-01-01T00:00:00Z`)
   and never writes into this directory itself.
4. Move the file to `maw/guards/keys/<your-phone-key-id>.pub`, commit it as a
   Layer-1 change, then acknowledge the key set (below).

### 3. Approval-relay passkey (`relay_pubkey_id`)

A passkey is bound to the domain it was created on, so the relay needs its own
key. The relay must already be deployed (`relay/deploy.sh`; the Mac reads its
URL from `~/.maw/relay/url`, or `MAW_RELAY_URL`, or `--relay <your-relay-url>`,
and its shared secret from `~/.secrets/maw-relay-secret`). Changing the relay's
Worker name or route later means enrolling a new key.

1. Set `relay_pubkey_id = "<your-relay-key-id>"` in `~/.maw/operator.toml`
   (or `MAW_RELAY_PUBKEY_ID`).
2. `PYTHONPATH=maw python3 -m phone_mint.relay_client register` prints a JSON
   `{"id": ..., "url": ...}` and a link. Open the link on the phone and create
   the passkey. The phone shows the key's fingerprint. If its page says the
   capture failed, abort and start a new enrollment.
3. After the phone says it is done, run once (do not poll):
   `PYTHONPATH=maw python3 -m phone_mint.relay_client pull --id <id> --out <your-relay-key-id>.pub`
   Exit status 3 means not captured yet. On success it prints the key
   fingerprint.
4. **Compare that fingerprint with the one the phone showed.** If they differ,
   do not commit the key.
5. Move the file to `maw/guards/keys/<your-relay-key-id>.pub` (`pull` already
   strips the fingerprint from the file), commit it as a Layer-1 change,
   then acknowledge the key set (below).

Residual: enrollment runs on the relay's own origin, so a relay compromised at
enrollment time could serve a page that reports its own key. The fingerprint
check catches a key swapped in storage or in transit, not a malicious page.
Deploy from reviewed source and enroll once.

### Acknowledge the changed key set

After any change here, the verifier refuses every grant until the operator
records the new key set as reviewed. On each Mac that runs seats, with the
installed plugin's launcher, after the plugin has synced the merged commit:

`maw-run guards.human_grant ack-keyset`

This is the operator's verb, never a seat's, and not a reflex to silence the
alarm: run it only after reviewing the keys-directory diff. The first-ever
read, with no recorded baseline, passes and records itself.

Not determined from the code: how a Secure Enclave key is re-created after its
blob is lost (the helper source says only that it needs a re-key and a new
`.pub` commit), and the Tailscale setup beyond the `tailscale serve` line the
command prints.

## Rotation

Add a new `<new-id>.pub` and issue under the new `pubkey_id`; keep old keys
committed with their validity windows so in-flight grants still verify. Never
delete a key from history (audit).

## Status

The public export ships with no `.pub` files: its trust store is empty, so no
grant can verify and the minting and approval paths fail closed until the
operator enrolls a key (see Enrollment). Keys are the operator's own; none is
provided. In a tree where the operator has enrolled, the trusted keys are exactly
the `.pub` files in this directory.
