"""guards.human_grant — verify operator-signed instruction grants (issue #106).

A grant lets a relaying agent (the CoS) carry the operator's authority for ONE literal
instruction to a seat, which verifies — offline, no network, possibly a fresh
clone — that the operator signed *this exact text*, within a bounded scope, inside a short
TTL. The message-scoped sibling of session_auth. Full design + rulings:
`findings/2026-07-18-human-grant-design.md` (design-of-record, PR #120).

THIS MODULE IS THE VERIFY + LEDGER SIDE. Signing is the Secure-Enclave helper on
The operator's Mac (stage 3); the seat here only verifies. Signature math is the pure-stdlib
`guards.p256` verifier (verify-only, no private-key material anywhere in the tree).

THREAT MODEL (session_auth precedent — what this stops vs does NOT):
  STOPS: a fabricated relay ("the operator said X" with no valid signature); a stretched
    relay (the operator said X, the relayer claims X+Y — `instruction_sha256` binds the
    literal text); replay outside the TTL; replay of a consumed grant (nonce +
    consumed-ledger, single-use); scope creep (the scope_class enum; the ONLY
    merge shape is per-PR + SHA-bound + advisor-verdict-required, never a blanket
    or unreviewed merge; a session-token arm or scope amend ONLY through the
    typed `session` object of SESSION_CLASSES, which session_auth re-derives
    fact by fact — operator ruling 2026-09-23); key exfiltration (the signing
    key is non-exportable in the Secure Enclave — it never reaches this side).
  Does NOT stop: a compromised process operating DURING the grant's short TTL can
    spend it within its already-narrow scope — the signature authorizes the scope,
    not the agent's subsequent good behavior; or the operator misreading the mint prompt —
    the ceremony shows the FULL literal text precisely because that is the only
    defense, and it is a human one. Mint is Mac-only (Touch ID is local).

FAIL-CLOSED THROUGHOUT: every malformed input, every failed check, every missing
input returns a rejecting VerifyResult — never a raise to the caller, never a
partial-trust middle state. A grant is trusted only when EVERY check passes.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import json
import os
import re
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from bridge.format import _acquire_lock, _release_lock
from selfpath import MAW_RUN

# The operator-approved scope_class enumeration (design §3, the operator trusted-path 2026-07-18).
# merge.approval is the operator's own addition; any ADDITION is a Layer-1 change.
SCOPE_CLASSES = frozenset(
    {"seat.standdown", "goal.stage", "prompt.relay", "unblock.instruct", "merge.approval",
     "session.arm", "scope.amend", "session.arm.batch"}
)

# The two SESSION classes (operator ruling 2026-09-23, refit ruling 3, DECISIONS.md):
# "A passkey (Face ID) grant may ARM a session and AMEND its scope. Caps: 6h max,
# one repo, logged. This reverses 'a grant can never arm a session' for these two
# classes only." Every other class still refuses to arm or extend a session.
# The grant carries a `session` object naming exactly ONE repo, seat and session;
# guards.session_auth re-derives each of those facts itself and refuses on any
# mismatch, so the object is what the operator sees and signs, never what the seat trusts.
SESSION_CLASSES = frozenset({"session.arm", "scope.amend"})
# The classes that ARE an instruction to a seat: the only ones a delivery point
# (net recv) may verify and consume. An allowlist, so a class added later is
# refused at delivery until someone decides it belongs here.
INSTRUCTION_CLASSES = frozenset(
    {"seat.standdown", "goal.stage", "prompt.relay", "unblock.instruct", "merge.approval"}
)

# One Face ID arms N advisor seats (run 1002b, docs/design/batch-arm.md; the operator
# consented to "the new grant type" and signed the run scope naming it). The grant
# carries a `batch` manifest of session lines instead of one `session` object;
# each seat verifies the signature over the WHOLE manifest and claims only the
# one line equal to facts it derived itself (guards.session_auth).
BATCH_CLASS = "session.arm.batch"
# Classes that verify ONLY inside the session_auth ceremony (verify(ceremony=True)).
CEREMONY_CLASSES = SESSION_CLASSES | {BATCH_CLASS}
MAX_BATCH_LINES = 8
# The Touch ID fallback (R10) must be able to show a line's scope whole, so the
# cap is session_auth.PROMPT_SCOPE_MAX; goals are sized for a phone card.
BATCH_SCOPE_MAX = 200
GOAL_MAX_BULLETS = 3
GOAL_BULLET_MAX = 120
_BATCH_LINE_KEYS = {"repo", "seat", "session_id", "scope", "goal"}
# The seats a batch line may name: session_auth.MERGE_AUTHORIZED_SEATS, repeated
# here to avoid an import cycle (a test pins the two equal). A line naming any
# other seat is unclaimable (arm refuses the seat), so it must not verify or
# render as a card either.
BATCH_SEATS = ("advisor",)
# The relay's own grant_id rule. A colon can never occur, so a batch line claim
# `line:<grant_id>:<i>` can never equal any grant's id (F4).
GRANT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
_SESSION_KEYS = {
    "session.arm": {"repo", "seat", "session_id", "scope"},
    "scope.amend": {"repo", "seat", "session_id", "from_scope", "scope"},
}
# Optional in both classes: the enforced PR set, rendered "#1,#2" (sorted, no
# spaces) so it rides the same ASCII string rules and shows on the mint page.
_SESSION_OPTIONAL = {"prs"}
_PRS_RE = re.compile(r"#[1-9][0-9]*(,#[1-9][0-9]*)*")


def _ascii_field_error(name: str, v: object) -> "str | None":
    """The string rule every signed session field shares: non-empty, single-spaced,
    printable ASCII. canonical_bytes is Python json.dumps (ensure_ascii escapes to
    \\uXXXX); the phone page's canon() is JS JSON.stringify (raw UTF-8). A
    non-ASCII value would derive a different challenge on each side and fail only
    AFTER the operator's Face ID (advisor F1 on #237). Refuse it up front."""
    if not isinstance(v, str) or not v.strip() or v != " ".join(v.split()):
        return f"{name} must be a non-empty single-spaced string"
    bad = next((c for c in v if ord(c) > 0x7E or ord(c) < 0x20), None)
    if bad is not None:
        return (f"{name} must be printable ASCII: it contains {bad!r} "
                f"(U+{ord(bad):04X}); write '-' for an em dash")
    return None


