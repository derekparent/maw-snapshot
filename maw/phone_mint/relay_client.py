"""phone_mint.relay_client — the Mac side of the hosted approval relay (refit R3b).

The relay (relay/worker.mjs, a Cloudflare Worker) lets the operator approve from his phone
with the Mac asleep and no Tailscale (refit goal R3, operator ruling 2026-09-23). This
module is everything the Mac does, stdlib only:

  post    — assemble (or take) an UNSIGNED grant, keep the Mac's own copy under
            ~/.maw/relay/pending/<id>.json, and POST it to the relay. Prints the
            link to push to the operator. Returns at once: it never waits for the operator.
  pull    — ONE-SHOT: GET the result. Not signed yet → exit 3 ("pull again
            later"), no polling loop. Signed → attach the assertion to the MAC'S
            OWN COPY via mint_session.attach_assertion, which self-verifies it
            OFFLINE against the committed key before anything is written. So a
            relay that swapped the displayed bytes, or replays an old assertion,
            yields nothing that verifies.
  register— the one-time passkey enrollment on the relay's domain. The pull writes
            a .pub record to --out for the operator to commit on the trusted path (a
            Layer-1 key ceremony). It NEVER writes into the keys dir itself.
  gen-page— regenerate relay/page.mjs from serve.py's _PAGE_TMPL (one page, two
            hosts; tests pin the committed file to the generator).

The relay's post/pull secret is read from MAW_RELAY_SECRET_FILE (default
~/.secrets/maw-relay-secret), never argv and never committed. The relay URL comes
from --relay, MAW_RELAY_URL, or ~/.maw/relay/url (written by relay/deploy.sh).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import opconf
from guards import human_grant as hg
from phone_mint import mint_session as ms
from phone_mint import serve
from selfpath import MAW_RUN

DEFAULT_SECRET_FILE = Path.home() / ".secrets" / "maw-relay-secret"
# The relay domain needs its OWN passkey (WebAuthn binds a passkey to its rpId), so
# its committed key is distinct from the tailnet key. The operator enrolls it (Layer-1).
PULL_PENDING = 3
USER_AGENT = "maw-relay-client/1"
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PAGE_MJS = Path(__file__).resolve().parents[2] / "relay" / "page.mjs"


class RelayError(RuntimeError):
    pass


def relay_pubkey_id() -> str:
    try:
        return opconf.require("relay_pubkey_id")
    except opconf.NotConfigured as e:
        raise RelayError(str(e)) from None


def pending_dir() -> Path:
    override = os.environ.get("MAW_RELAY_PENDING_DIR")
    return Path(override) if override else Path.home() / ".maw" / "relay" / "pending"


def read_secret(path: "str | None" = None) -> str:
    p = Path(path or os.environ.get("MAW_RELAY_SECRET_FILE") or DEFAULT_SECRET_FILE)
    try:
        secret = p.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RelayError(f"cannot read the relay secret at {p}: {e}") from None
    if len(secret) < 32:
        raise RelayError(f"the relay secret at {p} is shorter than 32 characters")
    if p.stat().st_mode & 0o077:
        raise RelayError(f"the relay secret at {p} is readable by others; chmod 600 it")
    return secret


def url_file() -> Path:
    """Where relay/deploy.sh records the deployed URL."""
    return Path(os.environ.get("MAW_RELAY_URL_FILE") or Path.home() / ".maw" / "relay" / "url")


def relay_url(explicit: "str | None" = None) -> str:
    url = explicit or os.environ.get("MAW_RELAY_URL") or ""
    if not url:
        try:
            url = url_file().read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
    url = url.rstrip("/")
    if not url:
        raise RelayError(f"no relay URL: pass --relay, set MAW_RELAY_URL, or deploy "
                         f"(relay/deploy.sh records it in {url_file()})")
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    if not (parts.scheme == "https" and parts.hostname) and not local:
        raise RelayError("the relay URL must be https (plain http only for a local dev relay)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise RelayError("the relay URL must be a bare origin (no credentials, query or fragment)")
    return url


def _call(method: str, url: str, secret: str, payload: "dict | None" = None) -> "tuple[int, bytes]":
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "authorization": f"Bearer {secret}", "content-type": "application/json",
        # Cloudflare's browser-integrity check 403s the default Python-urllib
        # User-Agent ("error code: 1010"), seen live on the deployed relay.
        "user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — URL scheme checked above
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise RelayError(f"relay unreachable: {e}") from None


def canon_safe(v: object, where: str = "grant") -> str:
    """'' when every string is printable ASCII and every number an int, else why.
    canonical_bytes (Python, ensure_ascii) and the page's canon() (JS) agree only
    on those; anything else would fail AFTER the operator's Face ID (R3a F1, relay F5)."""
    if isinstance(v, dict):
        for k, x in v.items():
            why = canon_safe(k, where) or canon_safe(x, f"{where}.{k}")
            if why:
                return why
        return ""
    if isinstance(v, list):
        return next((w for w in (canon_safe(x, f"{where}[]") for x in v) if w), "")
    if isinstance(v, str):
        bad = next((c for c in v if not (0x20 <= ord(c) <= 0x7E)), None)
        return "" if bad is None else f"{where} holds {bad!r} (U+{ord(bad):04X}); only printable ASCII"
    if isinstance(v, bool) or v is None:
        return ""
    if isinstance(v, int):
        return ""
    return f"{where} holds a {type(v).__name__}; only integers"


