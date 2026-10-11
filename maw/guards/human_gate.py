"""Human-presence gate — Touch ID proof that a human is at the wheel.

This is the presence PRIMITIVE behind `guards.session_auth arm`: one fresh
authentication arms the advisor session to merge within the scope rendered in
the prompt (6h max, this session only). `session_auth` calls `gate()` once, at
arm time, with the arm scope as `action`; a passkey-grant arm (`--grant-file`)
replaces this call rather than adding to it.

History: until the operator's 2026-07-16 ruling this module was Factor 2 of a per-merge
TWO-FACTOR ceremony (echo-Y + `python3 -m guards.human_gate '<action>'` before
every `gh pr merge`). That ceremony is RETIRED: nothing runs this gate per merge
any more, and the `python3 -m guards.human_gate` CLI below is kept only as a
standalone presence probe. The merge-time checks live in
`guards.session_auth check`.

FAIL-CLOSED — the deliberate opposite of the leakguard's fail-open direction.
A presence gate whose check errors, times out, or is refused must DENY: the
absence of a *proven* human is treated as no human. ONLY rc=0 from the auth
command (a human authenticated) opens it.

Mechanism: `sudo -k` drops any cached sudo credential (so a recent unrelated
sudo can't satisfy the gate) and must SUCCEED, or freshness is unproven. Then
`sudo -n /usr/bin/true` probes non-interactively: if it succeeds with no cached
credential, sudo needs no authentication on this host (a NOPASSWD rule), so a
successful sudo proves nothing and the gate REFUSES. Only then does
`sudo -p '<prompt>' /usr/bin/true` force a FRESH authentication. With Touch ID configured in PAM (pam_tid), that surfaces
the Touch ID dialog; `/usr/bin/true` is a no-op, so the gate runs no privileged
action — it only borrows sudo's authentication. stdin is /dev/null: if pam_tid
is NOT reached and sudo falls back to a password prompt, the read hits EOF and
fails FAST instead of hanging (the Touch ID dialog is a GUI surface,
independent of stdin, so it still works). A bounded timeout is the backstop.

Known hazard (see docs): pam_tid can fail inside tmux (session context detached
from the GUI). Because this is fail-closed, that surfaces as a clean DENY, not
a hang — the operator then decides on pam_reattach or a herdr-hosted seat.

Denials are CLASSIFIED (#206 item 7) so a refused arm says which of these it
was; the class changes only the reason text and the exit code, never whether
the gate opens:
  3 declined     the sheet was offered and not completed (cancel, wrong password)
  4 timeout      the prompt was up for TIMEOUT_S and nobody answered
  5 unreachable  the sheet was never shown: sudo cannot run here (sandbox), or
                 it failed faster than FAST_FAIL_S (no GUI / no pam_tid / tmux)
  6 unprovable   a NOPASSWD host, where no sudo success proves a human
"unreachable" vs "declined" after a plain sudo failure is read from timing
(sudo prints the same password-fallback text for both), so it is a hint.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

# The auth must complete within this bound; a hung prompt is a DENY, not a wait.
TIMEOUT_S = 90

# The action text's budget in the prompt. session_auth caps its scope part
# below this so the arm line always fits whole.
PROMPT_MAX = 360

# 0 iff a human freshly authenticated; DENY (1) is the CLI usage error. Each
# denial class has its own code: EXIT_CODES below.
PASS = 0
DENY = 1

Runner = Callable[[list[str], int], "subprocess.CompletedProcess[str]"]


def _run(cmd: list[str], timeout: int) -> "subprocess.CompletedProcess[str]":
    # stdin=DEVNULL so a password fallback fails fast on EOF instead of hanging;
    # Touch ID (pam_tid) is a GUI dialog and does not read stdin.
    return subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )


@dataclass(frozen=True)
class GateResult:
    """The gate's outcome. `ok` is the ONLY thing that opens anything; `kind` and
    `detail` only say WHY it stayed shut, so an operator knows whether to touch
    the sensor again, fix the host, or accept that he said no (#206 item 7)."""

    ok: bool
    kind: str  # one of KINDS
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.kind]

    def __bool__(self) -> bool:
        return self.ok


# Exit codes, one per outcome class. 0 iff a human freshly authenticated.
# `python3 -m guards.human_gate` and `python3 -m guards.session_auth arm` both
# return these (session_auth arm uses 1 for every refusal BEFORE the gate runs).
EXIT_CODES = {
    "ok": PASS,
    # The Touch ID sheet was offered and not completed: the human cancelled,
    # declined, or failed authentication. Re-arming means asking the operator.
    "declined": 3,
    # The prompt was up for the whole bound and nobody answered.
    "timeout": 4,
    # Touch ID could not be reached at all: sudo cannot run here (sandbox, no
    # sudo), or sudo failed too fast for a human (no GUI session, pam_tid not
    # configured, tmux without pam_reattach). Retrying from the same place
    # fails the same way; fix the host or arm from a direct pane.
    "unreachable": 5,
    # sudo needs no authentication on this host (NOPASSWD), so no sudo success
    # can prove a human. Host configuration, not an operator decision.
    "unprovable": 6,
}
KINDS = tuple(EXIT_CODES)

# A sudo failure faster than this did not wait on a human: the sheet most likely
# never appeared. It only picks the WORDING; every failure still denies.
FAST_FAIL_S = 2.0

# sudo/OS text that means sudo itself could not run as root here.
_CANNOT_RUN_MARKERS = (
    "no new privileges", "nosuid", "operation not permitted",
    "effective uid is not 0", "command not found",
)
_BAD_PASSWORD_MARKERS = ("incorrect password", "sorry, try again")


