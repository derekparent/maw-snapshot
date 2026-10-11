"""Pure-stdlib NIST P-256 (secp256r1) ECDSA signature VERIFICATION.

The seat side of `guards.human_grant` verifies the operator's grant signatures OFFLINE
against a committed public key — a fresh clone, no network, no third-party wheel.
Keeping verify dependency-free is what makes the fresh-clone story clean (design
doc §5a): every seat can verify with the stdlib alone; only the operator's signing Mac
needs the Secure-Enclave helper.

VERIFY ONLY. There is deliberately no signing, no key generation, and no private
-key material here — signing lives in the SE-backed helper on the operator's Mac. This
module takes a public key + message hash + (r, s) and returns True/False.

The failure mode of a hand-rolled ECDSA verify is ACCEPTING an invalid signature
via curve-arithmetic edge cases (advisor review, 2026-07-18), so every guard is
explicit and every reject is pinned by a Wycheproof-class test:
  - r, s strictly in [1, n-1] (rejects 0 and >= n);
  - the public key point is on the curve, in the field, and NOT the point at
    infinity (rejects off-curve / wrong-curve / infinity keys);
  - P-256 has prime order and cofactor 1, so any on-curve non-infinity point is a
    valid group element — no separate small-subgroup check is meaningful, and its
    absence is not a gap (unlike curves with cofactor > 1).

Fail-closed: any malformed input returns False, never raises to the caller.
"""
from __future__ import annotations

# NIST P-256 domain parameters (FIPS 186-4, D.1.2.3).
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = (P - 3) % P  # the curve's a coefficient is -3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

# A point is (x, y) with ints in [0, P); the point at infinity is None.
Point = "tuple[int, int] | None"


def _on_curve(pt: "tuple[int, int] | None") -> bool:
    """True iff pt is a finite affine point satisfying y^2 = x^3 - 3x + b (mod p)
    with both coordinates reduced into the field."""
    if pt is None:
        return False
    x, y = pt
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


def _add(p1: "tuple[int, int] | None", p2: "tuple[int, int] | None") -> "tuple[int, int] | None":
    """Affine point addition on P-256. None is the identity (point at infinity)."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None  # P + (-P) = infinity
    if p1 == p2:
        # Doubling: slope = (3x^2 + a) / (2y)
        s = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        s = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (s * s - x1 - x2) % P
    y3 = (s * (x1 - x3) - y1) % P
    return (x3, y3)


def _mul(k: int, pt: "tuple[int, int] | None") -> "tuple[int, int] | None":
    """Scalar multiplication k*pt via double-and-add (constant-time is NOT a goal
    here — this is verify only, over PUBLIC values, so there is no secret to leak)."""
    if k % N == 0 or pt is None:
        return None
    result: "tuple[int, int] | None" = None
    addend = pt
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def verify(pub: "tuple[int, int] | None", digest: int, r: int, s: int) -> bool:
    """True iff (r, s) is a valid P-256 ECDSA signature of `digest` under `pub`.

    pub: the public key as an (x, y) point (ints). digest: the message hash as an
    integer (for SHA-256 over P-256, the full 256-bit hash — bitlen(n) == 256, so
    no truncation). Fail-closed on every malformed input.
    """
    try:
        # (1) r, s must be in [1, n-1] — rejects 0 and anything >= n.
        if not (1 <= r < N and 1 <= s < N):
            return False
        # (2) the public key must be a finite on-curve point (rejects None /
        #     off-curve / wrong-curve / out-of-field / infinity).
        if not _on_curve(pub):
            return False
        # (3) the ECDSA verification equation.
        w = pow(s, -1, N)
        u1 = (digest * w) % N
        u2 = (r * w) % N
        point = _add(_mul(u1, (GX, GY)), _mul(u2, pub))
        if point is None:
            return False  # u1*G + u2*Q at infinity => invalid
        return (point[0] % N) == r
    except (ValueError, TypeError):
        # pow(_, -1, _) raises ValueError if the inverse doesn't exist; a
        # malformed pub raises TypeError on unpack. Either way: no valid sig.
        return False