def challenge_prefix(grant: dict, n: int = 12) -> str:
    """The first characters of the challenge the page displays (b64url of
    sha256(canon(grant))). The Mac prints it at mint so the operator can compare it with the
    page. PARTIAL, and named so: a malicious page can display anything; this
    catches a storage or transport swap, not a lying page."""
    import base64
    import hashlib
    d = hashlib.sha256(hg.canonical_bytes(grant)).digest()
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()[:n]


def check_origin(cap: dict, relay: str) -> str:
    """'' when the assertion was made on the relay's own origin and RP ID, else
    why. Confines a relay grant to the configured domain (a key enrolled there
    signs only there). It does NOT stop a malicious Worker on that same domain."""
    import hashlib
    from urllib.parse import urlsplit
    from phone_mint import webauthn_verify as wv
    parts = urlsplit(relay)
    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        cdj = json.loads(wv.b64url_decode(str(cap.get("client_data_json_b64", ""))))
        authdata = wv.b64url_decode(str(cap.get("authenticator_data_b64", "")))
    except (ValueError, TypeError):
        return "unreadable clientDataJSON or authenticatorData"
    if not isinstance(cdj, dict) or cdj.get("origin") != origin:
        return f"clientDataJSON.origin is {cdj.get('origin') if isinstance(cdj, dict) else None!r}, not {origin!r}"
    if authdata[:32] != hashlib.sha256((parts.hostname or "").encode()).digest():
        return f"rpIdHash is not sha256({parts.hostname!r})"
    return ""


def post(kind: str, grant: "dict | None", *, relay: str, secret: str) -> "tuple[str, str]":
    """POST a request; keep the Mac's own copy. Returns (id, page url)."""
    if kind == "mint":
        if not isinstance(grant, dict) or "sig" in grant or "webauthn" in grant:
            raise RelayError("mint needs an UNSIGNED grant")
        why = canon_safe(grant)
        if why:
            raise RelayError(f"refusing to post: {why}")
    status, body = _call("POST", f"{relay}/api/request", secret,
                         {"kind": kind, "grant": grant} if kind == "mint" else {"kind": kind})
    if status != 200:
        raise RelayError(f"relay refused the request ({status}): {body[:200]!r}")
    reply = json.loads(body)
    rid = str(reply.get("id", ""))
    if not _ID_RE.match(rid):
        raise RelayError(f"relay reply is malformed: {reply!r}")
    url = f"{relay}/r/{rid}"  # built HERE, never taken from the relay's reply
    d = pending_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    local = d / f"{rid}.json"
    local.write_text(json.dumps({"kind": kind, "grant": grant, "relay": relay,
                                 "posted_at": time.strftime(hg._TS_FMT, time.gmtime())}),
                     encoding="utf-8")
    os.chmod(local, 0o600)
    return rid, url