def _stderr_of(cp: object) -> str:
    err = getattr(cp, "stderr", "") or ""
    return err if isinstance(err, str) else ""


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line[:200] + ("…" if len(line) > 200 else "")


def _exc_line(exc: BaseException) -> str:
    return _one_line(f"{type(exc).__name__}: {exc}")


def gate(action: str, runner: "Runner | None" = None, timeout_s: int = TIMEOUT_S) -> bool:
    """True ONLY if a human freshly authenticated; fail-closed on everything else.

    `action` names what is being gated and appears in the Touch ID prompt so the
    human sees WHAT they are authorizing (the arm line built by `session_auth._arm`, scope included).
    """
    return gate_result(action, runner=runner, timeout_s=timeout_s).ok


def gate_result(
    action: str,
    runner: "Runner | None" = None,
    timeout_s: int = TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> GateResult:
    """`gate`, plus the class of a denial. Opens on exactly what `gate` opens on.

    `runner=None` resolves to this module's `_run` at CALL time. (Before this,
    `gate`'s default was bound at import, so a test that patched
    `human_gate._run` and called `gate()` without a runner ran the REAL sudo.)
    """
    run = runner if runner is not None else _run
    # The prompt is the dialog the human FINGERPRINTS — it must be honest by
    # construction. `action` is caller-controlled, so collapse all whitespace
    # (defeats a newline that could forge a second line: "…approved. also run X")
    # and cap the length so an oversized payload can't push the real intent off
    # screen. The cap must clear the longest honest caller (the arm line with a
    # full scope) and a cut must be VISIBLE: a silent cut once rendered the operator's
    # scope as "scope: maw" under a 120-char cap.
    clean = " ".join(action.split())
    if len(clean) > PROMPT_MAX:
        clean = clean[:PROMPT_MAX - 1].rstrip() + "…"
    # sudo -p expands %u %U %h %H %p in the prompt; %% is a literal %. Escape so
    # the operator sees the text that was passed, not the account or host name.
    prompt = f"HUMAN GATE — {clean.replace('%', '%%')} — Touch ID: "
    # Drop any cached credential first so the gate can't be satisfied by an
    # earlier unrelated sudo. If the drop fails, a cached credential could
    # satisfy the auth below, so freshness is unproven: deny.
    try:
        k = run(["sudo", "-k"], timeout_s)
        why = _one_line(_stderr_of(k)) or f"exit {getattr(k, 'returncode', '?')}"
    except Exception as exc:
        k, why = None, _exc_line(exc)
    if getattr(k, "returncode", 1) != 0:
        return GateResult(False, "unreachable", (
            f"sudo cannot run here (sudo -k failed: {why}), so a fresh "
            "authentication cannot be proven and the Touch ID sheet was never "
            "shown. Typical cause: a sandboxed shell or no sudo."))
    # With the cache dropped, a NON-interactive sudo can only succeed when this
    # host requires no authentication at all (NOPASSWD). There a successful sudo
    # proves no human, so refuse rather than prompt. `-n` never reaches PAM.
    try:
        probe = run(["sudo", "-n", "/usr/bin/true"], timeout_s)
    except Exception as exc:
        return GateResult(False, "unreachable", (
            f"the non-interactive sudo probe failed to run ({_exc_line(exc)}); "
            "the Touch ID sheet was never shown."))
    if getattr(probe, "returncode", 1) == 0:
        return GateResult(False, "unprovable", (
            "sudo succeeds here with no authentication (a NOPASSWD rule), so it "
            "cannot prove a human is present. The Touch ID sheet was not shown; "
            "this is host configuration, not a refusal."))
    start = clock()
    try:
        cp = run(["sudo", "-p", prompt, "/usr/bin/true"], timeout_s)
    except subprocess.TimeoutExpired:
        return GateResult(False, "timeout", (
            f"no answer within {timeout_s}s: the Touch ID prompt timed out. If "
            "the operator never saw a sheet, treat it as unreachable."))
    except Exception as exc:
        return GateResult(False, "unreachable", (
            f"sudo failed to run ({_exc_line(exc)}); the Touch ID sheet was "
            "never shown."))
    if getattr(cp, "returncode", 1) == 0:
        return GateResult(True, "ok")
    elapsed = clock() - start
    err = _stderr_of(cp)
    low = err.lower()
    said = f" [sudo: {_one_line(err)}]" if err.strip() else ""
    if any(m in low for m in _BAD_PASSWORD_MARKERS):
        return GateResult(False, "declined", (
            f"authentication failed (a wrong password was entered).{said}"))
    if any(m in low for m in _CANNOT_RUN_MARKERS):
        return GateResult(False, "unreachable", (
            f"sudo could not run as root here; the Touch ID sheet was never "
            f"shown.{said}"))
    if elapsed < FAST_FAIL_S:
        return GateResult(False, "unreachable", (
            f"sudo failed in {elapsed:.1f}s, too fast for a human: the Touch ID "
            "sheet most likely never appeared (no GUI session, pam_tid not "
            f"configured, or tmux without pam_reattach).{said}"))
    return GateResult(False, "declined", (
        f"the Touch ID prompt was up {elapsed:.1f}s and was not completed: the "
        f"operator cancelled, declined, or authentication failed.{said}"))


def main(argv: list[str] | None = None) -> int:
    """Exit 0 iff a human freshly authenticated. Otherwise: 1 usage, 3 declined,
    4 timeout, 5 unreachable (sheet never shown), 6 unprovable (NOPASSWD host)."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].strip():
        print("usage: python3 -m guards.human_gate '<action>'", file=sys.stderr)
        return DENY
    res = gate_result(args[0])
    if res.ok:
        return PASS
    print(f"HUMAN GATE DENIED ({res.kind}) — no fresh human authentication: "
          f"{res.detail} The gated action is aborted.", file=sys.stderr)
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