def session_shape_error(scope_class: str, session: object) -> "str | None":
    """The ONE validator for a grant's `session` object, shared by the verifier
    and both mint surfaces so a mint can never produce a grant that won't verify.
    None when well-formed; otherwise the reason."""
    if scope_class not in SESSION_CLASSES:
        return None if session is None else "session present on a non-session grant"
    if not isinstance(session, dict):
        return f"{scope_class} requires a session object"
    keys, base = set(session), _SESSION_KEYS[scope_class]
    if not (base <= keys <= base | _SESSION_OPTIONAL):
        return (f"{scope_class} session object must have exactly {sorted(base)}"
                f" (plus optional {sorted(_SESSION_OPTIONAL)})")
    if "prs" in session and not (isinstance(session["prs"], str)
                                 and _PRS_RE.fullmatch(session["prs"])):
        return "session.prs must look like #1,#2 (PR numbers, comma-separated)"
    for k, v in session.items():
        why = _ascii_field_error(f"session.{k}", v)
        if why:
            return why
    return None


def batch_line_error(line: object, where: str = "batch line") -> "str | None":
    """None when one manifest line is well-formed, else why. Shared by the
    verifier, the mint and the seat's own request, so they cannot drift."""
    if not isinstance(line, dict):
        return f"{where} must be an object"
    keys = set(line)
    if not (_BATCH_LINE_KEYS <= keys <= _BATCH_LINE_KEYS | _SESSION_OPTIONAL):
        return (f"{where} must have exactly {sorted(_BATCH_LINE_KEYS)} "
                f"(plus optional {sorted(_SESSION_OPTIONAL)})")
    for k in sorted(keys - {"goal"}):
        why = _ascii_field_error(f"{where}.{k}", line[k])
        if why:
            return why
    if line["seat"] not in BATCH_SEATS:
        return f"{where}.seat {line['seat']!r} is not a seat that may merge ({list(BATCH_SEATS)})"
    if len(line["scope"]) > BATCH_SCOPE_MAX:
        return f"{where}.scope is {len(line['scope'])} chars; the cap is {BATCH_SCOPE_MAX}"
    if "prs" in line and not _PRS_RE.fullmatch(line["prs"]):
        return f"{where}.prs must look like #1,#2 (PR numbers, comma-separated)"
    goal = line["goal"]
    if not isinstance(goal, list) or not 1 <= len(goal) <= GOAL_MAX_BULLETS:
        return f"{where}.goal must be a list of 1..{GOAL_MAX_BULLETS} bullets"
    for i, b in enumerate(goal):
        why = _ascii_field_error(f"{where}.goal[{i}]", b)
        if why:
            return why
        if len(b) > GOAL_BULLET_MAX:
            return f"{where}.goal[{i}] is {len(b)} chars; the cap is {GOAL_BULLET_MAX}"
    return None


