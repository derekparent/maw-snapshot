"""phone-mint v2 lane 1 (#159) — the ping-with-link orchestration verb.

One verb, `request()`, chains the pieces a caller (a seat, or the CoS) needs when
The operator is away from the Mac and a grant must be authorized from his phone:

  assemble  — build the UNSIGNED grant (mint_session.assemble_unsigned_grant); the
              TTL clock starts HERE (§9.2), so the ping must go out promptly.
  serve     — front the window-only mint server (serve.MintServer, unchanged) with
              `tailscale serve` over the tailnet, and EMIT the ping text + stable
              ts.net URL for the caller. This helper NEVER pushes the ping itself —
              who pings the operator and when (and the §9.3 "Mac-mint default when present"
              routing) is the CoS's call, per the maw/CoS build boundary. It hands
              the caller the text and the URL via `on_ready`.
  teardown  — stop the `tailscale serve` front when the window closes, whether the
              ceremony completed or timed out (no lingering exposure).

What this module does NOT do, on purpose:
  - It does not weaken serve.py's disciplines. The grant is passed to MintServer
    byte-for-byte as assembled; the phone still derives its challenge from the
    displayed bytes (§9.5 display-bind) and the server still self-verifies against
    the committed key before writing (N5 window-only, zero-trust body). This verb
    only wraps assemble + front + teardown around that unchanged core.
  - It does not push notifications, choose phone-vs-Mac, or decide WHEN to ask.
    Those are CoS routing policy (the operator's CoS), not maw code.

The subprocess seams (`run`) and the server (`server_factory`) are injectable so
the assemble/ping/teardown orchestration is fully CI-testable phone-absent; only
the live `tailscale serve` transport needs the operator's environment (the HTTPS toggle).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from guards import human_grant as hg
from phone_mint import mint_session as ms
from phone_mint import serve as srv

# The macOS Tailscale CLI is NOT on PATH and runs sandboxed; `status --json` is a
# read (safe), `serve` fronts the local window server. (field note, 2026-07-20 run)
TAILSCALE_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
DEFAULT_PORT = 8799
DEFAULT_WINDOW = 300


@dataclass
class RequestResult:
    """The outcome of one ping-with-link request."""
    grant: dict            # the unsigned grant that was assembled + served
    ping: str              # the text a caller pushes to the operator (link included)
    url: str               # the stable ts.net URL the operator opens on his phone
    completed: "dict | None"  # the signed, relayable grant — None on window timeout
    teardown_error: "Exception | None" = None  # front teardown FAILED after a
    # completed ceremony (the grant is still saved) — surfaced, never swallowed


def _run(cmd: "list[str]") -> subprocess.CompletedProcess:
    """Default subprocess seam: capture output, raise on non-zero."""
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def resolve_serve_url(*, tailscale_bin: str = TAILSCALE_BIN, run=_run) -> str:
    """The stable https URL the operator opens on his phone, from `tailscale status --json`
    (Self.DNSName). Raises RuntimeError if tailscale can't report a DNS name — the
    ts.net cert path needs the node up and the admin-console HTTPS toggle on."""
    proc = run([tailscale_bin, "status", "--json"])
    try:
        dnsname = json.loads(proc.stdout)["Self"]["DNSName"].rstrip(".")
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"could not read tailnet DNS name: {e}") from e
    if not dnsname:
        raise RuntimeError("tailnet DNS name is empty (node down or HTTPS off?)")
    return f"https://{dnsname}/"


def build_ping(*, what: str, url: str, expires_in_seconds: int) -> str:
    """The operator-facing ping text: what needs authorizing, the link, and when it
    expires. `expires_in_seconds` is the operator's ACTIONABLE deadline — the sooner of the
    grant TTL and the serve window (a caller passes min(ttl, window); see
    request()), so the ping never promises time past when the link dies. Plain
    text (phone-notification friendly); no grant secrets — the grant carries
    instruction_sha256, not the instruction plaintext."""
    if not isinstance(what, str) or not what:
        raise ValueError("what must be a non-empty summary string")
    mins = max(1, -(-expires_in_seconds // 60))  # ceil to minutes, floor 1
    return (f"Authorize needed: {what}\n"
            f"{url}\n"
            f"Open on your phone (Face ID). Expires in ~{mins} min.")


def start_front(port: int, *, tailscale_bin: str = TAILSCALE_BIN, run=_run) -> None:
    """Front the local window-only server on the tailnet. New-syntax serve
    (field note): `serve --bg http://127.0.0.1:PORT`."""
    run([tailscale_bin, "serve", "--bg", f"http://127.0.0.1:{port}"])


def stop_front(*, tailscale_bin: str = TAILSCALE_BIN, run=_run) -> None:
    """Tear the tailnet front down so nothing lingers past the window. `serve
    reset` clears the serve config this ceremony added.

    TRIPWIRE: `reset` clears ALL serve config on this node, not just this
    ceremony's `--bg http://127.0.0.1:PORT`. Today the operator's node runs no other
    serve service, so this clobbers nothing (advisor-verified live, 2026-07-21).
    If another tailscale-serve service is ever added on this host, switch this
    teardown to the narrower per-target off."""
    run([tailscale_bin, "serve", "reset"])


def request(
    *,
    instruction: str,
    scope_class: str,
    target: str,
    ttl_seconds: int,
    now_iso: str,
    what: str,
    merge: "dict | None" = None,
    port: int = DEFAULT_PORT,
    window_seconds: int = DEFAULT_WINDOW,
    url: "str | None" = None,
    serve_front: bool = True,
    on_ready=None,
    run=_run,
    server_factory=srv.MintServer,
    tailscale_bin: str = TAILSCALE_BIN,
) -> RequestResult:
    """Assemble -> serve -> teardown as one verb (#159).

    Order is load-bearing: the grant is assembled (TTL starts), the ping text +
    URL are built, the tailnet front comes up, then `on_ready(ping, url)` fires so
    the caller pings the operator BEFORE `serve()` blocks the ceremony window. The front is
    always torn down (finally), completed or not. Returns RequestResult; on a
    window timeout `completed` is None.

    `on_ready` is how the caller learns the ping text in time to send it — this
    verb emits, it does not push. `run` / `server_factory` are injectable for CI.
    """
    grant = ms.assemble_unsigned_grant(
        instruction=instruction,
        scope_class=scope_class,
        target=target,
        ttl_seconds=ttl_seconds,
        now_iso=now_iso,
        merge=merge,
    )
    key = hg.load_key(grant["pubkey_id"])
    if key is None:
        raise ValueError(
            f"{hg.missing_key_reason(grant['pubkey_id'])} — commit the phone key first"
        )

    if url is None:
        url = resolve_serve_url(tailscale_bin=tailscale_bin, run=run)
    # THE OPERATOR'S ACTIONABLE deadline is the SOONER of the grant TTL and the serve
    # window: the link dies when the window closes even if the grant TTL is
    # longer. Promise only what the link will honor (advisor MAJOR on #161).
    ping = build_ping(what=what, url=url,
                      expires_in_seconds=min(ttl_seconds, window_seconds))

    if serve_front:
        start_front(port, tailscale_bin=tailscale_bin, run=run)
    try:
        server = server_factory(
            kind="mint",
            grant=grant,
            pubkey_point=key[0],
            window_seconds=window_seconds,
            port=port,
        )
        if on_ready is not None:
            on_ready(ping, url)
        completed = server.serve()
    except BaseException:
        # The ceremony errored/aborted: still tear the front down, then let the
        # original error propagate (it is the loud signal; don't mask it).
        if serve_front:
            try:
                stop_front(tailscale_bin=tailscale_bin, run=run)
            except Exception as te:  # noqa: BLE001
                # Parity with the rc-2 path (#161 advisor note): the serve error
                # propagates, but a FAILED teardown here means the front may
                # LINGER (N5) — say so loudly instead of a silent pass.
                print(f"WARNING: tailscale serve teardown ALSO failed ({te}); the "
                      f"front may still be serving — run `tailscale serve reset` "
                      f"to close it.", file=sys.stderr)
        raise

    teardown_error: "Exception | None" = None
    if serve_front:
        try:
            stop_front(tailscale_bin=tailscale_bin, run=run)
        except Exception as e:  # noqa: BLE001
            if completed is not None:
                # The operator walked Face ID — the signed grant must NOT be lost to a
                # teardown failure. Surface it (field + CLI warning), don't raise.
                teardown_error = e
            else:
                # Incomplete ceremony AND teardown failed — never swallow it.
                raise

    return RequestResult(grant=grant, ping=ping, url=url, completed=completed,
                         teardown_error=teardown_error)


# --- CLI ---------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    import argparse
    args = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        prog="phone_mint.request",
        description="ping-with-link: assemble a grant, serve it to the operator's phone, teardown",
    )
    p.add_argument("--instruction", required=True,
                   help="the literal instruction the operator is authorizing (hashed into the grant)")
    p.add_argument("--scope-class", required=True, help=f"one of: {sorted(hg.SCOPE_CLASSES)}")
    p.add_argument("--target", required=True, help="the grant target (e.g. repo/PR ref)")
    p.add_argument("--ttl", type=int, required=True, help="TTL seconds (clock starts at request)")
    p.add_argument("--what", required=True, help="human summary for the ping (no secrets)")
    p.add_argument("--out", required=True, help="write the completed (signed) grant here")
    p.add_argument("--merge-repo", help="merge.approval: repo slug")
    p.add_argument("--merge-pr", type=int, help="merge.approval: PR number")
    p.add_argument("--merge-sha", help="merge.approval: 40-char head SHA")
    p.add_argument("--url", help="override the ts.net URL (default: resolve via tailscale)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    p.add_argument("--no-front", action="store_true",
                   help="do not run `tailscale serve` (front it yourself)")
    ns = p.parse_args(args)

    merge = None
    if ns.merge_repo or ns.merge_pr is not None or ns.merge_sha:
        merge = {"repo": ns.merge_repo, "pr": ns.merge_pr, "head_sha": ns.merge_sha}

    def on_ready(ping: str, url: str) -> None:
        # ping text -> stdout (a caller/CoS captures this to push to the operator); notes -> stderr.
        print(ping)
        sys.stdout.flush()
        print(f"phone-mint: serving the ceremony window ({ns.window}s) at {url}\n"
              f"  send the ping above to the operator; the window is open now.", file=sys.stderr)

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        result = request(
            instruction=ns.instruction,
            scope_class=ns.scope_class,
            target=ns.target,
            ttl_seconds=ns.ttl,
            now_iso=now_iso,
            what=ns.what,
            merge=merge,
            port=ns.port,
            window_seconds=ns.window,
            url=ns.url,
            serve_front=not ns.no_front,
            on_ready=on_ready,
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        print(f"phone-mint request failed: {e}", file=sys.stderr)
        return 1

    if result.completed is None:
        print("no valid assertion within the window (timeout)", file=sys.stderr)
        return 1
    Path(ns.out).write_text(json.dumps(result.completed, indent=2), encoding="utf-8")
    print(f"grant signed -> {ns.out} (relay it unchanged)", file=sys.stderr)
    if result.teardown_error is not None:
        # The grant is saved; the front may still be UP. Loud + non-zero so the
        # caller closes it manually (N5 — no lingering exposure goes unnoticed).
        print(f"WARNING: tailscale serve teardown FAILED after a completed "
              f"ceremony ({result.teardown_error}); the grant is saved but the "
              f"front may still be serving — run `tailscale serve reset` to close it.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
