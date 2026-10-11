"""phone-mint v2 (Option A — passkey/WebAuthn).

Mint a `merge.approval` grant from a passkey (Face ID / Touch ID) instead of the
Mac Secure Enclave, so the operator can authorize from his phone. Ruled Option A on the operator
trusted path 2026-07-19 (DECISIONS.md); design-of-record
`findings/2026-07-19-phone-mint-v2-design.md`.

The load-bearing new code is `webauthn_verify` — the pure-stdlib verification of
a WebAuthn assertion, the only security-critical new path v2 adds (design §4/§5).
It stays dependency-free for the same fresh-clone reason the rest of the verify
side is stdlib (guards.p256): every seat verifies offline.
"""
