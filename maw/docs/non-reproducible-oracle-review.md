# Reviewing a non-reproducible oracle

The advisor playbook's keystone gate — *reproduce at the SHA in a clean
worktree, run the suite* — is the cheapest way to keep a reviewer from signing
a verdict on the builder's evidence. It **structurally breaks** when correctness
depends on something the reviewer cannot reproduce in a worktree:

- a **rendered UI** (a slug silently rendering the wrong layer; the default tab
  landing on the wrong screen),
- an **external service** (a hosted assistant, a vector store whose corpus
  isn't local, a weather-embed host returning a guide page instead of a map),
- a **stateful runtime condition** (a privacy-revoked simulator, a dead network
  host).

In those cases a passing suite proves nothing about the oracle, and the
fallback — *the builder shows a screenshot* — is the builder choosing what to
show, defeated by the stale-artifact hazard. maw ships tooling to close that
gap honestly.

## The honest boundary — what maw ships vs what the product supplies

**maw ships the orchestration + the gates. The product supplies the
forcers and probes.** This is not a hedge; it is the only honest split. A
forcer that drives a Ventusky embed into a known layer, a capture that reads
the active tab out of a simulator, a probe that asks an OpenAI Assistant — these
are product-specific and live in the product repo. maw cannot ship them without
faking coverage it does not have.

| Reusable (shipped in maw) | Product-specific (injected hook) |
| --- | --- |
| `review.harness` — force → capture → verify sequencing, short-circuit, verdict | the **force** command, the **capture** command |
| `review.external_oracle` — probe / mock / snapshot posture selection + live-hole bookkeeping | the **live-probe** command, the **mock/fixture** |
| `review.oracle` — snapshot record/diff, probe-or-mock | — |
| `review.provenance` — build-from-SHA stamp/verify gate | the **build** that produces the artifact + the post-build `stamp` call |
| `review.risk` — path → tier → gate routing | — |

**Command surface:** the shipped slash commands are `/maw:review`, `/maw:risk`,
`/maw:provenance`, `/maw:review-request`. `review.harness` / `review.external_oracle` /
`review.oracle` are **module CLIs** (`python3 -m review.harness …`) invoked from
product scripts — deliberately **not** slash commands, because the forcer/probe
they need is product-supplied (a `/harness` command with no product forcer would
be the hollow-command trap this arc fights).

Everything in the right column is an injected HOOK. The examples below show
those hooks as app-specific ILLUSTRATIONS — **product-applied, not shipped in
maw.**

## The state-forcing harness (`review.harness`)

Given a product-supplied force command and capture command plus a snapshot
reference, the harness:

0. (optional) verifies **build-from-SHA provenance** — the artifact under test
   was built from the SHA under review (aborts on a stale build);
1. runs the **force** command — drives the oracle into a known state;
2. runs the **capture** command **headlessly** — reads the forced state as
   deterministic text/JSON (NOT a screenshot);
3. **verifies** the capture against a recorded snapshot via `review.oracle`.

It stops at the first failing leg and reports which one. The order is
load-bearing and is the reusable part; the force/capture are yours.

```bash
# PRODUCT-APPLIED ILLUSTRATION — not shipped in maw.
# MyApp: prove the Ventusky embed renders a MAP, not a guide page.
maw-run review.harness run \
  --name ventusky-renders-map \
  --force   './scripts/force-ventusky-layer.sh wind' \
  --capture './scripts/capture-embed-dom-kind.sh' \
  --dir     .maw/review-snaps \
  --artifact build/MyApp.app --sha "$SHA_UNDER_REVIEW"
```

Here `force-ventusky-layer.sh` and `capture-embed-dom-kind.sh` are the app's;
the capture emits something like `kind=map layer=wind` — a deterministic line
the snapshot pins. If a regression makes the embed return a guide page, the
capture becomes `kind=guide-page` and the `verify` leg fails with a diff.

Other product-applied illustrations of the same hook shape:

- **Forced default tab** — `--force './scripts/launch-and-settle.sh'`,
  `--capture './scripts/capture-active-tab.sh'` emitting `active-tab=Regulations`.
  Pins which screen the app lands on. (one app's stale-DerivedData build showed
  4 tabs, not 5 — exactly why the `--artifact/--sha` provenance leg runs first.)
- **simctl privacy-revoke** — `--force 'xcrun simctl privacy <dev> revoke location ...'`,
  `--capture './scripts/capture-location-fallback.sh'` emitting the no-permission
  fallback string. Proves the app degrades correctly when a grant is revoked.

## The external-oracle helper (`review.external_oracle`)

For an oracle you can only reach by ASKING it (a hosted assistant, a remote
corpus). The capability asymmetry — the builder can reach the live service, a
CI / credential-isolated reviewer cannot — is handled by three postures:

- `probe-live` — hit the real service (needs credentials). The honest answer.
- `mock-for-ci` — a deterministic fixture; the **live hole is acknowledged**
  (`LIVE HOLE` in the verdict), never hidden. The only no-credentials path.
- `snapshot` — record/diff the answer for a human to compare across runs/SHAs.

```bash
# PRODUCT-APPLIED ILLUSTRATION — not shipped in maw.
# MyApp: does the hosted Assistant still answer the FAQ query correctly?
# CI (no credentials) → mock-for-ci against a recorded fixture:
maw-run review.external_oracle check \
  --mode mock-for-ci \
  --probe './scripts/ask-assistant.sh "what are your opening hours?"' \
  --mock  'cat fixtures/assistant-faq.txt'

# A reviewer WITH credentials → probe-live, and snapshot for the human diff:
maw-run review.external_oracle check \
  --mode snapshot \
  --probe './scripts/ask-assistant.sh "what are your opening hours?"' \
  --mock  'cat fixtures/assistant-faq.txt' \
  --name assistant-faq --dir .maw/review-snaps
```

A `mock-for-ci` pass is a CONDITIONAL pass: the verdict says `LIVE HOLE` so no
one mistakes it for a confirmed live result.

## Risk-tier routing ties it together

`review.risk` classifies changed paths into a tier and the tier SELECTS the
gate (`/maw:risk`):

- **docs** → light: read the diff, skip the suite.
- **runtime** → full review + reproduce the suite in a clean worktree.
- **oracle** (UI / external-service paths) → full review **plus** the
  state-forcing harness **plus** build-from-SHA provenance. `Gate.harness` is
  `True` only for this tier — the routing is real, not cosmetic: an oracle path
  is the only tier that demands the harness + provenance evidence.
- **unknown** → full review (fail-safe).

So a PR touching `MyApp/Views/MapView.swift` routes to the oracle gate and a
passing suite alone does NOT discharge it; a PR touching only `docs/` stays
light.

## Why not just a screenshot?

A screenshot is the builder choosing what to show, and it is defeated by the
stale-artifact hazard: the picture can be of a build that lags the SHA. The
harness captures a **deterministic** state and pins it to a snapshot; the
provenance gate proves the artifact was built from the SHA under review. Both
are scriptable, CI-able, and grant-free (no Accessibility / Screen-Recording
grant) — which is exactly what a credential-isolated or unattended reviewer
needs.