def batch_shape_error(scope_class: str, grant: dict) -> "str | None":
    """The `batch` key: required (and fully shaped) on BATCH_CLASS, absent, not
    even null, on every other class (F3: verify() otherwise ignores unknown keys)."""
    if scope_class != BATCH_CLASS:
        return "batch present on a non-batch grant" if "batch" in grant else None
    batch = grant.get("batch")
    if not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH_LINES:
        return f"{BATCH_CLASS} requires a batch of 1..{MAX_BATCH_LINES} lines"
    for i, line in enumerate(batch):
        why = batch_line_error(line, f"batch[{i}]")
        if why:
            return why
    for k in ("repo", "session_id"):
        if len({line[k] for line in batch}) != len(batch):
            return f"batch lines must not repeat a {k}"
    return None


def batch_instruction(batch: list) -> str:
    """The exact instruction text a batch grant signs, rendered from the grant's
    own manifest (the seat knows only its own line, so it cannot re-render the
    rest from facts; the signature binds the whole list instead)."""
    return (f"ARM merge auth for {len(batch)} sessions (6h max each, one session per "
            f"line): " + json.dumps(batch, sort_keys=True, separators=(",", ":")))


def line_claim_id(grant_id: str, index: int) -> str:
    """The single-use ledger id for ONE batch line. Disjoint from every grant id
    (GRANT_ID_RE has no colon), so no line claim can spend another grant."""
    return f"line:{grant_id}:{index}"


def session_instruction(scope_class: str, session: dict) -> str:
    """The exact instruction text a session grant signs. Rendered from the
    session object by BOTH sides: the mint (what the operator is shown) and the seat (from
    facts it derived itself), so the instruction-hash binding fails on any drift.

    The fields ride as canonical JSON, never joined with separators: a scope is
    free text, and a separator-joined rendering let two different session objects
    (e.g. a from_scope containing " — to: ") render the same instruction."""
    if scope_class not in SESSION_CLASSES:
        raise ValueError(f"not a session class: {scope_class!r}")
    verb = ("ARM merge auth (6h max, this session only)" if scope_class == "session.arm"
            else "AMEND merge scope (expiry unchanged)")
    return f"{verb}: " + json.dumps(session, sort_keys=True, separators=(",", ":"))


# TTL is minutes, not hours (design §4; report: "tighter than your 6-hour merge
# cap since this authorizes a specific act, not a session").
MAX_TTL_SECONDS = 900  # 15 minutes

# A small tolerance for a grant whose issued_at is a few seconds ahead of the
# verifier's clock (mint on one process, verify on another). NOT a freshness
# loosener — expiry is still hard.
_FUTURE_SKEW_SECONDS = 5

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# The committed public keys (design §7): ship with the plugin so a fresh clone
# verifies offline. maw/guards/keys/<pubkey_id>.pub. A change under this dir is a
# LAYER-1 change (design §7, Addition 1) — never rides an autonomous merge.
_KEYS_DIR = Path(__file__).resolve().parent / "keys"


def keys_dir() -> Path:
    override = os.environ.get("MAW_GRANT_KEYS_DIR")
    return Path(override) if override else _KEYS_DIR