def pull(rid: str, *, secret: str) -> "dict | None":
    """ONE pull. None when the operator has not approved yet. Otherwise the verified result:
    for mint, the completed grant (self-verified against the committed key and the
    Mac's own copy); for register, the .pub record for the operator to commit."""
    if not _ID_RE.match(rid or ""):
        raise RelayError("bad request id")
    local = pending_dir() / f"{rid}.json"
    try:
        mine = json.loads(local.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RelayError(f"no local copy of request {rid} ({e}); the Mac only trusts its own copy") from None
    status, body = _call("GET", f"{mine['relay']}/api/result/{rid}", secret)
    if status == 202:
        return None
    if status != 200:
        raise RelayError(f"relay result {status}: {body[:200]!r}")
    reply = json.loads(body)
    caps = reply.get("captures")
    if (reply.get("kind") != mine["kind"] or not isinstance(caps, list) or not caps
            or not all(isinstance(c, dict) for c in caps)):
        raise RelayError("relay reply does not match the request kind")
    if mine["kind"] == "register":
        if len(caps) != 1:
            raise RelayError(f"{len(caps)} keys were captured for one enrollment: possible "
                             f"interference. Nothing written; run the enrollment again.")
        spki = caps[0].get("spki_pubkey_b64url")
        rec = serve.build_pubkey_record(
            spki, pubkey_id=relay_pubkey_id(),
            not_before=time.strftime(hg._TS_FMT, time.gmtime()), not_after="2099-01-01T00:00:00Z")
        rec = dict(rec, _fingerprint=spki_fingerprint(spki))
        local.unlink()
        try:
            _call("DELETE", f"{mine['relay']}/api/result/{rid}", secret)
        except RelayError:
            pass
        return rec
    pubkey_id = mine["grant"].get("pubkey_id") if isinstance(mine.get("grant"), dict) else None
    key = hg.load_key(str(pubkey_id)) if pubkey_id else None
    if key is None:
        raise RelayError(f"{hg.missing_key_reason(pubkey_id)}: commit the relay key first")
    # Anyone with the page link can post a capture, so try each; accept only one
    # that verifies against the MAC's OWN copy of the grant, never the relay's.
    reasons = []
    for cap in caps:
        why = check_origin(cap, mine["relay"])
        if why:
            reasons.append(why)
            continue
        grant = dict(mine["grant"])
        try:
            done = ms.attach_assertion(
                grant, client_data_json_b64=cap.get("client_data_json_b64"),
                authenticator_data_b64=cap.get("authenticator_data_b64"),
                signature_b64=cap.get("signature_b64"), pubkey_point=key[0])
        except ValueError as e:
            reasons.append(str(e))
            continue
        local.unlink()
        try:  # verified: now the relay may forget it (best effort; KV TTL is the backstop)
            _call("DELETE", f"{mine['relay']}/api/result/{rid}", secret)
        except RelayError:
            pass
        return done
    raise RelayError("no captured assertion verifies against the Mac's copy of the grant: "
                     + "; ".join(reasons))


def spki_fingerprint(spki_b64url: str) -> str:
    """SHA-256 of the SPKI bytes, hex: the same value the register page shows."""
    import hashlib
    from phone_mint import webauthn_verify as wv
    return hashlib.sha256(wv.b64url_decode(spki_b64url)).hexdigest()


def page_fingerprint() -> str:
    """sha256 (hex) of the page this Mac would serve: the relay's
    `/api/page-sha` must equal it before a batch grant is minted (R20)."""
    import hashlib
    return hashlib.sha256(serve._PAGE_TMPL.encode("utf-8")).hexdigest()


def deployed_page_sha(*, relay: str, secret: str) -> str:
    """The relay's own page fingerprint, or '' when it has none (an older
    deploy predating /api/page-sha): the caller treats '' as a mismatch."""
    status, body = _call("GET", f"{relay}/api/page-sha", secret)
    if status != 200:
        return ""
    try:
        sha = json.loads(body).get("page_sha256", "")
    except (ValueError, AttributeError):
        return ""
    return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else ""


# --- batch arm (run 1002b, docs/design/batch-arm.md §4, §7, §10) ------------
# The CoS collects N advisors' lines, mints ONE grant, polls for the operator's
# signature (bounded by the grant's own expiry: this replaces him typing "done"),
# writes the signed grant to a file and sends each advisor a PLAIN notice naming
# it. The grant verifies itself; the transport carries no trust.
BATCH_TTL_SECONDS = 900
BATCH_POLL_SECONDS = 15
DELIVER_MIN_LEFT_SECONDS = 120


def batch_dir() -> Path:
    return hg.grants_dir() / "batch"


def read_batch_lines(paths: "list[Path]") -> list:
    """The lines, from each advisor's `grant-request session.arm --batch` output."""
    lines = []
    for f in paths:
        try:
            doc = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RelayError(f"unreadable batch line {f}: {e}") from None
        if not isinstance(doc, dict) or set(doc) != {"line"}:
            raise RelayError(f"{f} is not `grant-request --batch` output (one 'line' object)")
        lines.append(doc["line"])
    return lines


def assemble_batch(lines: list, *, now_iso: str, pubkey_id: str,
                   ttl: int = BATCH_TTL_SECONDS) -> dict:
    """One unsigned session.arm.batch grant. The production assembler validates
    the manifest (caps, ASCII, uniqueness) before anything is posted."""
    return ms.assemble_unsigned_grant(
        instruction=hg.batch_instruction(lines), scope_class=hg.BATCH_CLASS,
        target="advisor", ttl_seconds=ttl, now_iso=now_iso, pubkey_id=pubkey_id,
        batch=lines)


def mint_batch(grant: dict, *, relay: str, secret: str,
               deployed_sha=None) -> "tuple[str, str]":
    """Post the batch grant, but ONLY to a relay serving the page that can
    describe it (R20): otherwise the operator would Face ID raw JSON."""
    got = (deployed_sha or deployed_page_sha)(relay=relay, secret=secret)
    want = page_fingerprint()
    if got != want:
        raise RelayError(
            f"refusing to mint: the relay's page ({got[:12] or 'no fingerprint'}) is not "
            f"this Mac's batch-capable page ({want[:12]}). The relay Worker needs a deploy "
            f"(operator only). Arm these seats with single-seat links meanwhile.")
    return post("mint", grant, relay=relay, secret=secret)


def pull_batch(rid: str, *, secret: str, sleep=time.sleep, clock=time.time,
               poll_seconds: int = BATCH_POLL_SECONDS) -> "tuple[Path, int]":
    """Poll for the operator's signature until the grant expires (never past it),
    then write the verified grant 0600 under batch_dir(). Returns (path, seconds
    left before the grant expires)."""
    try:
        mine = json.loads((pending_dir() / f"{rid}.json").read_text(encoding="utf-8"))
        g = mine["grant"]
        expires = hg._epoch(g["issued_at"]) + int(g["ttl_seconds"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RelayError(f"no usable local copy of request {rid} ({e})") from None
    if g.get("scope_class") != hg.BATCH_CLASS:
        raise RelayError(f"request {rid} is not a batch grant")
    while True:
        done = pull(rid, secret=secret)
        if done is not None:
            break
        left = expires - clock()
        if left <= 0:
            raise RelayError(f"the batch grant expired unsigned (request {rid}); re-mint")
        sleep(min(poll_seconds, left))
    d = batch_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"{done['grant_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(done, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path, int(expires - clock())


def batch_notice(grant: dict, index: int, path: Path) -> str:
    """The plain text one advisor receives. No attachment, no grant bytes. Call
    it only on a grant load_batch_grant() accepted: every value interpolated
    here is then single-spaced printable ASCII, and the suggested command is
    shell-quoted, so the notice cannot carry a forged line or a shell tail."""
    import shlex
    line = grant["batch"][index]
    expires = time.strftime(hg._TS_FMT, time.gmtime(
        hg._epoch(grant["issued_at"]) + int(grant["ttl_seconds"])))
    cmd = ["guards.session_auth", "arm", "--grant-file", str(path),
           "--scope", "<your requested scope>"]
    if "prs" in line:
        cmd += ["--prs", ",".join(re.findall(r"[0-9]+", line["prs"]))]
    return (f"batch arm grant ready: one Face ID for {len(grant['batch'])} advisor(s); "
            f"yours is line {index} ({line['repo']}).\n"
            f"file: {path}\nexpires: {expires}\n"
            f"Arm before anything else, with the SAME scope you requested:\n"
            f"  {MAW_RUN} {shlex.join(cmd)}\n"
            f"The grant verifies itself; this notice carries no authority.")


def _maw_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _net_cli(*argv: str) -> "subprocess.CompletedProcess":
    import subprocess
    # Run maw's OWN net.cli: cwd and PYTHONPATH are this plugin's tree, never the
    # caller's repo (whose top-level net/ would otherwise shadow it, #324).
    return subprocess.run([sys.executable, "-m", "net.cli", *argv], capture_output=True,
                          text=True, timeout=60, cwd=str(_maw_root()),
                          env={**os.environ, "PYTHONPATH": str(_maw_root())})


def _net_send(net: str, peer: str, sender: str, body_file: Path) -> "subprocess.CompletedProcess":
    # `--flag=value` form after the subcommand (--net is a subcommand option):
    # a value can never parse as another flag.
    return _net_cli("send", peer, f"--net={net}", f"--as={sender}", f"--from-file={body_file}")


def _net_who(net: str) -> list:
    """The live entries of one pair switchboard (read-only: --no-prune)."""
    cp = _net_cli("who", f"--net={net}", "--json", "--no-prune")
    try:
        rows = json.loads(cp.stdout) if cp.returncode == 0 else []
    except ValueError:
        rows = []
    return rows if isinstance(rows, list) else []


# A switchboard slug as the launcher makes them (sw-<slug>): no path, no flag,
# no `~`. Repo names reach argv below, so they are checked even after the shape.
_NET_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def load_batch_grant(path: Path) -> dict:
    """The grant deliver-batch may act on: the file pull-batch wrote, and a
    manifest the verifier's own shape rule accepts. Delivery does not verify the
    signature (each seat does, at arm), but it never turns unvalidated text into
    a notice or an argv: a forged line in an advisor's pane is read BEFORE arm."""
    try:
        grant = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RelayError(f"unreadable batch grant {path}: {e}") from None
    if not isinstance(grant, dict) or grant.get("scope_class") != hg.BATCH_CLASS:
        raise RelayError(f"{path} is not a {hg.BATCH_CLASS} grant")
    gid = grant.get("grant_id")
    if not isinstance(gid, str) or not hg.GRANT_ID_RE.fullmatch(gid):
        raise RelayError("the grant_id is malformed")
    if Path(path).resolve() != (batch_dir() / f"{gid}.json").resolve():
        raise RelayError(f"deliver only the file pull-batch wrote: {batch_dir() / (gid + '.json')}")
    why = hg.batch_shape_error(hg.BATCH_CLASS, grant)
    if why:
        raise RelayError(f"refusing to deliver: {why}")
    if hg._epoch(str(grant.get("issued_at"))) is None or not isinstance(grant.get("ttl_seconds"), int):
        raise RelayError("the grant's issued_at/ttl_seconds are malformed")
    for line in grant["batch"]:
        if not _NET_SLUG_RE.fullmatch(line["repo"].rsplit("/", 1)[-1]):
            raise RelayError(f"repo {line['repo']!r} does not name a switchboard slug")
    return grant


def deliver_batch(path: Path, *, sender: str, clock=time.time, send=_net_send,
                  who=_net_who) -> "list[tuple[str, bool, str]]":
    """Send every line's advisor its notice over its pair's switchboard
    (`--net <repo name>`, the launcher convention). The recipient is the live
    advisor-role entry whose session_id IS the line's, so a generation-2 seat
    (advisor2) gets its own notice and a stale name gets none. Refuses with
    under DELIVER_MIN_LEFT_SECONDS left: re-mint instead of racing N seats."""
    if not _NET_SLUG_RE.fullmatch(sender or ""):
        raise RelayError(f"sender {sender!r} is not a registered-name shape")
    grant = load_batch_grant(path)
    left = hg._epoch(grant["issued_at"]) + grant["ttl_seconds"] - clock()
    if left < DELIVER_MIN_LEFT_SECONDS:
        raise RelayError(f"only {int(left)}s left on the grant: re-mint for the unarmed "
                         f"seats instead of delivering")
    import tempfile
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i, line in enumerate(grant["batch"]):
            net = line["repo"].rsplit("/", 1)[-1]
            peers = [e.get("name") for e in who(net) if isinstance(e, dict)
                     and e.get("role") == "advisor" and e.get("session_id") == line["session_id"]]
            if len(peers) != 1 or not _NET_SLUG_RE.fullmatch(str(peers[0])):
                out.append((line["repo"], False, f"no live advisor with session "
                                                 f"{line['session_id']} on {net}"))
                continue
            body = Path(td) / f"notice-{i}.txt"
            body.write_text(batch_notice(grant, i, Path(path)), encoding="utf-8")
            cp = send(net, peers[0], sender, body)
            tail = (cp.stdout or cp.stderr or "").strip().splitlines()
            out.append((line["repo"], cp.returncode == 0, tail[-1] if tail else ""))
    return out


def page_module_source() -> str:
    return ("// GENERATED from maw/phone_mint/serve.py _PAGE_TMPL by\n"
            "// `python3 -m phone_mint.relay_client gen-page`. Do not edit by hand:\n"
            "// the relay must serve the SAME page the window server serves.\n"
            f"export const PAGE_TMPL = {json.dumps(serve._PAGE_TMPL)};\n")


def _assemble(ns) -> dict:
    session = json.loads(Path(ns.session_file).read_text(encoding="utf-8")) if ns.session_file else None
    if ns.scope_class in hg.SESSION_CLASSES:
        instruction = hg.session_instruction(ns.scope_class, session or {})
    else:
        instruction = Path(ns.instruction_file).read_text(encoding="utf-8")
    return ms.assemble_unsigned_grant(
        instruction=instruction, scope_class=ns.scope_class, target=ns.target,
        ttl_seconds=ns.ttl, now_iso=time.strftime(hg._TS_FMT, time.gmtime()),
        pubkey_id=ns.pubkey_id or relay_pubkey_id(), session=session)


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="phone_mint.relay_client", description=__doc__.split("\n")[0])
    p.add_argument("--relay", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint", help="post an unsigned grant; print the link for the operator")
    m.add_argument("--grant-file", default=None, help="an already-assembled UNSIGNED grant")
    m.add_argument("--scope-class", default=None)
    m.add_argument("--target", default=None)
    m.add_argument("--instruction-file", default=None)
    m.add_argument("--session-file", default=None,
                   help="session.arm/scope.amend: the `session` from `session_auth grant-request`")
    m.add_argument("--ttl", type=int, default=600)
    m.add_argument("--pubkey-id", default=None, help="default: relay_pubkey_id from opconf")
    sub.add_parser("register", help="one-time: post a passkey enrollment request")
    pl = sub.add_parser("pull", help="ONE pull of a result (exit 3 = not approved yet)")
    pl.add_argument("--id", required=True)
    pl.add_argument("--out", required=True)
    mb = sub.add_parser("mint-batch", help="coordinator: mint ONE batch arm grant from N advisors' "
                                           "`grant-request --batch` outputs")
    mb.add_argument("lines", nargs="+", help="one JSON file per advisor")
    mb.add_argument("--pubkey-id", default=None, help="default: relay_pubkey_id from opconf")
    pb = sub.add_parser("pull-batch", help="coordinator: poll for the operator's signature until the "
                                           "grant expires, then write it under ~/.maw/grants/batch/")
    pb.add_argument("--id", required=True)
    db = sub.add_parser("deliver-batch", help="coordinator: send each line's advisor a plain notice "
                                              "naming the grant file")
    db.add_argument("--grant", required=True)
    db.add_argument("--as", dest="sender", required=True, help="the coordinator's registered name")
    g = sub.add_parser("gen-page", help="regenerate relay/page.mjs from serve.py")
    g.add_argument("--out", default=str(PAGE_MJS))
    ns = p.parse_args(argv)
    try:
        if ns.cmd == "gen-page":
            Path(ns.out).write_text(page_module_source(), encoding="utf-8")
            print(f"wrote {ns.out}")
            return 0
        if ns.cmd == "deliver-batch":
            rows = deliver_batch(Path(ns.grant), sender=ns.sender)
            for repo, ok, note in rows:
                print(f"{'sent  ' if ok else 'FAILED'} {repo}: {note}")
            return 0 if all(ok for _r, ok, _n in rows) else 1
        secret = read_secret()
        if ns.cmd == "pull-batch":
            path, left = pull_batch(ns.id, secret=secret)
            print(f"signed batch grant verified -> {path} ({left}s left); now "
                  f"`deliver-batch --grant {path} --as <coordinator>`")
            return 0
        if ns.cmd == "mint-batch":
            relay = relay_url(ns.relay)
            grant = assemble_batch(read_batch_lines([Path(f) for f in ns.lines]),
                                   now_iso=time.strftime(hg._TS_FMT, time.gmtime()),
                                   pubkey_id=ns.pubkey_id or relay_pubkey_id())
            rid, url = mint_batch(grant, relay=relay, secret=secret)
            print(json.dumps({"id": rid, "url": url, "lines": len(grant["batch"])}))
            print(f"send the operator ONE link: {url} ({len(grant['batch'])} advisors; check "
                  f"code {challenge_prefix(grant)}), then `pull-batch --id {rid}`",
                  file=sys.stderr)
            return 0
        if ns.cmd == "pull":
            res = pull(ns.id, secret=secret)
            if res is None:
                print("not approved yet — pull again later (do not poll)", file=sys.stderr)
                return PULL_PENDING
            fp = res.pop("_fingerprint", None)
            Path(ns.out).write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
            if fp:
                print(f"key fingerprint: {fp}\n  the operator: this MUST equal the fingerprint your phone "
                      f"showed. If it differs, do NOT commit the key.")
            if "webauthn" in res:
                print(f"grant signed and verified offline -> {ns.out} (relay it unchanged)")
            else:
                print(f"public key captured -> {ns.out} (pubkey_id={res['pubkey_id']}). "
                      f"COMMIT IT AS A LAYER-1 KEY ON THE OPERATOR'S TRUSTED PATH.")
            return 0
        relay = relay_url(ns.relay)
        if ns.cmd == "register":
            relay_pubkey_id()  # fail before the phone enrolls, not at pull
            rid, url = post("register", None, relay=relay, secret=secret)
        else:
            if ns.grant_file:
                grant = json.loads(Path(ns.grant_file).read_text(encoding="utf-8"))
            else:
                if not (ns.scope_class and ns.target):
                    p.error("mint needs --grant-file, or --scope-class and --target")
                grant = _assemble(ns)
            rid, url = post("mint", grant, relay=relay, secret=secret)
        print(json.dumps({"id": rid, "url": url}))
        print(f"send the operator this link: {url}  — then `pull --id {rid}` once he says it is done",
              file=sys.stderr)
        if ns.cmd == "mint":
            print(f"the page must show a challenge starting {challenge_prefix(grant)} "
                  f"(a mismatch means the grant changed on the way; a lying page can "
                  f"still fake it)", file=sys.stderr)
        return 0
    except (RelayError, ValueError, OSError) as e:
        print(f"relay: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