def grants_dir() -> Path:
    """The shared, cross-repo grant ledger (design §7): ~/.maw/grants/, sibling to
    the switchboard net_dir. All seats on the Mac read it offline."""
    override = os.environ.get("MAW_GRANTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".maw" / "grants"


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str = ""


def _reject(reason: str) -> VerifyResult:
    return VerifyResult(False, reason)


# --- canonicalization ------------------------------------------------------

def canonical_bytes(grant: dict) -> bytes:
    """The exact bytes a grant's signature covers: the grant object WITHOUT its
    signature carrier, JSON with sorted keys and compact separators, UTF-8.
    Deterministic so the signer and every verifier agree byte-for-byte.

    A Mac-SE grant carries `sig`; a phone-minted grant carries `webauthn` (§9.5).
    Both are the signature and both are excluded — the phone must be able to
    compute the challenge from the grant BEFORE the assertion exists. Excluding a
    field a grant does not have is a no-op, so this is byte-identical for existing
    Mac-signed grants."""
    unsigned = {k: v for k, v in grant.items() if k not in ("sig", "webauthn")}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --- keyring ---------------------------------------------------------------

def load_key(pubkey_id: str) -> "tuple[tuple[int, int], str, str] | None":
    """Load a committed public key by id → ((x, y), not_before, not_after), or None
    if absent/malformed. Key file is JSON: {pubkey_id, x, y (hex), not_before,
    not_after (ISO)}. Fail-closed: any parse problem → None (unknown key)."""
    if not pubkey_id or "/" in pubkey_id or ".." in pubkey_id:
        return None  # path-injection defense: id lands in a filename
    f = keys_dir() / f"{pubkey_id}.pub"
    if not f.is_file():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        x = int(str(d["x"]), 16)
        y = int(str(d["y"]), 16)
        nb = str(d["not_before"])
        na = str(d["not_after"])
        if d.get("pubkey_id") != pubkey_id:
            return None  # the file's self-declared id must match its name
        return ((x, y), nb, na)
    except (ValueError, KeyError, TypeError, OSError):
        return None


def missing_key_reason(pubkey_id: object) -> str:
    """Why `load_key(pubkey_id)` found nothing, in words an operator can act on.
    Wording only: the caller has already refused.

    An EMPTY or missing store (no `*.pub` at all, as on a fresh install of the
    export) says no key is installed and where keys go. A store that holds keys,
    just not this one, keeps the `unknown pubkey_id` reason."""
    d = keys_dir()
    try:
        empty = not d.is_dir() or not any(d.glob("*.pub"))
    except OSError:
        empty = False  # unreadable: say no more than "unknown"
    if empty:
        return (f"no grant public key is installed (wanted pubkey_id {pubkey_id!r}): "
                f"the trust store {d} holds no <pubkey_id>.pub file, so no grant "
                "can verify. Installing a key is the operator's Layer-1 step; "
                "maw/guards/keys/README.md gives the file format, and "
                "MAW_GRANT_KEYS_DIR points at another store")
    return f"unknown pubkey_id: {pubkey_id!r}"


def keyset_alarm(record: bool = True) -> "str | None":
    """Addition-1 wiring, STRICT (advisor ruling 2026-07-18, target 9): while
    the committed key set differs from the reviewed baseline, EVERY grant
    presentation refuses — indefinitely. The baseline advances ONLY via the
    explicit `ack-keyset` verb, run after the keys-dir change has been reviewed
    on the trusted path (it slots into the Layer-1 key-commit ceremony).

    Auto-advancing after one refusal would defeat the alarm's whole purpose:
    the attack it exists for — swap the key via an armed-window merge — would
    succeed on RETRY (first presentation refused, attacker presents again,
    forged grants verify against the ambient new baseline). One stderr line is
    not "a human reviewed the rotation". This is a GATE, not telemetry.

    First-ever read (no recorded baseline) is bootstrap — records and passes.
    `record=False` is the PEEK path: a side-effect-free look records nothing,
    not even the bootstrap.

    Honest ceiling (target 10, accepted): the baseline is a FILE. `rm keyset.fp`
    resets to bootstrap — a local filesystem write, the same attacker who can
    edit the ledger or the session token. This alarm is a CONFUSION gate against
    an un-reviewed rotation riding normal channels, not a defense against
    filesystem write (the session_auth ceiling, stated the same way)."""
    current = keyset_fingerprint()
    fp_file = grants_dir() / "keyset.fp"
    d = grants_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _acquire_lock(d / ".ledger.lock")
    try:
        try:
            last = fp_file.read_text(encoding="utf-8").strip()
        except OSError:
            last = ""
        if last == current:
            return None
        if not last:
            # bootstrap: no baseline yet. Record it (unless peeking) and pass.
            if record:
                fp_file.write_text(current + "\n", encoding="utf-8")
                os.chmod(fp_file, 0o600)
            return None
        # A CHANGE: refuse — and keep refusing until an explicit ack. The
        # baseline is deliberately NOT advanced here.
        if last == _legacy_keyset_fingerprint():
            return (f"grant keyset fingerprint FORMAT upgraded (it now covers each "
                    f"key's validity window); key ids and points are unchanged. "
                    f"EVERY grant refuses until the validity windows in "
                    f"maw/guards/keys/ are reviewed on the trusted path and the "
                    f"new baseline is recorded: {ACK_KEYSET_CMD} — {_ack_records()}")
        return (f"committed grant key set CHANGED from the reviewed baseline "
                f"(was {last[:12]}, now {current[:12]}) — EVERY grant refuses "
                f"until the change is reviewed on the trusted path and "
                f"`{ACK_KEYSET_CMD}` records the new "
                f"baseline (maw/guards/keys/ is Layer-1) — {_ack_records()}")
    finally:
        _release_lock(lock)


def _ack_records() -> str:
    """Which keys the printed ack command would record. The command names the
    copy of maw that raised the alarm, and a build or seat worktree raises it
    exactly when ITS keys differ from the baseline: the operator has to see
    which directory he is about to bless."""
    return (f"that command records the keys in {keys_dir()}; run it only if that "
            f"is the copy whose keys were reviewed")


def ack_keyset() -> str:
    """Record the CURRENT committed key set as the reviewed baseline.

    The ONLY way the baseline advances past a rotation. Run it as part of the
    Layer-1 key-commit ceremony — after the operator has reviewed the keys-dir change on
    the trusted path — never as a reflex to make the alarm go away."""
    current = keyset_fingerprint()
    d = grants_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _acquire_lock(d / ".ledger.lock")
    try:
        (d / "keyset.fp").write_text(current + "\n", encoding="utf-8")
        os.chmod(d / "keyset.fp", 0o600)
    finally:
        _release_lock(lock)
    return current


def keyset_fingerprint() -> str:
    """A stable digest of the CURRENT committed key set (design §7, Addition 1).

    The verifier compares this to the set it last saw; a change is a finding
    (loud), never ambient — a rotated key must be NEWS to a seat. Sorted over
    (id, x, y, not_before, not_after): every field verify() trusts, so extending
    a key's validity window is as loud as swapping the key. Order-independent
    and content-addressed."""
    rows: list[list[str]] = []
    for stem, loaded in _keyset_rows():
        if loaded is None:
            rows.append([stem, "MALFORMED"])
        else:
            (x, y), nb, na = loaded
            rows.append([stem, f"{x:x}", f"{y:x}", nb, na])
    # JSON, not a separator-joined string: the fields contain ':' themselves.
    return hashlib.sha256(json.dumps(sorted(rows)).encode("utf-8")).hexdigest()


def _keyset_rows() -> "list[tuple[str, tuple[tuple[int, int], str, str] | None]]":
    d = keys_dir()
    if not d.is_dir():
        return []
    return [(f.stem, load_key(f.stem)) for f in sorted(d.glob("*.pub"))]


def _legacy_keyset_fingerprint() -> str:
    """The pre-2026-09-25 fingerprint (id, x, y only, ':'-joined). Used only to
    tell a FORMAT upgrade apart from a real key change in the alarm."""
    if not keys_dir().is_dir():
        return hashlib.sha256(b"").hexdigest()
    rows = []
    for stem, loaded in _keyset_rows():
        if loaded is None:
            rows.append(f"{stem}:MALFORMED")
        else:
            (x, y), _nb, _na = loaded
            rows.append(f"{stem}:{x:x}:{y:x}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


ACK_KEYSET_CMD = f"{MAW_RUN} guards.human_grant ack-keyset"


# --- ledger (allow-list + single-use) --------------------------------------

def _ledger_file() -> Path:
    return grants_dir() / "ledger.jsonl"


def _ledger_events() -> "list[dict]":
    f = _ledger_file()
    if not f.is_file():
        return []
    out: list[dict] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a corrupt line is ignored, never trusted
    return out


def _append_event(event: dict) -> None:
    d = grants_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _acquire_lock(d / ".ledger.lock")
    try:
        with open(_ledger_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        os.chmod(_ledger_file(), 0o600)
    finally:
        _release_lock(lock)


def record_issued(grant: dict) -> None:
    """Append the allow-list record for a minted grant (the mint side, stage 3).
    A seat rejects any grant whose id is not present here — the design's revocation
    workaround for offline tokens (delete/omit = revoked)."""
    _append_event({
        "event": "issued", "grant_id": grant["grant_id"],
        "scope_class": grant.get("scope_class"), "ts": grant.get("issued_at"),
    })


def is_issued(grant_id: str) -> bool:
    return any(e.get("event") == "issued" and e.get("grant_id") == grant_id
              for e in _ledger_events())


def is_consumed(grant_id: str) -> bool:
    return any(e.get("event") == "consumed" and e.get("grant_id") == grant_id
               for e in _ledger_events())


def consume(grant_id: str, nonce: str) -> bool:
    """Atomically CLAIM a grant as single-use-spent. Returns True for the FIRST
    caller, False if it was already consumed.

    The check-then-append MUST be atomic: verify()'s is_consumed() is only a fast
    pre-check, and two concurrent presentations can both pass it before either
    records the consume (advisor note N2, 2026-07-18). So the real single-use gate
    is THIS function — the read and the append happen under one lock, and callers
    ACT only when it returns True. A False means someone else already spent it."""
    d = grants_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _acquire_lock(d / ".ledger.lock")
    try:
        # read UNDER the lock — this is what makes the claim atomic.
        if any(e.get("event") == "consumed" and e.get("grant_id") == grant_id
               for e in _ledger_events()):
            return False
        with open(_ledger_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": "consumed", "grant_id": grant_id, "nonce": nonce,
                "ts": time.strftime(_TS_FMT, time.gmtime()),
            }) + "\n")
        os.chmod(_ledger_file(), 0o600)
        return True
    finally:
        _release_lock(lock)


# --- verify ----------------------------------------------------------------

def _epoch(ts: str) -> "int | None":
    try:
        return calendar.timegm(time.strptime(ts, _TS_FMT))
    except (ValueError, TypeError):
        return None


def _decode_sig(sig_b64: str) -> "tuple[int, int] | None":
    """A grant sig is base64 of the 64-byte r||s (each a 32-byte big-endian
    scalar). Fixed-width, not DER — no length-prefix ambiguity to exploit."""
    try:
        raw = base64.b64decode(sig_b64, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != 64:
        return None
    return (int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))


def verify_presented(
    grant: dict,
    instruction_text: str,
    now_iso: str,
    *,
    record_baseline: bool = True,
    live_head_sha: "str | None" = None,
    verdict_present: "bool | None" = None,
    receiver: "Collection[str] | None" = None,
    ceremony: bool = False,
) -> "VerifyResult":
    """The complete check for a grant someone is presenting: the keyset alarm
    FIRST, then verify(). A delivery point calls this rather than composing
    the two itself. `record_baseline=False` is the side-effect-free peek."""
    alarm = keyset_alarm(record=record_baseline)
    if alarm is not None:
        return _reject(alarm)
    return verify(grant, instruction_text, now_iso,
                  live_head_sha=live_head_sha, verdict_present=verdict_present,
                  receiver=receiver, ceremony=ceremony)


_REQUIRED = (
    "grant_id", "instruction_sha256", "scope_class", "target", "issued_at",
    "ttl_seconds", "nonce", "pubkey_id",
)
# The signature carrier is required too, but it is exactly ONE of these — a
# Mac-SE `sig` or a phone `webauthn` assertion — enforced in verify(), not here.
_SIG_CARRIERS = ("sig", "webauthn")


def verify(
    grant: dict,
    instruction_text: str,
    now_iso: str,
    *,
    live_head_sha: "str | None" = None,
    verdict_present: "bool | None" = None,
    receiver: "Collection[str] | None" = None,
    ceremony: bool = False,
) -> VerifyResult:
    """True-result iff EVERY check passes; fail-closed otherwise (design §8/§10).

    Enforced here: schema/types, scope_class enum, TTL bound, instruction-hash
    binding, committed-key signature (via guards.p256), key validity window,
    freshness (issued_at..+ttl), allow-list presence + single-use (ledger). For a
    `merge.approval` grant the caller MUST pass `live_head_sha` (the PR's current
    head) and `verdict_present` (does the advisor's verdict exist at that SHA) —
    the grant is refused unless the head matches its bound SHA AND a verdict
    exists. The grant replaces the operator's presence, NEVER the review.

    `receiver` is the set of names the RECEIVING seat answers to, derived by
    the caller (net/cli derives it from the recv entry). When given, the signed
    `target` must be one of them: a valid grant for another seat is not an order
    for this one. A delivery point must pass it; session grants bind their seat
    through the session object instead. OMITTING it skips the target check
    (fail-open by omission): the only delivery point today, net/cli
    _grant_trust, always passes it, and a test pins that.

    For the SESSION_CLASSES this checks the grant's shape and signature only.
    The caller (guards.session_auth) must pass `session_instruction()` rendered
    from facts IT derived, and compare the session object field by field, and
    must pass `ceremony=True`. Without it a session grant is refused: it is not
    an instruction, and a delivery point that verified it would go on to consume
    it, burning the seat's own arm (R16, run 1002b).
    """
    if not isinstance(grant, dict):
        return _reject("grant is not an object")
    if "typed" in grant:
        # A typed approval (guards.typed_approval) is a CoS record, never a
        # signed grant: it must not be able to verify here, whatever it carries.
        return _reject("a typed approval is not a signed grant")
    for k in _REQUIRED:
        if k not in grant:
            return _reject(f"missing field: {k}")

    scope = grant["scope_class"]
    if not isinstance(scope, str) or scope not in SCOPE_CLASSES:
        return _reject(f"unknown scope_class: {scope!r}")
    if scope in CEREMONY_CLASSES and not ceremony:
        return _reject(f"a {scope} grant verifies only inside the session_auth ceremony")
    gid = grant["grant_id"]
    if not isinstance(gid, str) or not GRANT_ID_RE.fullmatch(gid):
        return _reject("grant_id must match [A-Za-z0-9._-]{1,80}")

    if receiver is not None:
        target = grant["target"]
        if not isinstance(target, str) or target not in receiver:
            return _reject(f"grant target {target!r} is not this receiver "
                           f"({sorted(receiver)})")

    ttl = grant["ttl_seconds"]
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not (0 < ttl <= MAX_TTL_SECONDS):
        return _reject("ttl_seconds out of bounds")

    # merge shape: null for every class except merge.approval, where it is a
    # {repo, pr, head_sha} object. This is the ONLY merge shape a grant may carry.
    merge = grant.get("merge")
    if scope == "merge.approval":
        if not isinstance(merge, dict):
            return _reject("merge.approval requires a merge object")
        if not (isinstance(merge.get("repo"), str)
                and isinstance(merge.get("pr"), int) and not isinstance(merge.get("pr"), bool)
                and isinstance(merge.get("head_sha"), str) and len(merge["head_sha"]) == 40):
            return _reject("malformed merge object")
    elif merge is not None:
        return _reject("merge present on a non-merge.approval grant")

    # a grant may never carry a session-token arm/extend (design §11) — except
    # through the typed `session` object of the two SESSION_CLASSES (ruling 3).
    if grant.get("session_token_mint") or grant.get("arm_session"):
        return _reject("a grant may not arm or extend a session token")
    why = session_shape_error(scope, grant.get("session")) or batch_shape_error(scope, grant)
    if why is not None:
        return _reject(why)

    # instruction-hash binding: the literal text the seat received must be what
    # The operator signed.
    if not isinstance(instruction_text, str):
        return _reject("instruction_text missing")
    if hashlib.sha256(instruction_text.encode("utf-8")).hexdigest() != grant["instruction_sha256"]:
        return _reject("instruction hash mismatch")

    # a grant carries EXACTLY ONE signature carrier: a Mac-SE `sig` OR a phone
    # `webauthn` assertion. PRESENCE of the key decides, not its value — the
    # carrier container rides an operator-authorized artifact yet sits OUTSIDE the signed
    # bytes (canonical_bytes excludes both). A grant that carries the other
    # carrier key at all (even as null) is an uncovered writable channel, so it is
    # rejected rather than ignored (advisor N1).
    sig_present = "sig" in grant
    webauthn_present = "webauthn" in grant
    if sig_present and webauthn_present:
        return _reject("grant carries both sig and webauthn")
    if not sig_present and not webauthn_present:
        return _reject("grant carries no signature")

    # signature against the committed key (the SAME keyset for both carriers; a
    # phone key is just a distinct pubkey_id, §9.4 — a Layer-1 committed key, N6).
    key = load_key(grant["pubkey_id"])
    if key is None:
        return _reject(missing_key_reason(grant["pubkey_id"]))
    pub, not_before, not_after = key
    issued = grant["issued_at"]
    if not (not_before <= issued <= not_after):  # ISO strings sort chronologically
        return _reject("issued_at outside the key's validity window")
    try:
        canon = canonical_bytes(grant)  # can raise on a non-JSON-native value
    except (TypeError, ValueError):
        return _reject("grant is not JSON-serializable")

    if sig_present:
        from guards import p256  # local import: verify side stays a leaf dependency
        if not isinstance(grant["sig"], str):
            return _reject("malformed signature encoding")
        sig = _decode_sig(grant["sig"])
        if sig is None:
            return _reject("malformed signature encoding")
        digest = int.from_bytes(hashlib.sha256(canon).digest(), "big")
        if not p256.verify(pub, digest, sig[0], sig[1]):
            return _reject("signature does not verify")
    else:
        # phone WebAuthn path. guards DERIVES the challenge digest from the grant
        # it will ACT ON — never a caller-supplied digest. That is the §9.5
        # derive-what-you-display bind on the verify side: if a compromised mint
        # displayed text X but the grant object says Y, canon(Y)'s digest will not
        # equal the challenge the passkey signed over X, and the assertion fails.
        from phone_mint import webauthn_verify as wv
        webauthn = grant["webauthn"]
        if not isinstance(webauthn, dict):
            return _reject("webauthn must be an object")
        # strict keys: the container is uncovered by the signature, so it may
        # carry NOTHING beyond the three assertion fields (advisor N1).
        if set(webauthn) != {"client_data_json", "authenticator_data", "signature"}:
            return _reject("webauthn object has unexpected keys")
        try:
            cdj = wv.b64url_decode(webauthn["client_data_json"])
            authdata = wv.b64url_decode(webauthn["authenticator_data"])
            sig_der = wv.b64url_decode(webauthn["signature"])
        except (KeyError, TypeError, ValueError):
            return _reject("malformed webauthn assertion encoding")
        challenge_digest = hashlib.sha256(canon).digest()
        res = wv.verify_assertion(
            challenge_digest=challenge_digest,
            client_data_json=cdj,
            authenticator_data=authdata,
            signature_der=sig_der,
            pubkey_point=pub,
            require_uv=True,
        )
        if not res.ok:
            return _reject(f"webauthn assertion: {res.reason}")

    # freshness
    issued_e, now_e = _epoch(issued), _epoch(now_iso)
    if issued_e is None or now_e is None:
        return _reject("unparseable timestamp")
    if now_e + _FUTURE_SKEW_SECONDS < issued_e:
        return _reject("issued_at is in the future")
    if now_e > issued_e + ttl:
        return _reject("grant expired")

    # allow-list + single-use
    gid = grant["grant_id"]
    if not is_issued(gid):
        return _reject("grant_id not in the issued ledger (revoked or forged)")
    if is_consumed(gid):
        return _reject("grant already consumed (replay)")

    # merge.approval: the grant is the operator's presence, never the review — require the
    # live head to match the bound SHA AND the advisor's verdict to exist at it.
    if scope == "merge.approval":
        if live_head_sha is None or verdict_present is None:
            return _reject("merge.approval needs live_head_sha + verdict_present")
        if live_head_sha != merge["head_sha"]:
            return _reject("PR head moved since approval (SHA-drift)")
        if not verdict_present:
            return _reject("no advisor verdict at the approved SHA")

    return VerifyResult(True)


def main(argv: "list[str] | None" = None) -> int:
    import sys
    args = sys.argv[1:] if argv is None else argv
    if args == ["ack-keyset"]:
        fp = ack_keyset()
        print(f"keyset baseline recorded: {fp[:12]} from {keys_dir()} — grants "
              f"verify against the key set committed there")
        return 0
    print("usage: python3 -m guards.human_grant ack-keyset", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
