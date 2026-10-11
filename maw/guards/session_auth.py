"""session_auth — the session-scoped merge authorization (operator ruling 2026-07-16).

WHAT THE OPERATOR GRANTED, AND WHAT HE DID NOT. Read this before changing anything here.

The operator redesigned the merge posture on 2026-07-16. The ruling: one Touch ID
authorization per session is enough; there is no per-merge Touch ID; the
advisor merges in the recommended order; and the authorization may be bounded
to 6 hours.

OLD: literal echo-Y (authorizes the plan) + `guards.human_gate` Touch ID
     immediately before EACH `gh pr merge`, run by the EXECUTOR.
NEW: ONE Touch ID arms the ADVISOR SESSION; the ADVISOR merges. The executor
     does not merge at all.

NO-SELF-MERGE IS RETIRED (operator ruling 2026-07-17, trusted path — it superseded and
cancelled his own earlier same-day option-A). The ruling: once a session is
armed, which seat authored a PR does not matter; the arm authorizes the advisor
to merge in the operator's name, and the check should not add complexity beyond
that. The arm IS the authorization; PR seat-authorship is not a boundary he
wants enforced. The check that compared PR author to acting seat is
GONE, not fixed. This docstring's remaining "no-self-merge" mentions describe the
history of the module, not a live control. See [[merge-posture-session-auth]].

His grounds are sound and are recorded so nobody re-litigates them: he can merge
anything on any repo from the GitHub app with one passkey, so a per-merge
fingerprint stacked on review + branch protection was a redundant layer, not the
load-bearing one. Touch ID is also finicky, and he ends up typing his password
repeatedly — itself a regression, since a password is more shoulder-surfable
than a fingerprint.

THE ECHO IS FULLY RETIRED (operator ruling 2026-07-18, trusted path). On 2026-07-16 the operator
first NARROWED the echo — most repos went no-echo, but one whose merges deploy
to real users kept a per-batch echo-Y. On 2026-07-18 he removed even that:
every repo behaves the same.

So EVERY classified repo is now no-echo: the arming fingerprint is the whole
gate, and a real-users repo's blast radius is governed by the same
armed-session posture (scope + session/6h expiry + signed verdict + branch
protection), not a per-batch checkpoint. ECHO_REQUIRED_REPOS is now empty. The
control on that blast radius is the operator's tripwire: the first real user
re-opens the decision. Re-adding a repo to ECHO_REQUIRED_REPOS is a one-line
auditable scope change if that day comes.

The DEFAULT for an unlisted slug is no-echo too, since the operator's 2026-07-24
ruling that every pair he launches must behave the same retired the
unclassified-slug typo guard — see DEFAULT_REQUIRES_ECHO below.

THE RESIDUAL, RECORDED WITHOUT SPIN. On NO_ECHO_REPOS there is now NO human
between the executor writing code and it reaching main, for up to 6h. The only
human act is the arming fingerprint. What still bounds it:
  * branch protection (PR-only, enforce_admins) — but it now means only "it was
    a PR", NOT "the operator saw it". It is doing far more load-bearing work than before.
  * the advisor's signed verdict — on a no-echo repo this is THE ENTIRE GATE.
  * the session+6h expiry, and the audit trail below.
The playbook's median-model principle cuts hardest here: the seat can be swapped
for a weaker model mid-arc without notice and the grant does not shrink. That is
not an argument against the operator's ruling — he made it deliberately, and narrowed it
himself when the cost was named. It is what the next maintainer must be told.

THE REVIEWER MERGES; IT STILL NEVER GRANTS AUTHORITY. That half of the
escalation floor is untouched and matters MORE now, not less. Nothing in this
module lets a seat widen its own scope or anyone else's, and nothing here
weakens branch protection — which the whole design leans on harder than it did
before.

FAIL-CLOSED THROUGHOUT, the deliberate opposite of the fail-open leakguard.
Missing token, corrupt JSON, unreadable file, absent fields, dead session,
recycled pid, expired cap, backward clock, unreviewed PR, a verdict that does not
match the code being merged, unreachable `gh`, an ungranted seat, or a missing
echo on an echo-required repo — every one of them REFUSES. The
absence of a proven authorization is treated as no authorization.

SCOPE OF THAT CLAIM — read it before you trust it. "Fail-closed" describes the
`arm`/`check` PATHS, not the SYSTEM. The token is a FILE, and the seat runs as
The operator: an agent that hand-writes `.maw/.runtime/session-auth.json` never meets a
fingerprint at all. There is no fix — you cannot protect a file from a process
that can write it — and pretending otherwise is the same overclaim that made
`--signed-verdict` look like a gate.

So state the ceiling plainly: THIS WHOLE MODULE IS A CONFUSION GATE. It stops a
seat that is muddled, mistaken, or over-eager. It does not stop a seat that
sets out to defeat it. The only env-independent guarantee beneath it is branch
protection — and an agent holding the operator's `gh` token can defeat that via the API
too. That is the recorded v0.8 credential-isolation ceiling (CLAUDE.md, "Residual
(v0.8 ceiling), ENLARGED"); it predates this module and this module does not
close it. Every control here is worth having against the threat that actually
occurs — confusion — and none of it is a substitute for credential isolation.

DERIVE, DO NOT TRUST — the rule that makes the sentence above true. Every fact
this gate decides on is derived from a source the acting agent does not control:

  * the PR's AUTHOR and HEAD SHA come from `gh`, not from a flag. A first cut of
    this module took `--pr-author` from the caller, which made no-self-merge a
    convention an agent defeats by typing `--pr-author executor`. (`gh` is
    resolved via PATH — ground truth against a CONFUSED seat, not against a
    determined one. See gh_pr_facts.)
  * WHICH SEATS MAY MERGE AT ALL is a constant here (MERGE_AUTHORIZED_SEATS), not
    an emergent consequence of author != seat. Bounding merging ONLY by "not your
    own PR" silently authorized the executor to merge advisor PRs — the exact
    arrangement the operator retired — and let a watcher seat merge anything.
  * the ACTING SEAT comes from the TOKEN, recorded at `arm` — the one moment the operator
    is physically present. A `--acting-seat` flag has the identical hole: claim
    to be the other seat and the self-merge check evaporates.
  * the SIGNED VERDICT is an ARTIFACT that `check` reads, not a `--signed-verdict`
    boolean the advisor asserts about itself. On a no-echo repo the verdict IS
    the entire gate; a gate whose subject reports its own compliance is
    telemetry, not enforcement (playbook gate #7).

HONEST LIMIT ON THE VERDICT ARTIFACT — do not oversell it. The advisor writes it,
so it is a RECEIPT, not a lock: a seat that never read the diff can still write
one. What it buys is (a) an explicit, timestamped, auditable act, and (b) a HARD
BINDING TO THE REVIEWED SHA, so a verdict cannot follow the code as it changes
underneath it. That second property is the real one, and it is not forgeable by
accident — the SHA comes from `gh`. Confusion is the threat model here, not
malice; this stops a confused seat, not a determined one.

PHONE ARM + AMEND (operator ruling 2026-09-23, DECISIONS.md refit ruling 3): "A passkey
(Face ID) grant may ARM a session and AMEND its scope. Caps: 6h max, one repo,
logged." `arm --grant-file` takes a `session.arm` grant in place of the Touch ID
prompt; `amend --grant-file` replaces the live token's scope with a `scope.amend`
grant. The grant replaces the FINGERPRINT, never a bound: the seat check,
seatcheck, the unbounded-scope refusal, the 6h cap and the session/pid bind all
still apply, and the grant's repo/seat/session/scope must equal what this module
derives (_session_grant_ceremony). An amend cannot move the repo, the seat, the
session or the expiry. Every refused grant is audited (`arm-refused` /
`amend-refused`). `grant-request` prints the unsigned half for the mint.

VERDICT LIFETIME — verdicts SURVIVE SESSIONS, tokens do not (#103). The verdict
is a repo-runtime artifact (`.maw/.runtime/verdicts.jsonl`) bound to the reviewed
SHA, NOT to the arming session or its token. So a verdict recorded in one session
remains valid after that session ends, and a NEW arm may merge on it — this is BY
DESIGN, and it is safe because the SHA binding still holds (a push moves the head
SHA, sha-drift refuses, the PR must be re-reviewed). What expires is the AUTHORITY
TO MERGE (the session token + 6h cap), not the REVIEW. Observed live (a sibling
repo, 2026-07-18): a PR sat approved from an expired session and the next session
merged it correctly under its own arm. Do not "expire" verdicts to match the
token — that would force needless re-reviews and conflates the review with the
merge authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import opconf  # noqa: E402
from guards.human_gate import GateResult  # noqa: E402
from guards.human_gate import gate_result as human_gate  # noqa: E402
from net import registry  # noqa: E402
from net import seatcheck  # noqa: E402

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

PASS = 0
DENY = 1


class ArmRefusal(str):
    """A refusal message that also carries its CLI exit code. It IS the message
    (a str), so arm()'s (ok, msg) contract and every caller stay as they were;
    main() reads `.exit_code` to exit with the Touch ID class (#206 item 7)."""

    exit_code: int

    def __new__(cls, text: str, exit_code: int = DENY) -> "ArmRefusal":
        obj = super().__new__(cls, text)
        obj.exit_code = exit_code
        return obj


TOKEN_REL = ".maw/.runtime/session-auth.json"
AUDIT_REL = ".maw/.runtime/session-auth-audit.jsonl"
ECHO_REL = ".maw/.runtime/merge-echoes.jsonl"
VERDICT_REL = ".maw/.runtime/verdicts.jsonl"

# The 6h cap. The operator offered it ("or it could be valid for 6hrs i can accept that as
# well") and it is taken IN ADDITION to the session bind, not instead of it.
#
# DO NOT RAISE THIS because it feels inconvenient. Its PRIMARY purpose is not
# security — it is COMPREHENSION, and that is the operator's own stated reason
# (2026-07-16):
#
#   "if he has a 6hr permissions slip to merge. This prevents him from working a
#    full 24hrs and me losing track of everything and forces me to reengage and
#    catch up."
#
# The cap is a FORCING FUNCTION that pulls the human back into the loop before
# the delta grows past what he can review. A 24h token would still be perfectly
# "secure" by every bound in this module and would defeat the actual point: the operator
# would return to a day of merged work he never saw. The number is bounded by
# how much change a person can catch up on, not by how long a key should live.
#
# It ALSO closes a security hole the session bind cannot — they are different
# holes:
#   * the session_id+pid bind kills the token the instant the operator closes the pane; a
#     pure timer would leave a dead session's token live for the remainder.
#   * the 6h cap kills a token whose session is left parked (an advisor running
#     overnight); a pure session bind would let it live for the session's whole
#     lifetime — days. That is the standing blank check the echo rule was
#     written against, just slower.
# Neither costs the operator anything in normal use: a real arc is well under 6h, and
# re-arming is one touch.
MAX_TOKEN_AGE_H = 6

# WHO THE OPERATOR GRANTED MERGE AUTHORITY TO. The 2026-07-16 ruling: the advisor merges
# in the recommended order, and the executor does not merge at all. The grant
# is to the ADVISOR SEAT — this constant is that ruling expressed as a REFUSAL
# rather than as prose, because a reader should learn the boundary from the code
# denying them, not from a docstring they skimmed.
#
# Without it, `arm` bounded merging only by author != seat, which let in two
# arrangements the operator never granted:
#   * the EXECUTOR arms and merges ADVISOR-authored PRs — author('advisor') !=
#     seat('executor') passes. That is the arrangement the operator RETIRED, resurrected
#     through the new mechanism.
#   * a WATCHER arms and merges anything. `_seat()` falls back to MAW_NET_NAME,
#     the `cos` alias sets MAW_NET_NAME=cos with no MAW_BRIDGE_ROLE, and 'cos' is
#     not in seatcheck.ROLES — so a cos seat skipped the seat check entirely and
#     then author != 'cos' passed for EVERY PR in the repo. The seat whose whole
#     defining property is that it does not build could have merged everything.
#     (role='cos' is not hypothetical: WATCHER_ROLES shipped in #80.)
#
# The Touch ID prompt does render seat=, so the operator could in principle catch both by
# reading it. That is a BACKSTOP, not the gate: a control that depends on a human
# noticing the wrong word in a prompt he sees dozens of times is the
# "learns to click through prompts" failure this module refuses elsewhere.
#
# Widening this is a scope-of-authority change — Layer-1 floor. It requires the operator on
# the trusted path, never a peer's say-so, and never an agent's own reasoning
# about what would be convenient.
MERGE_AUTHORIZED_SEATS = ("advisor",)

# The seats whose recorded verdict can authorize a merge. Checked when a verdict
# is RECORDED and again when it is CONSUMED, so a verdict line written by any
# other seat (or by hand) authorizes nothing. Same widening rule as above.
REVIEWER_SEATS = ("advisor",)

# These are REPO SLUGS (the git repo basename).
#
# NO_ECHO_REPOS: pre-auth is sufficient; the advisor merges its signed PRs with
# no per-batch operator checkpoint. The operator RULING 2026-07-18 (trusted path) folded
# the last echo repo in: every repo behaves the same. So the arming fingerprint is
# now the whole gate on EVERY classified repo; a real-users repo's blast radius is
# governed by the same armed-session posture, not a per-batch echo. The operator's
# tripwire (the first real user re-opens the decision) is that repo's control.
#
# Which slugs the operator classified is HIS record, not maw's: it lives in
# `no_echo_repos` in ~/.maw/operator.toml (env MAW_NO_ECHO_REPOS), read once at
# import. Since the 2026-07-24 no-echo-default ruling (see DEFAULT_REQUIRES_ECHO
# below) the list is no longer load-bearing: an unlisted slug gets the same
# no-echo answer, so a missing or empty list changes no decision. Classifications
# are the operator's trusted-path act; a seat never edits the deployed pin for one.
NO_ECHO_REPOS: tuple[str, ...] = opconf.get_list("no_echo_repos")
# ECHO_REQUIRED_REPOS: empty as of the 2026-07-18 ruling. Kept as a constant (not
# deleted) so re-adding a repo here is a one-line, auditable scope change if a
# real user ever re-opens the decision. It stays in CODE, not the operator file:
# it is the stricter direction, and a missing file must never drop a checkpoint.
ECHO_REQUIRED_REPOS: tuple[str, ...] = ()

# THE OPERATOR RULING 2026-07-24 (trusted path, live in the maw main session): the
# per-slug typo guard is retired — every pair the operator launches must behave
# the same. It had fired on every brand-new repo pair (07-23, 07-24) and its
# only power was choosing
# echo-vs-no-echo: a mis-resolved slug gains no merge authority either way —
# same armed token, same signed verdict, same branch-protected repo. So the
# default is now no-echo for EVERY repo; ECHO_REQUIRED_REPOS above is the
# auditable opt-back-in if a repo ever needs the stricter per-batch checkpoint
# again (e.g. the operator's first-real-user tripwire).
DEFAULT_REQUIRES_ECHO = False


def _run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _now_s() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> float | None:
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return dt.timestamp()


def requires_echo(repo: str) -> bool:
    """Does `repo` still need a recorded the operator echo on top of the session token?

    The operator's carve-out, in code. Since the 2026-07-24 ruling the default is
    no-echo; only an ECHO_REQUIRED_REPOS entry demands the checkpoint.
    """
    slug = (repo or "").strip()
    if slug in ECHO_REQUIRED_REPOS:
        return True
    if slug in NO_ECHO_REPOS:
        return False
    return DEFAULT_REQUIRES_ECHO


def _runtime_path(repo_root: Path, rel: str) -> Path:
    return repo_root / rel


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (OSError, TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class Refusal:
    """A refusal that NAMES which bound tripped.

    An opaque refusal wastes the operator's time diagnosing it, and a gate that
    refuses silently cannot be proven to have worked. Every refusal says what was
    OBSERVED — not what it was guessed to mean.
    """

    bound: str
    detail: str

    def line(self) -> str:
        return f"MERGE AUTH DENIED [{self.bound}] — {self.detail}"


def audit(repo_root: Path, record: dict) -> None:
    """Append one audit record. Pass AND refusal both land here.

    Never raises: an audit failure must not crash a gate, but it also must not
    silently pass — callers decide, and the write is best-effort by design since
    the gate's verdict is the security property, not the log.
    """
    path = _runtime_path(repo_root, AUDIT_REL)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _session_id() -> str:
    """This session's identity. Env-provided; absent -> empty (arm refuses)."""
    return os.environ.get("MAW_SESSION_ID", "").strip()


def _seat() -> str:
    """This seat's CLAIMED role, from the launch env.

    A CLAIM, not proof — env is agent-reachable, so this value alone attests to
    nothing. It is made trustworthy at `arm` by two things it cannot fake:
      * `net.seatcheck` refuses a launch-env/seat contradiction (the observed %5
        raw-pane-paste class — this is not a hypothetical vector in this system);
      * the seat is rendered INTO the Touch ID prompt, so the operator's fingerprint
        attests to WHO is merging, not merely to what.
    Without those two, deriving the seat from env and calling it "derived" would
    just relocate the caller-supplied hole one layer down.
    """
    return (
        os.environ.get("MAW_BRIDGE_ROLE", "")
        or os.environ.get("MAW_NET_NAME", "")
    ).strip()


def _net_name() -> str:
    """This seat's switchboard registry KEY (the name it registered under).

    The registry is keyed by net NAME, which for a gated seat equals its role
    ('advisor'/'executor'). MAW_NET_NAME is the launcher value; fall back to
    MAW_BRIDGE_ROLE so a seat that set only the role still resolves.
    """
    return (
        os.environ.get("MAW_NET_NAME", "")
        or os.environ.get("MAW_BRIDGE_ROLE", "")
    ).strip()


def _registry_identity(
    net_name: str, runner: Runner = _run
) -> tuple[tuple[str, int] | None, str]:
    """Resolve THIS seat's STABLE (session_id, pid) from its switchboard entry.

    This is the fix for the pid self-compare (#85). `arm` and `check` are
    SEPARATE `python3 -m` invocations; `os.getpid()` gives each a different,
    transient pid that can never match, so `check` could never PASS. The
    switchboard registry records ONE stable identity at seat launch — the
    claude-ancestor pid and the session_id — and both invocations read the SAME
    entry, so they agree.

    The real bind is `registry.verify_caller_is`: it walks THIS caller's own
    process tree up to the registered claude pid. A foreign process cannot
    borrow the token because its lineage does not reach that pid. That lineage
    check is what `os.getpid() == os.getpid()` was trying, and failing, to be.

    Returns ((session_id, pid), "") on success, (None, error) otherwise. Every
    failure path makes the caller REFUSE (fail-closed): no verifiable seat
    identity, no token. Env-reachable like the rest of the module — a confusion
    gate, not a malice gate (see the module docstring).
    """
    if not net_name:
        return None, (
            "no seat identity (MAW_NET_NAME / MAW_BRIDGE_ROLE) — cannot locate "
            "this seat's switchboard entry to bind the token to"
        )
    try:
        entry = registry.load_entry(net_name)
    except OSError as e:
        return None, f"cannot read switchboard entry for {net_name!r}: {e}"
    if entry is None:
        return None, (
            f"no switchboard entry for seat {net_name!r} — the seat must be "
            "registered (SessionStart auto-registers) before it can arm or merge"
        )
    if not registry.verify_caller_is(entry, runner=runner):
        return None, (
            f"caller lineage does not reach seat {net_name!r}'s session "
            f"(registered pid {entry.pid}) — this process is not in that "
            "session's tree; refusing"
        )
    sid = (entry.session_id or "").strip()
    if not sid:
        return None, f"switchboard entry for {net_name!r} records no session_id"
    return (sid, entry.pid), ""


def _git_out(runner: Runner, *args: str) -> str:
    try:
        cp = runner(["git", *args])
    except (OSError, subprocess.SubprocessError):
        return ""
    out = (getattr(cp, "stdout", "") or "").strip()
    return out if getattr(cp, "returncode", 1) == 0 else ""


def default_repo_root(runner: Runner = _run) -> Path:
    """The ONE runtime root a CLI call uses when --repo-root is not given (T5).

    It used to be the cwd, so the root a token, verdict or audit line landed in
    depended on where the shell happened to be: one run armed at
    ~/Projects/maw while `peer-status` and `check` read the advisor's worktree,
    and a stale token sat in the other tree. Resolution, first hit wins:

      1. this seat's own switchboard entry (repo_root), only when the caller's
         lineage reaches it AND the cwd is in the SAME repository (same git
         common dir): another worktree or a subdirectory of the seat's repo
         resolves to the seat's tree, the root `peer_status` reads;
      2. the git toplevel of the cwd. A cwd in a DIFFERENT repo lands here,
         never on the seat's repo: gh targeting is derived from this root, so
         a seat cd'd into repo B must refuse (no token) rather than act on its
         own repo A's PR of the same number;
      3. the cwd (not a git repo: a test or a scratch dir).
    """
    top = _git_out(runner, "rev-parse", "--show-toplevel")
    name = _net_name()
    if name and top:
        try:
            entry = registry.load_entry(name)
        except OSError:
            entry = None
        here = _git_out(runner, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if (entry is not None and entry.repo_root and entry.repo_common_dir and here
                and Path(here).resolve() == Path(entry.repo_common_dir).resolve()
                and registry.verify_caller_is(entry, runner=runner)):
            return Path(entry.repo_root)
    return Path(top) if top else Path.cwd()


def _reviewer_identity(
    seat: str | None, runner: Runner = _run
) -> tuple[str, str, str]:
    """(seat, session_id, error) for the seat recording a verdict.

    With no injected `seat` (the CLI path) the role is DERIVED: the caller's
    lineage must reach its registered switchboard entry, and the role is the one
    that entry records, never the env's claim. An injected `seat` is the test
    seam; it still has to be a reviewer seat. Any failure is an error.
    """
    if seat is not None:
        the_seat, sid = seat.strip(), ""
    else:
        net_name = _net_name()
        ident, why = _registry_identity(net_name, runner=runner)
        if ident is None:
            return "", "", why
        sid = ident[0]
        try:
            entry = registry.load_entry(net_name)
        except OSError as e:
            return "", "", f"cannot read switchboard entry for {net_name!r}: {e}"
        if entry is None:
            return "", "", f"no switchboard entry for seat {net_name!r}"
        the_seat = (entry.role or "").strip()
    if the_seat not in REVIEWER_SEATS:
        return "", "", (
            f"seat {the_seat or '<none>'!r} is not a reviewer seat "
            f"({list(REVIEWER_SEATS)}) — only a reviewer's verdict can authorize "
            f"a merge")
    return the_seat, sid, ""


def _reviewer_refusal(verdict: dict, subject: str) -> "Refusal | None":
    """Consume-side check: the verdict line must name a reviewer seat."""
    seat = str(verdict.get("seat", "")).strip()
    if seat in REVIEWER_SEATS:
        return None
    return Refusal(
        "verdict-not-reviewer",
        f"{subject}'s verdict was recorded by seat {seat or '<none>'!r}, not a "
        f"reviewer seat ({list(REVIEWER_SEATS)}) — it authorizes nothing")


@dataclass(frozen=True)
class PRFacts:
    """Ground truth about a PR, from `gh` — NOT from the caller."""

    author: str
    head_sha: str
    base_ref: str = ""   # the branch the PR merges INTO (a stacked PR's is not main)
    head_ref: str = ""   # the PR's own branch (for worktree-safe cleanup)
    title: str = ""      # echoed in the verdict/merge confirmation to catch a wrong-PR


def _gh_repo_args(gh_repo: str) -> list[str]:
    """`--repo owner/name` for a gh call, so it queries the repo the caller's
    --repo-root names rather than whatever the working directory resolves to."""
    return ["--repo", gh_repo] if gh_repo else []


def gh_pr_facts(pr: int, runner: Runner = _run,
                gh_repo: str = "") -> tuple[PRFacts | None, str]:
    """(facts, error). `gh` is ground truth AGAINST A CONFUSED SEAT.

    Deliberately qualified. `gh` is resolved through PATH, so a DETERMINED agent
    could shim it — the playbook names this exact vector (gate #7: "a PATH-shim
    is bypassed by an absolute path"). Confusion is this module's threat model,
    and against confusion this is real enforcement: a muddled seat does not
    rewrite its own PATH. Against malice it is not, and the sentence must not
    promise otherwise — an unqualified "cannot forge" is the same overclaim that
    made `--signed-verdict` look like a gate. See the receipt-not-a-lock note
    above; same honesty, same reason.

    Any failure returns an error and the caller REFUSES: an unverifiable PR is
    not a mergeable PR. This deliberately reuses the same `gh pr view` shape as
    `review.cli.resolve_ref_sha` rather than inventing a second lookup.
    """
    try:
        cp = runner(
            ["gh", "pr", "view", str(pr), "--json",
             "author,headRefOid,baseRefName,headRefName,title",
             *_gh_repo_args(gh_repo)]
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"gh unreachable: {e}"
    if getattr(cp, "returncode", 1) != 0 or not cp.stdout.strip():
        return None, f"gh pr view {pr} failed: {(cp.stderr or '').strip()}"
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return None, f"gh returned unparseable JSON: {e}"
    if not isinstance(data, dict):
        return None, "gh returned a non-object"
    author = ((data.get("author") or {}) if isinstance(data.get("author"), dict)
              else {}).get("login", "")
    sha = data.get("headRefOid", "")
    if not isinstance(author, str) or not author.strip():
        return None, f"gh did not report an author for PR {pr}"
    if not isinstance(sha, str) or not sha.strip():
        return None, f"gh did not report a head SHA for PR {pr}"
    base_ref = data.get("baseRefName", "") if isinstance(data.get("baseRefName"), str) else ""
    head_ref = data.get("headRefName", "") if isinstance(data.get("headRefName"), str) else ""
    title = data.get("title", "") if isinstance(data.get("title"), str) else ""
    return PRFacts(
        author=author.strip(), head_sha=sha.strip(),
        base_ref=base_ref.strip(), head_ref=head_ref.strip(), title=title.strip(),
    ), ""


def record_verdict(
    repo_root: Path,
    pr: int,
    verdict: str,
    seat: str | None = None,
    runner: Runner = _run,
    now_s: float | None = None,
    expect_sha: str | None = None,
) -> tuple[bool, str]:
    """The advisor writes its verdict, BOUND TO THE SHA IT REVIEWED.

    The SHA is derived from `gh`, not accepted from the caller — that binding is
    the property that makes the artifact worth anything: a verdict on SHA A must
    never authorize merging SHA B (the executor pushes; the verdict must not
    follow). See the docstring's honest limit: this is a receipt, not a lock.

    `expect_sha` (#185 defect 2) binds the advisor's INTENT: a mistyped/mis-
    assumed PR NUMBER otherwise records a valid-looking verdict for the WRONG PR
    (the live incident this run — a verdict ran against a number the advisor never
    reviewed). When given, the PR's live head must match the SHA the advisor
    actually reviewed, or the record is REFUSED. The confirmation echoes
    title+base+head so a wrong-PR is visible even without --expect-sha.
    """
    now = now_s if now_s is not None else _now_s()
    gh_repo = _repo_identity(repo_root, runner)
    if not gh_repo:
        return False, ("cannot record verdict: no readable origin remote, so the PR "
                       "cannot be tied to this repo")
    facts, err = gh_pr_facts(pr, runner, gh_repo=gh_repo)
    if facts is None:
        return False, f"cannot record verdict: {err}"
    if expect_sha and not _sha_matches(expect_sha, facts.head_sha):
        return False, (
            f"cannot record verdict: PR #{pr} ({facts.title!r}) live head "
            f"{facts.head_sha[:7]} != --expect-sha {expect_sha.strip()[:12]} — refusing "
            f"to record a verdict for a PR whose head is not the code you reviewed "
            f"(wrong PR number, or the head moved after you reviewed)")
    the_seat, sid, why = _reviewer_identity(seat, runner)
    if not the_seat:
        return False, f"cannot record verdict: {why}"
    rec = {
        "ts": _iso(now),
        "pr": int(pr),
        "repo": gh_repo,
        "sha": facts.head_sha,
        "verdict": verdict,
        "seat": the_seat,
    }
    if sid:
        rec["session_id"] = sid
    path = _runtime_path(repo_root, VERDICT_REL)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    base_note = f", base {facts.base_ref}" if facts.base_ref else ""
    title_note = f" ({facts.title!r}{base_note})" if facts.title else base_note
    return True, f"recorded {verdict} for PR #{pr}{title_note} @ {facts.head_sha[:7]} by {the_seat}"


def load_verdict(repo_root: Path, pr: int) -> dict | None:
    """The LATEST recorded verdict for `pr`, or None. Torn lines are not verdicts."""
    path = _runtime_path(repo_root, VERDICT_REL)
    if not path.is_file():
        return None
    latest: dict | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a torn line is not an authorization
        if isinstance(rec, dict) and rec.get("pr") == int(pr):
            latest = rec
    return latest


def list_verdicts(repo_root: Path, pr: int | None = None) -> list[dict]:
    """Recorded verdicts (optionally filtered to one PR), oldest-first.

    The read counterpart to record_verdict (#146): #140's `status` closed the
    read loop for TOKENS, not verdicts — this exposes the verdict artifact so a
    close-out doesn't have to re-derive it from merge-wrapper output. Torn/
    non-dict lines are skipped (a torn line is not a verdict)."""
    path = _runtime_path(repo_root, VERDICT_REL)
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if pr is not None and rec.get("pr") != int(pr):
            continue
        out.append(rec)
    return out


def arm(
    scope: str,
    repo_root: Path,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    gate_fn: "Callable[[str], bool | GateResult]" = human_gate,
    seat: str | None = None,
    env: dict[str, str] | None = None,
    runner: Runner = _run,
    grant: "dict | None" = None,
    prs: "list[int] | None" = None,
) -> tuple[bool, str]:
    """Arm THIS session, then audit a refused GRANT arm (gate 8). See _arm."""
    ok, msg = _arm(scope, repo_root, session_id=session_id, pid=pid, now_s=now_s,
                   gate_fn=gate_fn, seat=seat, env=env, runner=runner, grant=grant,
                   prs=prs)
    if not ok and grant is not None:
        batch = isinstance(grant, dict) and grant.get("scope_class") == _batch_class()
        audit(repo_root, {"ts": _iso(now_s if now_s is not None else _now_s()),
                          "event": "arm-refused", "via": "grant-batch" if batch else "grant",
                          "grant_id": _grant_id(grant), "reason": msg})
    return ok, msg


def _batch_class() -> str:
    from guards import human_grant as hg
    return hg.BATCH_CLASS


def _grant_id(grant: object) -> str:
    return str(grant.get("grant_id", "")) if isinstance(grant, dict) else ""


def _session_grant_ceremony(
    grant: object, scope_class: str, facts: dict, now: float,
) -> "tuple[str, str]":
    """Verify a phone (passkey) grant as the arm/amend ceremony (operator ruling
    2026-09-23, refit ruling 3). Returns (grant_id, "") on success, after the
    single-use claim, else ("", why).

    DERIVE, DO NOT TRUST: `facts` are what THIS module derived (repo slug from the
    origin remote, the seat, the registry session id, the scope on the command
    line, the live token's scope for an amend). The grant's `session` object must
    equal them field by field, AND its signature must cover the instruction
    rendered from them, so a grant minted for another repo, seat, session or scope
    refuses twice over. The committed key, TTL, allow-list and single use are
    human_grant's checks, unchanged.
    """
    from guards import human_grant as hg

    if not isinstance(grant, dict):
        return "", "grant is not a JSON object"
    if grant.get("scope_class") != scope_class:
        return "", (f"grant scope_class is {grant.get('scope_class')!r}, this "
                    f"ceremony needs {scope_class!r}")
    alarm = hg.keyset_alarm(record=True)
    if alarm is not None:
        return "", alarm
    session = grant.get("session")
    if not isinstance(session, dict):
        return "", f"{scope_class} grant carries no session object"
    wrong = sorted(k for k in set(facts) | set(session) if session.get(k) != facts.get(k))
    if wrong:
        return "", "grant does not match this session: " + "; ".join(
            f"{k} grant={session.get(k)!r} derived={facts.get(k)!r}" for k in wrong)
    instruction = hg.session_instruction(scope_class, facts)
    if "typed" in grant:
        # Operator ruling 2026-09-23 (refit ruling 4): a TYPED approval may amend a scope
        # within the same repo; session.arm stays passkey only (class_refusal).
        from guards import typed_approval as ta
        res = ta.verify(grant, instruction, _iso(now), consumer=_net_name() or facts.get("seat"),
                        ceremony=True)
    else:
        res = hg.verify_presented(grant, instruction, _iso(now), ceremony=True)
    if not res.ok:
        return "", f"grant refused: {res.reason}"
    gid = str(grant["grant_id"])
    if not hg.consume(gid, str(grant["nonce"])):
        return "", "grant already consumed (replay) — refused"
    return gid, ""


# The advisor's run-goal artifact (batch-arm design R4): its own <= 3-bullet
# summary of the goal it accepted, written at goal-confirm. Per WORKTREE (the
# executor has its own) and keyed by the registry session id, so a restart
# invalidates it. Same-uid ceiling as the token beside it.
# maw's own module launcher: drops the cwd from sys.path on any Python, so a
# target repo with a top-level repin/ or net/ can never stand in for maw's (#324).
MAW_RUN = str(Path(__file__).resolve().parents[1] / "bin" / "maw-run")

RUN_GOAL_REL = ".maw/.runtime/run-goal-{sid}.json"
# A batch request record older than this at mint time is stale (R5).
REQUEST_MAX_AGE_S = 1800


def _run_goal_path(repo_root: Path, sid: str) -> Path:
    return _runtime_path(repo_root, RUN_GOAL_REL.format(sid=sid))


def _request_path(repo: str, seat: str, sid: str) -> Path:
    """This seat's batch request record, keyed by (repo, seat, session id).

    #338: keyed by session id alone, two seats that shared an id overwrote
    each other's record and the overwritten seat's arm failed closed. The
    readable prefix is the session id (so `ls` still finds a seat); the
    suffix is a digest of all three, so no other seat's request can land on
    this path even if an id repeats."""
    from guards import human_grant as hg
    key = hashlib.sha256(json.dumps([repo, seat, sid]).encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:80] or "_"
    return hg.grants_dir() / "requests" / f"{safe}.{key}.json"


def _legacy_request_path(sid: str) -> Path:
    """The pre-#338 record path (`requests/<session id>.json`). Read only as a
    fallback during rollout; a record there counts only if its line is this
    seat's own, exactly as for the keyed path."""
    from guards import human_grant as hg
    return hg.grants_dir() / "requests" / f"{sid}.json"


def _goal_problem(goal: object) -> str:
    """'' when `goal` is a well-formed bullet list (the batch line rule), else why."""
    from guards import human_grant as hg
    probe = {"repo": "r", "seat": "advisor", "session_id": "s", "scope": "probe", "goal": goal}
    return (hg.batch_line_error(probe, "run goal") or "")


def record_goal(repo_root: Path, bullets: "list[str]", runner: Runner = _run,
                session_id: str | None = None) -> tuple[bool, str]:
    """Write THIS seat's run-goal artifact (advisor, at goal-confirm). Tests
    inject `session_id`; a seat derives it from its registry entry."""
    sid = session_id
    if sid is None:
        ident, why = _registry_identity(_net_name(), runner=runner)
        if ident is None:
            return False, f"refusing to record the run goal: {why}"
        sid = ident[0]
    goal = [" ".join(b.split()) for b in bullets if b.strip()]
    problem = _goal_problem(goal)
    if problem:
        return False, f"refusing to record the run goal: {problem}"
    path = _run_goal_path(repo_root, sid)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"session_id": sid, "goal": goal}) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return True, f"run goal recorded for session {sid} ({len(goal)} bullet(s)): {path}"


def _read_goal(repo_root: Path, sid: str) -> "tuple[list | None, str]":
    path = _run_goal_path(repo_root, sid)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, (f"no run-goal artifact for session {sid} ({e}); record it with "
                      f"`session_auth goal-record --file <bullets>`")
    if not isinstance(data, dict) or data.get("session_id") != sid:
        return None, f"run-goal artifact at {path} is not this session's"
    problem = _goal_problem(data.get("goal"))
    if problem:
        return None, f"run-goal artifact is malformed: {problem}"
    return data["goal"], ""


def _batch_facts(scope: str, repo_root: Path, sid: str, seat: str,
                 prs: "list[int] | None", runner: Runner) -> "tuple[dict | None, str]":
    """The line THIS seat would sign for, derived exactly as arm derives it."""
    repo = _repo_identity(repo_root, runner)
    if not repo:
        return None, "no readable origin remote (a session grant binds owner/name)"
    goal, why = _read_goal(repo_root, sid)
    if goal is None:
        return None, why
    facts = {"repo": repo, "seat": seat, "session_id": sid,
             "scope": " ".join((scope or "").split()), "goal": goal}
    if prs is not None:
        facts["prs"] = _prs_text(prs)
    return facts, ""


def batch_request(scope: str, repo_root: Path, runner: Runner = _run,
                  prs: "list[int] | None" = None, now_s: float | None = None,
                  session_id: str | None = None,
                  seat: str | None = None) -> "tuple[dict | None, str]":
    """The advisor's half of a batch arm: print the line, record that it asked.

    The record (R5) is a CONFUSION gate, not a boundary: every seat runs as one
    uid, so a malicious CoS could write it. It stops an honest seat from arming
    on a line it never asked for (a stale or misdirected grant)."""
    from guards import human_grant as hg
    clean = " ".join((scope or "").split())
    problem = _scope_problem(clean)
    if problem:
        return None, problem
    sid = session_id
    if sid is None:
        ident, why = _registry_identity(_net_name(), runner=runner)
        if ident is None:
            return None, why
        sid = ident[0]
    the_seat = (seat if seat is not None else _seat()).strip()
    if the_seat not in MERGE_AUTHORIZED_SEATS:
        return None, f"seat {the_seat!r} is not authorized to merge"
    if prs is not None and not _valid_prs(prs):
        return None, "--prs must be one or more positive PR numbers"
    facts, why = _batch_facts(clean, repo_root, sid, the_seat,
                              sorted(set(prs)) if prs else None, runner)
    if facts is None:
        return None, why
    shape = hg.batch_line_error(facts)
    if shape:
        return None, shape
    path = _request_path(facts["repo"], facts["seat"], sid)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"line": facts, "created_at": _iso(now_s if now_s is not None
                                                                  else _now_s())}),
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return facts, ""


ARM_WAIT_MAX_REQUESTS = 64


def _record_arm_wait(sid: str, repo: str, repo_root: Path,
                     now_s: float | None = None) -> None:
    """Note that this seat asked the operator for an arm (#345). Read ONLY by
    net.waitstate, so the pair-sweep reads both seats as waiting on the
    operator instead of dark while the arm is outstanding. It authorizes
    nothing and no gate reads it: the batch ceremony keeps its own request
    record. A separate file on purpose, so a single-arm request can never
    overwrite a batch record.

    The marker keeps EVERY request still unanswered (`requests`, oldest
    first), not only the newest: net.waitstate counts its cap from the oldest
    one, so asking again cannot restart the clock. A request is answered once
    a token armed at or after it sits in this worktree; answered requests are
    dropped here. Best-effort: a marker that cannot be written leaves the
    sweep at its old behavior (the seat reads dark, fail-loud)."""
    if not sid or not repo:
        return
    try:
        now = now_s if now_s is not None else _now_s()
        path = _arm_wait_path(sid, repo)
        armed: float | None = None
        data, refusal = _load_token(repo_root)
        if refusal is None and data is not None:
            armed = _parse_iso(str(data.get("armed_at", "")))
        kept: list[float] = []
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        if isinstance(old, dict) and old.get("session_id") == sid \
                and old.get("repo") == repo and isinstance(old.get("requests"), list):
            for raw in old["requests"]:
                at = _parse_iso(raw) if isinstance(raw, str) else None
                if at is None or at > now:
                    continue  # unparseable or future-dated: not a request
                if armed is not None and armed >= at:
                    continue  # answered
                kept.append(at)
        kept = sorted(kept)[:ARM_WAIT_MAX_REQUESTS - 1] + [now]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"session_id": sid, "repo": repo,
                                   "created_at": _iso(now),
                                   "requests": [_iso(t) for t in kept]}),
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — a best-effort marker never breaks grant-request
        pass


def _arm_wait_path(sid: str, repo: str) -> Path:
    """The arm-wait marker's path, keyed by (repo, session id): the grants dir
    is machine-wide, and a session id alone does not name one seat of one repo
    (#338). The readable prefix is the session id; the suffix is a digest of
    both. Built here, not from the batch request record's path, so a change to
    that record's keying cannot break the marker (or `grant-request`).
    net.waitstate finds it by the prefix and matches the repo inside."""
    from guards import human_grant as hg
    key = hashlib.sha256(json.dumps([repo, sid]).encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:80] or "_"
    return hg.grants_dir() / "requests" / f"{safe}.{key}.arm-wait.json"


def _nearest_line_diff(batch: list, facts: dict) -> str:
    same_repo = [ln for ln in batch if isinstance(ln, dict) and ln.get("repo") == facts.get("repo")]
    line = (same_repo or batch)[0]
    wrong = sorted(k for k in set(facts) | set(line) if line.get(k) != facts.get(k))
    return "; ".join(f"{k} grant={line.get(k)!r} derived={facts.get(k)!r}" for k in wrong)


def _batch_grant_ceremony(grant: object, facts: dict, now: float) -> "tuple[str, int, str]":
    """Verify a session.arm.batch grant for THIS seat's line (batch-arm design §5).
    Returns (grant_id, line index, "") after the single-use LINE claim, else
    ("", -1, why).

    Order matters: nothing is mutated (no record deleted, no ledger claim) until
    the signature over the whole manifest has verified (advisor N1), so an
    unsigned file carrying a matching line cannot destroy the genuine request."""
    from guards import human_grant as hg

    def no(why: str) -> "tuple[str, int, str]":
        return "", -1, why

    if not isinstance(grant, dict):
        return no("grant is not a JSON object")
    if grant.get("scope_class") != hg.BATCH_CLASS:
        return no(f"grant scope_class is {grant.get('scope_class')!r}, this ceremony "
                  f"needs {hg.BATCH_CLASS!r}")
    alarm = hg.keyset_alarm(record=True)
    if alarm is not None:
        return no(alarm)
    shape = hg.batch_shape_error(hg.BATCH_CLASS, grant)
    if shape:
        return no(f"grant refused: {shape}")
    batch = grant["batch"]
    matches = [i for i, line in enumerate(batch) if line == facts]
    if len(matches) != 1:
        return no("no line of this grant matches this session: "
                  + _nearest_line_diff(batch, facts) if not matches
                  else "more than one line matches this session")
    res = hg.verify_presented(grant, hg.batch_instruction(batch), _iso(now), ceremony=True)
    if not res.ok:
        return no(f"grant refused: {res.reason}")
    # Signature-valid from here: the request record may now be read and deleted.
    rec_path = _request_path(facts["repo"], facts["seat"], facts["session_id"])
    try:
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rec = None
    rec_path.unlink(missing_ok=True)
    if rec is None:
        # Rollout fallback: a request filed before #338 sits at the old path.
        # Consume it only when it is THIS seat's line; a record another seat
        # left there (the #338 collision) is not ours to read or delete.
        legacy = _legacy_request_path(facts["session_id"])
        try:
            old = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        if isinstance(old, dict) and old.get("line") == facts:
            rec = old
            legacy.unlink(missing_ok=True)
    issued, made = hg._epoch(grant["issued_at"]), None
    if isinstance(rec, dict) and isinstance(rec.get("created_at"), str):
        made = hg._epoch(rec["created_at"])
    if not isinstance(rec, dict) or rec.get("line") != facts:
        return no("this seat has no matching batch request record (run `session_auth "
                  "grant-request session.arm --batch` before the mint)")
    if made is None or issued is None or made > issued or issued - made > REQUEST_MAX_AGE_S:
        return no("this seat's batch request record does not predate the mint "
                  f"within {REQUEST_MAX_AGE_S // 60} min")
    gid, i = str(grant["grant_id"]), matches[0]
    if not hg.consume(hg.line_claim_id(gid, i), str(grant["nonce"])):
        return no(f"line {i} of grant {gid} already claimed (replay) — refused")
    return gid, i, ""


def _prs_text(prs: "list[int]") -> str:
    """The canonical text of a PR set, as the session object carries it."""
    return ",".join(f"#{n}" for n in sorted(set(prs)))


def _valid_prs(prs: object) -> bool:
    return (isinstance(prs, list) and bool(prs)
            and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in prs))


# The scope's share of the Touch ID prompt. With the arm line's boilerplate this
# stays under human_gate.PROMPT_MAX, so the gate never cuts the arm line itself.
PROMPT_SCOPE_MAX = 200


def _prompt_scope(clean: str) -> str:
    """The scope as the fingerprint prompt shows it: whole, or cut with a visible
    ellipsis. The token keeps the full text either way."""
    if len(clean) <= PROMPT_SCOPE_MAX:
        return clean
    return clean[:PROMPT_SCOPE_MAX - 1].rstrip() + "…"


def _scope_problem(clean: str) -> str:
    if not clean:
        return "empty scope. Name what this session may merge."
    if len(clean) < 8:
        return (f"scope {clean!r} is too short to tell the operator what this "
                "session is for. Name the PRs/arc (and pass --prs to enforce a set).")
    return ""


def _write_token(repo_root: Path, token: dict) -> None:
    """Atomic replace, but UNLOCKED: an arm, amend or narrow racing another
    read-modify-write of the token can lose one update (known debt; the seats
    serialize these by hand today)."""
    path = _runtime_path(repo_root, TOKEN_REL)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(token, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


_GATE_NEXT_STEP = {
    "declined": "The operator did not authenticate; ask before arming again.",
    "timeout": "Retry only once the operator is at the Mac and watching for the sheet.",
    "unreachable": ("Retrying from here fails the same way: arm from an unsandboxed "
                    "direct pane on the operator's Mac (or use --grant-file)."),
    "unprovable": ("Retrying cannot help: remove the NOPASSWD rule, or arm with "
                   "--grant-file."),
}


def _gate_refusal(res: object) -> ArmRefusal:
    """The arm refusal for a closed Touch ID gate, worded and coded by class."""
    if isinstance(res, GateResult) and res.kind in _GATE_NEXT_STEP:
        return ArmRefusal(
            f"refusing to arm ({res.kind}): no fresh human authentication — "
            f"{res.detail} {_GATE_NEXT_STEP[res.kind]} Fail-closed: no "
            "fingerprint, no token.", res.exit_code)
    return ArmRefusal("refusing to arm: no fresh human authentication (timeout, "
                   "refusal, or error). Fail-closed: no fingerprint, no token.")


def _arm(
    scope: str,
    repo_root: Path,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    gate_fn: "Callable[[str], bool | GateResult]" = human_gate,
    seat: str | None = None,
    env: dict[str, str] | None = None,
    runner: Runner = _run,
    grant: "dict | None" = None,
    prs: "list[int] | None" = None,
) -> tuple[bool, str]:
    """One Touch ID arms THIS session to merge within `scope` — or, since the operator's
    2026-09-23 ruling, one passkey grant of class `session.arm` (`grant`) that
    names this repo, seat, session and scope. Same token, same 6h cap, same
    bounds; the grant only replaces the fingerprint.

    The scope text MUST reach the Touch ID prompt: the operator's fingerprint authorizes
    THAT SCOPE, and the prompt is the only place he sees what he is granting. An
    arm whose scope never renders is a blind grant.

    SCOPE IS ADVISORY TEXT. No code parses it: it is what the operator reads and
    agrees to at arm time, and the reviewing seat is expected to stay inside it.
    The only mechanical scope bound is `prs`: when given, the token carries that
    PR set, the prompt shows it, and check() refuses any other PR (out-of-scope).
    A passkey-grant arm carries it as the session object's `prs` field, so the
    phone page shows it too. The length check below catches an empty or typo'd
    scope; it does not judge breadth.
    """
    now = now_s if now_s is not None else _now_s()
    # Identity binds to THIS seat's SWITCHBOARD ENTRY, not to this transient CLI
    # process. os.getpid() gave arm and check different pids that can never
    # match (#85); the registry entry is one stable identity both invocations
    # read. Tests inject session_id+pid directly and stay off this path.
    if session_id is None or pid is None:
        ident, why = _registry_identity(_net_name(), runner=runner)
        if ident is None:
            return False, f"refusing to arm: {why}"
        reg_sid, reg_pid = ident
        sid = session_id if session_id is not None else reg_sid
        the_pid = pid if pid is not None else reg_pid
    else:
        sid, the_pid = session_id, pid

    clean = " ".join((scope or "").split())
    problem = _scope_problem(clean)
    if problem:
        return False, f"refusing to arm: {problem}"
    pr_set: "list[int] | None" = None
    if prs is not None:
        if not _valid_prs(prs):
            return False, ("refusing to arm: --prs must be one or more positive PR "
                           "numbers")
        pr_set = sorted(set(prs))
    bound_line = (f"PRs={_prs_text(pr_set)}" if pr_set
                  else "PRs=any (scope is advisory)")
    if not sid:
        return False, (
            "refusing to arm: no MAW_SESSION_ID — a token that cannot be bound to "
            "a session cannot be expired with it."
        )

    the_seat = (seat if seat is not None else _seat()).strip()
    if not the_seat:
        return False, (
            "refusing to arm: no seat identity (MAW_BRIDGE_ROLE) — the merge "
            "grant rests on the seat; a token without one cannot be checked "
            "against MERGE_AUTHORIZED_SEATS."
        )

    # THE GRANT'S BOUNDARY. Checked before anything else about the seat, because
    # a seat the operator never granted merge authority to has no business reaching a
    # seat-env check, let alone his fingerprint.
    if the_seat not in MERGE_AUTHORIZED_SEATS:
        return False, (
            f"refusing to arm: seat {the_seat!r} is not authorized to merge. The operator "
            f"granted merge authority to {list(MERGE_AUTHORIZED_SEATS)} on "
            f"2026-07-16 ('the advisor merges in the reccomended order'; 'the "
            f"executor no longer merges at all').\n"
            f"This is not a bug to route around: widening it is a "
            f"scope-of-authority change (Layer-1 floor) and needs the operator on the "
            f"trusted path — not a peer's say-so, and not your own reasoning "
            f"about what would be convenient."
        )

    # The seat CLAIM must survive net.seatcheck before the operator's finger goes anywhere
    # near it. A launch-env/seat contradiction is an OBSERVED class here (the %5
    # raw-pane-paste tell), and it is precisely the input that would defeat
    # no-self-merge: a session mis-seated as 'executor' bakes that into the token
    # under the operator's fingerprint, and then the advisor may merge its own PRs.
    # REUSED, not reimplemented — seatcheck is the single source of this policy.
    if the_seat in seatcheck.ROLES:
        rc, msgs = seatcheck.check(the_seat, env=env)
        if rc != 0:
            return False, (
                "refusing to arm: seat-env contradiction — "
                + "; ".join(msgs)
                + "\nDisputed seating is a scope-of-authority question (Layer-1 "
                "floor). Page the operator; do not arm."
            )

    # The scope AND THE SEAT are in the prompt the operator fingerprints. This is the grant,
    # and it is the only moment a human attests to anything in this design — so it
    # must cover every field the gate later leans on. Scope alone would authorize
    # WHAT may be merged while saying nothing about WHO merges it, leaving
    # no-self-merge resting on a value no human ever saw.
    # The repo is part of the grant too: the token binds it and check() refuses
    # any other repo, so the human must see it here.
    repo_id = _repo_identity(repo_root, runner)
    if pr_set and not repo_id:
        return False, ("refusing to arm: --prs needs a PR rail, and this repo has no "
                       "readable origin (the local no-remote rail has no PR numbers)")
    goal_sha256 = None
    if grant is not None and isinstance(grant, dict) \
            and grant.get("scope_class") == _batch_class():
        # F9: a batch line would replace a live token and silently restart its 6h.
        live, _line = status(repo_root, now_s=now)
        if live:
            return False, ("refusing to arm: this seat already holds a live token; a "
                           "batch line never re-arms. Amend it, or re-arm by the "
                           "single-seat path")
        facts, why = _batch_facts(clean, repo_root, sid, the_seat, pr_set, runner)
        if facts is None:
            return False, f"refusing to arm: {why}"
        grant_id, line_i, why = _batch_grant_ceremony(grant, facts, now)
        if not grant_id:
            return False, f"refusing to arm: {why}"
        armed_via = f"grant-batch:{grant_id}:{line_i}"
        goal_sha256 = hashlib.sha256(json.dumps(
            facts["goal"], separators=(",", ":")).encode("utf-8")).hexdigest()
    elif grant is not None:
        if not repo_id:
            return False, ("refusing to arm: no readable origin remote, so the "
                           "grant's one-repo binding cannot be checked (Touch ID "
                           "still arms this repo)")
        facts = {"repo": repo_id, "seat": the_seat,
                 "session_id": sid, "scope": clean}
        if pr_set:
            facts["prs"] = _prs_text(pr_set)
        grant_id, why = _session_grant_ceremony(grant, "session.arm", facts, now)
        if not grant_id:
            return False, f"refusing to arm: {why}"
        armed_via = f"grant:{grant_id}"
    else:
        res = gate_fn(
            f"ARM merge auth as seat={the_seat} repo={repo_id or 'none (local rail)'} "
            f"{bound_line} (6h max, this session only) — scope: {_prompt_scope(clean)}"
        )
        # Opens on exactly what it opened on before (GateResult is truthy iff
        # ok); the class only words and codes the refusal.
        if not res:
            return False, _gate_refusal(res)
        armed_via = "touch-id"

    token = {
        "session_id": sid,
        "pid": the_pid,
        "armed_at": _iso(now),
        "expires_at": _iso(now + MAX_TOKEN_AGE_H * 3600),
        "scope": clean,
        # The acting seat is FIXED HERE, at the one moment the operator is physically
        # present. check() reads it from the token rather than from a flag: an
        # `--acting-seat` argument lets the caller claim to be the other seat,
        # which evaporates no-self-merge exactly as a caller-supplied author does.
        "seat": the_seat,
        # The repo this arm authorizes (owner/name from origin; "" when there is
        # none, which only the local no-remote rail can use).
        "repo": repo_id,
        # The enforced PR bound, or None: then the scope text is advisory.
        "prs": pr_set,
        "nonce": secrets.token_hex(16),
        "armed_via": armed_via,
    }
    if goal_sha256:
        # F10: ties the arm to the goal bullets the operator saw on the card.
        token["goal_sha256"] = goal_sha256
    _write_token(repo_root, token)
    audit(repo_root, {"ts": _iso(now), "event": "arm", "session_id": sid,
                      "scope": clean, "expires_at": token["expires_at"],
                      "via": armed_via,
                      **({"goal_sha256": goal_sha256} if goal_sha256 else {})})
    return True, (
        f"armed until {token['expires_at']} (6h cap) for scope: {clean}\n"
        + (f"Enforced PR set: {bound_line[4:]}\n" if pr_set else
           "No --prs: the scope is advisory; check() will authorize any PR in this "
           "repo that has an approved verdict.\n") +
        f"Token dies with this session (pid {the_pid}) or at the cap, whichever first."
    )


def amend(
    scope: str,
    repo_root: Path,
    grant: object,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
    prs: "list[int] | None" = None,
) -> tuple[bool, str]:
    """Amend the LIVE token's scope with one passkey grant of class
    `scope.amend` (operator ruling 2026-09-23, refit ruling 3). Every refusal is
    audited (gate 8).

    What an amend can NOT do, by construction: change the repo (derived from the
    origin remote and bound in the grant), change the seat or session (read from
    the live token), touch MERGE_AUTHORIZED_SEATS (a constant), or extend the
    window (armed_at, expires_at and pid are copied unchanged). It replaces the
    scope text, and only from the scope the grant names as `from_scope`; with
    `prs` it also replaces the enforced PR set, and the grant's session object
    must carry that same set. Shrinking the set needs no grant at all: `narrow`.
    """
    now = now_s if now_s is not None else _now_s()

    def refuse(why: str) -> tuple[bool, str]:
        audit(repo_root, {"ts": _iso(now), "event": "amend-refused",
                          "grant_id": _grant_id(grant), "reason": why})
        return False, f"refusing to amend: {why}"

    data, refusal = _session_bounds(repo_root, session_id, pid, now, runner)
    if refusal is not None:
        return refuse(f"no live armed session [{refusal.bound}] — {refusal.detail}")
    assert data is not None
    token_seat = str(data.get("seat", ""))
    if token_seat not in MERGE_AUTHORIZED_SEATS:
        return refuse(f"token seat {token_seat!r} is not authorized to merge")
    clean = " ".join((scope or "").split())
    problem = _scope_problem(clean)
    if problem:
        return refuse(problem)
    if clean == data["scope"]:
        return refuse("the new scope equals the current scope")
    repo_id = _repo_identity(repo_root, runner)
    if not repo_id:
        return refuse("no readable origin remote: the one-repo binding cannot be checked")
    if repo_id != str(data.get("repo", "")):
        return refuse(f"this session was armed for repo {data.get('repo')!r}, but the "
                      f"origin here is {repo_id!r}")
    facts = {"repo": repo_id, "seat": token_seat,
             "session_id": str(data["session_id"]), "from_scope": str(data["scope"]),
             "scope": clean}
    if prs is not None:
        if not _valid_prs(prs):
            return refuse("--prs must be one or more positive PR numbers")
        current = data.get("prs")
        typed = isinstance(grant, dict) and "typed" in grant
        if typed and (current is None or not set(prs) <= set(current)):
            return refuse("a typed (coordinator-recorded) amend may only shrink an armed PR set; "
                          "widening it, or bounding an unbounded session, needs a "
                          "passkey-signed grant or a new arm")
        facts["prs"] = _prs_text(prs)
    if isinstance(grant, dict) and "typed" in grant:
        # Identity laundering (advisor F2 on #238): the armed seat could briefly
        # register as `cos`, record a typed amend, and switch back. The token's
        # pid is the beneficiary of any widening, so a typed amend whose recorder
        # ran under THAT pid is refused, whatever name it recorded under.
        from guards import typed_approval as ta
        line = ta._typed_issued_line(str(grant.get("grant_id", "")))
        if line is not None and line.get("recorder_pid") == data.get("pid"):
            return refuse(f"the typed amend was recorded by the armed session itself "
                          f"(pid {data.get('pid')}); a seat cannot approve its own widening")
    grant_id, why = _session_grant_ceremony(grant, "scope.amend", facts, now)
    if not grant_id:
        return refuse(why)
    # A Mac Secure-Enclave grant (`guards.mint --session-file`) carries `sig`;
    # the audit names the ceremony the operator actually performed.
    carrier = ("typed" if "typed" in grant else
               "touch-id" if "sig" in grant else "passkey")
    token = dict(data)
    token["scope"] = clean
    if prs is not None:
        token["prs"] = sorted(set(prs))
    token["amended"] = list(data.get("amended") or []) + [
        {"at": _iso(now), "grant_id": grant_id, "from_scope": data["scope"],
         "carrier": carrier}]
    _write_token(repo_root, token)
    audit(repo_root, {"ts": _iso(now), "event": "amend", "grant_id": grant_id,
                      "carrier": carrier,
                      "session_id": token["session_id"], "from_scope": data["scope"],
                      "scope": clean, "expires_at": token["expires_at"]})
    return True, (f"scope amended to: {clean}"
                  + (" (OPERATOR-TYPED: coordinator-recorded, no passkey)" if carrier == "typed" else "")
                  + "\n"
                  f"expiry unchanged: {token['expires_at']} (an amend never extends)")


def narrow(
    repo_root: Path,
    prs: "list[int]",
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
) -> tuple[bool, str]:
    """Shrink the live token's enforced PR set. No grant: narrowing authority
    needs no consent. The new set must be non-empty and inside the current one
    (any set, when the session was armed with no set). Widening needs a new arm
    or a grant-backed amend. Audited."""
    now = now_s if now_s is not None else _now_s()
    data, refusal = _session_bounds(repo_root, session_id, pid, now, runner)
    if refusal is not None:
        return False, f"refusing to narrow: no live armed session [{refusal.bound}]"
    assert data is not None
    if not _valid_prs(prs):
        return False, "refusing to narrow: --prs must be one or more positive PR numbers"
    new = sorted(set(prs))
    current = data.get("prs")
    if current is not None and not set(new) <= set(current):
        return False, (f"refusing to narrow: {_prs_text(new)} is not inside the armed "
                       f"set {_prs_text(current)}; widening needs a new arm or a "
                       f"grant-backed amend")
    token = dict(data)
    token["prs"] = new
    _write_token(repo_root, token)
    audit(repo_root, {"ts": _iso(now), "event": "narrow", "session_id": token["session_id"],
                      "from_prs": current, "prs": new})
    return True, f"PR set narrowed to {_prs_text(new)} (expiry unchanged: {token['expires_at']})"


def session_request(
    scope_class: str,
    scope: str,
    repo_root: Path,
    runner: Runner = _run,
    prs: "list[int] | None" = None,
) -> "tuple[dict | None, str]":
    """The UNSIGNED half of a phone arm/amend: the `session` object and the
    instruction a mint must sign, derived exactly as arm()/amend() will derive
    them. Returns ({"session": ..., "instruction": ...}, "") or (None, why)."""
    from guards import human_grant as hg

    clean = " ".join((scope or "").split())
    problem = _scope_problem(clean)
    if problem:
        return None, problem
    ident, why = _registry_identity(_net_name(), runner=runner)
    if ident is None:
        return None, why
    sid = ident[0]
    repo = _repo_identity(repo_root, runner)
    if not repo:
        return None, "no readable origin remote (a session grant binds owner/name)"
    if scope_class == "session.arm":
        seat = _seat().strip()
        if seat not in MERGE_AUTHORIZED_SEATS:
            return None, f"seat {seat!r} is not authorized to merge"
        session = {"repo": repo, "seat": seat, "session_id": sid, "scope": clean}
    elif scope_class == "scope.amend":
        data, refusal = _session_bounds(repo_root, None, None, _now_s(), runner)
        if refusal is not None:
            return None, f"no live armed session [{refusal.bound}]"
        assert data is not None
        session = {"repo": repo, "seat": str(data["seat"]), "session_id": str(data["session_id"]),
                   "from_scope": str(data["scope"]), "scope": clean}
    else:
        return None, f"not a session class: {scope_class!r}"
    if prs is not None:
        if not _valid_prs(prs):
            return None, "--prs must be one or more positive PR numbers"
        session["prs"] = _prs_text(prs)
    shape = hg.session_shape_error(scope_class, session)
    if shape:
        return None, shape
    return {"session": session,
            "instruction": hg.session_instruction(scope_class, session)}, ""


def _read_grant_file(path: str) -> "tuple[object, str]":
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as e:
        return None, f"unreadable grant file: {e}"


def _load_token(repo_root: Path) -> tuple[dict | None, Refusal | None]:
    """Read + validate the token's SHAPE. Every failure path refuses."""
    path = _runtime_path(repo_root, TOKEN_REL)
    if not path.is_file():
        return None, Refusal("no-token", f"no session token at {path} — arm first")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, Refusal("unreadable-token", f"cannot read {path}: {e}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, Refusal("corrupt-token", f"token is not valid JSON: {e}")
    if not isinstance(data, dict):
        return None, Refusal("corrupt-token", "token is not a JSON object")
    for field in ("session_id", "pid", "armed_at", "expires_at", "scope", "nonce"):
        if field not in data:
            return None, Refusal("malformed-token", f"token missing {field!r}")
    prs = data.get("prs")
    if prs is not None and not _valid_prs(prs):
        return None, Refusal("malformed-token",
                             f"token prs must be null or a list of positive PR numbers, "
                             f"not {prs!r}; re-arm")
    return data, None


def _expiry_line(repo_root: Path, now_s: float | None = None) -> str:
    """A human 'token expires <ISO> (Nh Nm left)' line for a PASS, or '' if the
    token is unreadable.

    #103: the 6h cap fired mid-run and the seat learned only at refusal time.
    Surfacing the remaining window on every PASS is a WARNING, not a softening —
    the cap itself never moves; the operator just sees it coming.
    """
    data, _refusal = _load_token(repo_root)
    if data is None:
        return ""
    exp = _parse_iso(str(data.get("expires_at", "")))
    if exp is None:
        return ""
    now = now_s if now_s is not None else _now_s()
    left = int(exp - now)
    if left < 0:
        return f"token EXPIRED at {data['expires_at']}"
    h, m = left // 3600, (left % 3600) // 60
    return f"token expires {data['expires_at']} ({h}h {m}m left)"


def _time_bounds(data: dict, now: float) -> "Refusal | None":
    """The token's time window — the drift-prone security core SHARED by check()
    and status(): unparseable timestamps, a backward clock, and the 6h cap.
    Factored out so a status read can NEVER disagree with a merge check on whether
    the window is still open (the run-3 'a stale token looked live' bite). Returns
    the tripped Refusal, or None if every time bound holds."""
    armed_at = _parse_iso(data["armed_at"])
    expires_at = _parse_iso(data["expires_at"])
    if expires_at is None or armed_at is None:
        return Refusal("corrupt-token", "armed_at/expires_at unparseable — refusing")
    if now < armed_at:
        # Wall clock moved backward. Do NOT silently extend the window.
        return Refusal(
            "clock-backward",
            f"now ({_iso(now)}) precedes armed_at ({data['armed_at']}) — clock "
            "moved; refusing rather than extending the window",
        )
    if now >= expires_at:
        return Refusal(
            "cap-reached",
            f"6h cap reached (armed {data['armed_at']}, expired "
            f"{data['expires_at']}) — re-arm",
        )
    return None


def status(repo_root: Path, *, now_s: float | None = None) -> tuple[bool, str]:
    """Read-only: is there a LIVE armed token on this machine right now?

    Reports the token's OWN liveness — shape (_load_token), the shared time window
    (_time_bounds), and whether the ARMING PROCESS is still alive — and is NOT
    bound to the caller's identity, so anyone can run it as a diagnostic. The
    pid-liveness check is the run-3 bite's answer: a token whose arming session
    died is STALE, never 'armed'. Returns (True, armed-line) when live, else
    (False, why) — the bool doubles as an exit code for scripting.
    """
    now = now_s if now_s is not None else _now_s()
    data, refusal = _load_token(repo_root)
    if refusal is not None:
        return False, f"NOT ARMED [{refusal.bound}] — {refusal.detail}"
    assert data is not None
    tb = _time_bounds(data, now)
    if tb is not None:
        return False, f"NOT ARMED [{tb.bound}] — {tb.detail}"
    try:
        token_pid = int(data["pid"])
    except (TypeError, ValueError):
        return False, "NOT ARMED [corrupt-token] — pid is not an integer"
    if not pid_alive(token_pid):
        return False, (
            f"NOT ARMED [session-gone] — arming session (pid {token_pid}) is gone; "
            "the token on disk is STALE — re-arm"
        )
    return True, (
        f"ARMED — scope={data['scope']!r}  "
        f"prs={_prs_text(data['prs']) if data.get('prs') else 'any'}  "
        f"session={data['session_id']}  "
        f"pid={token_pid}  {_expiry_line(repo_root, now)}"
        + (f"  goal={str(data['goal_sha256'])[:12]}" if data.get("goal_sha256") else "")
    )


def peer_status(common_dir: str, entries: "list", *, now_s: float | None = None) -> tuple[bool, str]:
    """Read-only, for the EXECUTOR: is this repo's registered ADVISOR armed?

    The executor DERIVES the arm instead of asking the operator again or taking
    the advisor's word: a live registry entry with role advisor for the SAME
    logical repo (shared .git) whose worktree holds a live token that THAT seat
    armed (seat advisor, same pid, same session id: the bounds check() applies
    to the same token). The token is read ONCE. It proves an arm is live for the
    printed scope, not that the operator saw any particular goal; the executor
    compares the /maw:goal against that scope. It grants nothing: the executor
    still cannot merge.
    """
    now = now_s if now_s is not None else _now_s()
    reasons: list[str] = []
    advisors = [e for e in entries
                if getattr(e, "role", "") == "advisor"
                and common_dir and getattr(e, "repo_common_dir", "") == common_dir]
    if not advisors:
        return False, "NOT ARMED [no-advisor] — no live advisor seat registered for this repo"
    for e in advisors:
        root = Path(e.repo_root)
        data, refusal = _load_token(root)
        if refusal is not None:
            reasons.append(f"{e.name}: NOT ARMED [{refusal.bound}] — {refusal.detail}")
            continue
        assert data is not None
        tb = _time_bounds(data, now)
        if tb is not None:
            reasons.append(f"{e.name}: NOT ARMED [{tb.bound}] — {tb.detail}")
            continue
        if data.get("seat") != "advisor" or data.get("pid") != e.pid \
                or not e.session_id or data.get("session_id") != e.session_id:
            reasons.append(f"{e.name}: NOT ARMED [not-this-seat] — the token was not armed "
                           f"by this advisor seat (seat/pid/session differ from the registry)")
            continue
        if not pid_alive(e.pid):
            reasons.append(f"{e.name}: NOT ARMED [session-gone] — arming session "
                           f"(pid {e.pid}) is gone")
            continue
        return True, (
            f"{e.name} ARMED — scope={data['scope']!r}  "
            f"prs={_prs_text(data['prs']) if data.get('prs') else 'any'}  "
            f"armed_at={data.get('armed_at')}  {_expiry_line(root, now)}"
            + (f"  goal={str(data['goal_sha256'])[:12]}" if data.get("goal_sha256") else "")
        )
    return False, "; ".join(reasons)


def _session_bounds(
    repo_root: Path,
    session_id: str | None,
    pid: int | None,
    now: float,
    runner: Runner = _run,
) -> "tuple[dict | None, Refusal | None]":
    """The SESSION half of the gate, shared by `check` and `check_local`.

    Everything here is about the TOKEN — identity, the session/pid bind, the
    arming process still being alive, the 6h cap and clock sanity — and nothing
    about what is being merged. It is extracted rather than duplicated because a
    second copy of a fail-closed gate is a gate that drifts: the PR path and the
    local path must expire on exactly the same bounds.
    """
    # Same stable-identity derivation as arm(): the token's session_id+pid were
    # bound to THIS seat's switchboard entry, so check must resolve identity the
    # same way rather than against its own transient pid (#85). Tests inject both
    # and stay off this path.
    if session_id is None or pid is None:
        ident, why = _registry_identity(_net_name(), runner=runner)
        if ident is None:
            return None, Refusal("no-identity", why)
        reg_sid, reg_pid = ident
        sid = session_id if session_id is not None else reg_sid
        the_pid = pid if pid is not None else reg_pid
    else:
        sid, the_pid = session_id, pid

    data, refusal = _load_token(repo_root)
    if refusal is not None:
        return None, refusal
    assert data is not None

    # bound to THIS session — a token must not be usable by another seat.
    if data["session_id"] != sid or not sid:
        return None, Refusal(
            "wrong-session",
            f"token belongs to session {data['session_id']!r}, this is {sid!r} — "
            "a token is not transferable between sessions",
        )
    # the arming process must still be alive. "Once per session" expressed
    # structurally: no timer to tune, no token outliving its window.
    if data["pid"] != the_pid:
        return None, Refusal(
            "wrong-pid",
            f"token armed by pid {data['pid']}, this is pid {the_pid} — a "
            "recycled or different pid cannot inherit a token",
        )
    if not pid_alive(int(data["pid"])):
        return None, Refusal(
            "session-gone",
            f"arming session (pid {data['pid']}) is gone — re-arm",
        )
    # the 6h cap + clock sanity, via the shared _time_bounds so status() reads
    # the EXACT same window.
    tb = _time_bounds(data, now)
    if tb is not None:
        return None, tb
    return data, None



# ---------------------------------------------------------------------------
# LOCAL-BRANCH MODE (#146-1) — the audited rail for repos with NO remote.
#
# Two arcs on a remote-less internal repo merged OUTSIDE the wrapper entirely
# (nine merges as bare `git merge --ff-only`, no verdicts, no audit) because
# merge/check/record-verdict are gh-PR-bound and that repo has no remote. The
# operator approved building this rail for internal, remote-less repos (#146).
#
# THE BOUNDARY THAT MAKES IT SAFE: it REFUSES the moment any remote exists. On a
# repo with a remote, branch protection is the guarantee, and a local rail there
# would be a way around it — an agent could merge to a local `main` and push, or
# simply prefer the weaker path. "No remote" is not a convenience check; it is
# the condition under which this mode's weaker guarantee is the ONLY guarantee
# available anyway.
#
# EVERY local merge is stamped `local-unprotected` BY THE CODE. No flag drops it.
# Branch protection does not exist on this rail and the audit trail must not read
# as though it does — the honesty is the point, not the ceremony.
LOCAL_TAG = "local-unprotected"


def _remotes(repo_root: Path, runner: Runner = _run) -> "tuple[list[str], str]":
    """(remote names, error). An unreadable git is NOT proof of "no remote": it
    returns an error, and every caller treats that as a refusal."""
    try:
        cp = runner(["git", "-C", str(repo_root), "remote"])
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"cannot list remotes: {e}"
    if getattr(cp, "returncode", 1) != 0:
        return [], f"cannot list remotes: {(cp.stderr or '').strip()}"
    return [r.strip() for r in (cp.stdout or "").splitlines() if r.strip()], ""


def _refuse_if_remote(repo_root: Path, runner: Runner = _run) -> str | None:
    remotes, err = _remotes(repo_root, runner)
    if err:
        return (f"{err} — local mode refuses when it cannot PROVE the repo has no "
                f"remote; an unreadable git is not an absent remote")
    if remotes:
        return (f"this repo has remote(s) {remotes} — local-branch mode is for "
                f"repos with NO remote. Where a remote exists, branch protection "
                f"is the guarantee and the PR rail is the audited path; a local "
                f"merge here would be a way around it")
    return None


def local_branch_sha(repo_root: Path, branch: str,
                     runner: Runner = _run) -> "tuple[str, str]":
    """(sha, error) for a LOCAL branch ref. refs/heads/ is spelled out so a tag
    or a remote-tracking ref of the same name cannot stand in for a branch."""
    try:
        cp = runner(["git", "-C", str(repo_root), "rev-parse", "--verify",
                     f"refs/heads/{branch}"])
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"cannot resolve branch {branch!r}: {e}"
    if getattr(cp, "returncode", 1) != 0:
        return "", f"no local branch {branch!r}"
    sha = (cp.stdout or "").strip()
    return (sha, "") if sha else ("", f"no local branch {branch!r}")


def _current_branch(repo_root: Path, runner: Runner = _run) -> str:
    try:
        cp = runner(["git", "-C", str(repo_root), "symbolic-ref", "--quiet",
                     "--short", "HEAD"])
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(cp, "returncode", 1) != 0:
        return ""
    return (cp.stdout or "").strip()


def _local_default_branch(repo_root: Path, runner: Runner = _run) -> str:
    """The repo's default branch WITHOUT a remote to ask. `main` then `master`,
    both by ref existence — no guessing from the current checkout, which is
    whatever the operator last touched."""
    for name in ("main", "master"):
        sha, _err = local_branch_sha(repo_root, name, runner)
        if sha:
            return name
    return ""


def record_verdict_local(
    repo_root: Path,
    branch: str,
    verdict: str,
    seat: str | None = None,
    runner: Runner = _run,
    now_s: float | None = None,
    expect_sha: str | None = None,
) -> "tuple[bool, str]":
    """The advisor's verdict for a LOCAL branch, bound to that branch's head SHA.

    Same binding as the PR path — the SHA is DERIVED from git, never accepted
    from the caller — and the record carries `local_branch` instead of `pr`, so
    the two ledgers cannot authorize each other.
    """
    now = now_s if now_s is not None else _now_s()
    remote_refusal = _refuse_if_remote(repo_root, runner)
    if remote_refusal:
        return False, f"cannot record local verdict: {remote_refusal}"
    sha, err = local_branch_sha(repo_root, branch, runner)
    if not sha:
        return False, f"cannot record local verdict: {err}"
    if expect_sha and not _sha_matches(expect_sha, sha):
        return False, (
            f"cannot record local verdict: branch {branch!r} head {sha[:7]} != "
            f"--expect-sha {expect_sha.strip()[:12]} — refusing to record a verdict "
            f"for code you did not review (wrong branch, or the head moved)")
    the_seat, sid, why = _reviewer_identity(seat, runner)
    if not the_seat:
        return False, f"cannot record local verdict: {why}"
    rec = {
        "ts": _iso(now),
        "local_branch": branch,
        "sha": sha,
        "verdict": verdict,
        "seat": the_seat,
        "protection": LOCAL_TAG,
    }
    if sid:
        rec["session_id"] = sid
    path = _runtime_path(repo_root, VERDICT_REL)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return True, (f"recorded {verdict} for local branch {branch!r} @ {sha[:7]} "
                  f"by {the_seat} [{LOCAL_TAG}]")


def load_verdict_local(repo_root: Path, branch: str) -> dict | None:
    """The LATEST verdict for a LOCAL branch. Records keyed by `pr` are ignored:
    a PR verdict must never authorize a local merge, or the weaker rail would
    inherit the stronger one's receipts."""
    path = _runtime_path(repo_root, VERDICT_REL)
    if not path.is_file():
        return None
    latest: dict | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a torn line is not an authorization
        if isinstance(rec, dict) and rec.get("local_branch") == branch:
            latest = rec
    return latest


def check_local(
    branch: str,
    repo: str,
    repo_root: Path,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
) -> "tuple[bool, Refusal | None]":
    """Fail-closed: may THIS seat merge LOCAL branch `branch` right now?

    The read-only gate; the merge path uses `_authorize_local` for the SHA.
    """
    ok, refusal, _sha = _authorize_local(branch, repo, repo_root,
                                         session_id=session_id, pid=pid,
                                         now_s=now_s, runner=runner)
    return ok, refusal


def _authorize_local(
    branch: str,
    repo: str,
    repo_root: Path,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
) -> "tuple[bool, Refusal | None, str]":
    """`check_local`, plus the branch head it authorized ("" on refusal).

    The session half is the SAME code as `check` (`_session_bounds`), so the
    expiry, session bind and liveness bounds cannot drift between the rails. The
    subject half swaps gh for git: the branch must exist locally, carry an
    approved verdict, and its head must still be the SHA that verdict reviewed.
    """
    now = now_s if now_s is not None else _now_s()
    remote_refusal = _refuse_if_remote(repo_root, runner)
    if remote_refusal:
        return False, Refusal("remote-exists", remote_refusal), ""

    data, refusal = _session_bounds(repo_root, session_id, pid, now, runner)
    if refusal is not None:
        return False, refusal, ""
    assert data is not None
    if data.get("prs") is not None:
        return False, Refusal(
            "out-of-scope",
            f"this session was armed with a PR set ({_prs_text(data['prs'])}), which "
            f"cannot bound a local branch — re-arm without --prs for the local rail"), ""

    head, err = local_branch_sha(repo_root, branch, runner)
    if not head:
        return False, Refusal("branch-unverifiable",
                              f"cannot verify branch {branch!r}: {err}"), ""

    verdict = load_verdict_local(repo_root, branch)
    if verdict is None:
        return False, Refusal(
            "no-verdict",
            f"local branch {branch!r} has no recorded verdict — the verdict is the "
            f"gate; record one at review time"), ""
    not_reviewer = _reviewer_refusal(verdict, f"local branch {branch!r}")
    if not_reviewer is not None:
        return False, not_reviewer, ""
    if str(verdict.get("verdict", "")).strip().lower() not in ("approved", "approve"):
        return False, Refusal(
            "not-approved",
            f"local branch {branch!r}'s latest verdict is "
            f"{verdict.get('verdict')!r}, not approved — refusing"), ""
    reviewed = str(verdict.get("sha", "")).strip()
    if not reviewed:
        return False, Refusal(
            "verdict-unbound",
            f"local branch {branch!r}'s verdict records no reviewed SHA — cannot "
            f"prove it reviewed the code being merged; refusing"), ""
    if reviewed != head:
        return False, Refusal(
            "sha-drift",
            f"local branch {branch!r} was reviewed at {reviewed[:7]} but its head is "
            f"now {head[:7]} — the verdict does not cover this code; re-review"), ""
    return True, None, head


def _merge_local_body(
    branch: str,
    repo_root: Path,
    *,
    dry_run: bool = False,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
    repo: str | None = None,
) -> "tuple[bool, str]":
    """The local counterpart of `merge_pr`: authorize, fast-forward, audit.

    Fast-forward ONLY, and only with the default branch checked out. Refusing
    rather than checking out for the caller keeps this from moving a branch out
    from under a worktree — and a non-fast-forward is a REFUSAL here, not a git
    error discovered after the ceremony printed success.
    """
    now = now_s if now_s is not None else _now_s()
    slug = repo or _repo_slug(repo_root, runner=runner)
    out: list[str] = []

    remote_refusal = _refuse_if_remote(repo_root, runner)
    if remote_refusal:
        return False, f"[refused] {remote_refusal}"

    default_branch = _local_default_branch(repo_root, runner)
    if not default_branch:
        return False, ("[refused] no local `main` or `master` — cannot tell which "
                       "branch is the trunk without a remote to ask")
    if branch == default_branch:
        return False, (f"[refused] {branch!r} IS the default branch — refusing to "
                       f"merge it into itself")

    ok, refusal, head = _authorize_local(branch, slug, repo_root,
                                         session_id=session_id, pid=pid,
                                         now_s=now, runner=runner)
    if not ok:
        assert refusal is not None
        return False, refusal.line()
    exp = _expiry_line(repo_root)
    out.append(f"[auth] OK — local branch {branch!r} in {slug!r} [{LOCAL_TAG}]"
               + (f" — {exp}" if exp else ""))

    current = _current_branch(repo_root, runner)
    if current != default_branch:
        return False, "\n".join(out + [
            f"[refused] {default_branch!r} is not checked out (HEAD is "
            f"{current or 'detached'}) — refusing to move a branch out from under a "
            f"worktree. Check out {default_branch} first, then merge."])

    # `head` is the SHA the verdict covered. Everything below uses it, never
    # the branch name, so a branch moved after authorization cannot merge.
    base, _berr = local_branch_sha(repo_root, default_branch, runner)
    try:
        ff = runner(["git", "-C", str(repo_root), "merge-base", "--is-ancestor",
                     base, head])
        is_ff = getattr(ff, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError) as e:
        return False, "\n".join(out + [f"[refused] cannot prove a fast-forward: {e}"])
    if not is_ff:
        return False, "\n".join(out + [
            f"[refused] {branch!r} is not a fast-forward of {default_branch!r} "
            f"({default_branch} has moved since the branch forked) — rebase the "
            f"branch and re-review; this rail does not create merge commits"])

    if dry_run:
        out.append(f"DRY-RUN — auth passed; {branch!r} ({head[:7]}) -> "
                   f"{default_branch} ({base[:7]}); would now:")
        out.append(f"  1. git merge --ff-only {head}")
        out.append(f"  2. audit the merge, stamped {LOCAL_TAG}")
        return True, "\n".join(out)

    try:
        cp = runner(["git", "-C", str(repo_root), "merge", "--ff-only", head])
    except (OSError, subprocess.SubprocessError) as e:
        return False, "\n".join(out + [f"[merge] FAILED to invoke git: {e}"])
    if getattr(cp, "returncode", 1) != 0:
        return False, "\n".join(
            out + [f"[merge] git merge --ff-only {head} FAILED: "
                   f"{(cp.stderr or cp.stdout or '').strip()}"])

    audit(repo_root, {"ts": _iso(now), "event": "merge", "local_branch": branch,
                      "repo": slug, "method": "ff-only", "base": default_branch,
                      "sha": head, "protection": LOCAL_TAG})
    out.append(f"[merge] {branch!r} fast-forwarded into {default_branch} @ {head[:7]} "
               f"[{LOCAL_TAG} — this repo has no branch protection; the arm and the "
               f"verdict are the whole gate]")
    return True, "\n".join(out)


def check(
    pr: int,
    repo: str,
    repo_root: Path,
    echo_id: str | None = None,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
) -> tuple[bool, Refusal | None]:
    """Fail-closed: may THIS seat merge PR #`pr` in `repo` right now?

    The read-only gate. The merge path calls `_authorize` instead, because it
    also needs the SHA this decision covered.
    """
    ok, refusal, _sha = _authorize(pr, repo, repo_root, echo_id=echo_id,
                                   session_id=session_id, pid=pid, now_s=now_s,
                                   runner=runner)
    return ok, refusal


def _authorize(
    pr: int,
    repo: str,
    repo_root: Path,
    echo_id: str | None = None,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
    gh_repo: str | None = None,
) -> tuple[bool, Refusal | None, str]:
    """`check`, plus the head SHA the decision authorized ("" on refusal).

    `gh_repo` is the repo's canonical identity (owner/name from the origin
    remote of `repo_root`), derived here when not passed. The token, the verdict
    and every gh query must agree on it, so --repo-root cannot select one repo's
    authorization while gh answers about another.

    The merge must be conditioned on this SHA. A head read again later is a
    different fact, and the verdict does not cover it.

    EVERY bound must hold, and every fact is DERIVED — the acting seat from the
    token the operator fingerprinted, the PR's author and head SHA from `gh`, the verdict
    from a recorded artifact. Nothing the acting agent asserts about itself is
    an input. The refusal names which bound tripped.
    """
    now = now_s if now_s is not None else _now_s()
    data, refusal = _session_bounds(repo_root, session_id, pid, now, runner)
    if refusal is not None:
        return False, refusal, ""
    assert data is not None

    the_repo = gh_repo if gh_repo is not None else _repo_identity(repo_root, runner)
    if not the_repo:
        return False, Refusal(
            "repo-unverifiable",
            f"no readable origin remote under {repo_root} — cannot tie PR #{pr} "
            "to the repo this authorization belongs to; refusing"), ""
    if str(data.get("repo", "")) != the_repo:
        return False, Refusal(
            "token-repo-mismatch",
            f"the armed token is for repo {data.get('repo')!r}, not {the_repo!r} "
            "— an arm authorizes one repo; re-arm here"), ""
    armed_prs = data.get("prs")
    if armed_prs is not None and int(pr) not in armed_prs:
        return False, Refusal(
            "out-of-scope",
            f"PR #{pr} is not in the PR set this session was armed for "
            f"({armed_prs}) — re-arm with it in --prs"), ""

    # GROUND TRUTH from gh: author + head SHA. Unreachable gh -> REFUSE. An
    # unverifiable PR is not a mergeable PR.
    facts, err = gh_pr_facts(pr, runner, gh_repo=the_repo)
    if facts is None:
        return False, Refusal(
            "unknown-author",
            f"cannot verify PR #{pr} from gh ({err}) — cannot prove author or "
            "reviewed SHA; refusing",
        ), ""

    # (g) the signed verdict, READ from an artifact. On a no-echo repo this IS
    # the gate, so it cannot be a boolean the advisor asserts about itself.
    verdict = load_verdict(repo_root, pr)
    if verdict is None:
        return False, Refusal(
            "no-verdict",
            f"PR #{pr} has no recorded verdict — the verdict is the gate on a "
            "no-echo repo; record one at review time",
        ), ""
    not_reviewer = _reviewer_refusal(verdict, f"PR #{pr}")
    if not_reviewer is not None:
        return False, not_reviewer, ""
    if str(verdict.get("verdict", "")).strip().lower() not in ("approved", "approve"):
        return False, Refusal(
            "not-approved",
            f"PR #{pr}'s latest verdict is "
            f"{verdict.get('verdict')!r}, not approved — refusing",
        ), ""

    # THE SHA BINDING. A verdict on SHA A must not authorize merging SHA B: the
    # executor pushes and the verdict must NOT follow the code underneath it.
    reviewed = str(verdict.get("sha", "")).strip()
    if not reviewed:
        return False, Refusal(
            "verdict-unbound",
            f"PR #{pr}'s verdict records no reviewed SHA — cannot prove it "
            "reviewed the code being merged; refusing",
        ), ""
    if str(verdict.get("repo", "")) != the_repo:
        return False, Refusal(
            "verdict-repo-mismatch",
            f"PR #{pr}'s verdict was recorded for repo {verdict.get('repo')!r}, "
            f"not {the_repo!r} — it does not cover this repo's PR"), ""
    if reviewed != facts.head_sha:
        return False, Refusal(
            "sha-drift",
            f"PR #{pr} was reviewed at {reviewed[:7]} but its head is now "
            f"{facts.head_sha[:7]} — the verdict does not cover this code; re-review",
        ), ""

    # NO-SELF-MERGE IS RETIRED (operator ruling 2026-07-17, trusted path): within an
    # armed session, which seat authored a PR does not matter; the arm
    # authorizes the advisor to merge in the operator's name. The arm IS
    # the authorization; PR seat-authorship is not a boundary the operator wants enforced.
    # This ruling superseded — and cancelled — his own earlier same-day option-A
    # (per-seat git identity), after he saw its full cost. So the check that once
    # compared the PR author to the acting seat is gone, not fixed. The bounds
    # that remain within an armed session: scope + session/6h expiry + signed
    # verdict + branch protection. See [[merge-posture-session-auth]].

    # (f) The echo checkpoint. As of the 2026-07-24 ruling the default is
    # no-echo for every repo (every pair behaves the same); this fires only for
    # an explicit ECHO_REQUIRED_REPOS
    # opt-in — the retired typo guard's refusal path, kept live for that case.
    if requires_echo(repo):
        if not echo_id:
            return False, Refusal(
                "echo-required",
                f"{repo!r} is in ECHO_REQUIRED_REPOS — this repo opted back "
                f"into the per-batch operator checkpoint; no echo id supplied — "
                f"refusing",
            ), ""
        ok, why = echo_covers(repo_root, echo_id, pr)
        if not ok:
            return False, Refusal("echo-mismatch", why), ""

    return True, None, facts.head_sha


def echo_covers(repo_root: Path, echo_id: str, pr: int) -> tuple[bool, str]:
    """Is PR #`pr` inside the recorded echo `echo_id`'s batch? Fail-closed."""
    path = _runtime_path(repo_root, ECHO_REL)
    if not path.is_file():
        return False, f"no recorded echoes at {path} — record the echo first"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return False, f"cannot read {path}: {e}"
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a torn line is not an authorization
        if not isinstance(rec, dict) or rec.get("echo_id") != echo_id:
            continue
        prs = rec.get("prs")
        if not isinstance(prs, list):
            return False, f"echo {echo_id!r} has a malformed batch — refusing"
        if pr in [int(p) for p in prs if isinstance(p, (int, str)) and str(p).isdigit()]:
            return True, ""
        return False, (
            f"PR #{pr} is NOT in echo {echo_id!r}'s batch ({prs}) — the operator's Y "
            f"authorized that batch, not this PR"
        )
    return False, f"no recorded echo with id {echo_id!r} — refusing"


def record_echo(
    repo_root: Path,
    prs: list[int],
    response: str,
    now_s: float | None = None,
) -> tuple[str, str]:
    """Persist the operator's literal echo + his verbatim reply BEFORE any merge.

    The verbatim capture makes a fabricated echo DISCOVERABLE after the fact. It
    does NOT prevent one — an agent writes this file too. Do not oversell it: it
    is a receipt, not a lock.
    """
    now = now_s if now_s is not None else _now_s()
    echo_id = secrets.token_hex(8)
    rec = {
        "echo_id": echo_id,
        "ts": _iso(now),
        "prs": [int(p) for p in prs],
        "response": response,
    }
    path = _runtime_path(repo_root, ECHO_REL)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return echo_id, f"recorded echo {echo_id} for PRs {rec['prs']}"


def _slug_from_remote_url(url: str) -> str:
    """github.com:owner/maw.git -> 'maw'. '' if unparseable.

    Handles scp-form (git@github.com:owner/repo.git) and URL-form
    (https://github.com/owner/repo.git) by folding ':' into '/' and taking the
    last path component minus the '.git' suffix.
    """
    # Slashes first: 'repo.git/' must lose its '.git' like 'repo.git' does.
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    u = u.replace(":", "/").rstrip("/")
    if not u:
        return ""
    return u.rsplit("/", 1)[-1] if "/" in u else u


def _repo_slug(repo_root: Path, runner: Runner = _run) -> str:
    """The LOGICAL repo identity for echo classification — from the ORIGIN
    REMOTE, not the directory name.

    repo_root.name is the per-seat worktree dir ('maw-executor',
    'maw-advisor'), which never matches NO_ECHO_REPOS / ECHO_REQUIRED_REPOS,
    so every seat silently degraded to echo-required (#87). The origin remote
    basename ('maw') is the stable logical identity every
    worktree of a repo shares. Falls back to the directory name only when the
    remote is unreadable. (Since the 2026-07-24 no-echo-default ruling the slug
    only matters for an ECHO_REQUIRED_REPOS match and the audit trail — a
    mis-resolved slug no longer changes the merge posture.)
    """
    try:
        cp = runner(["git", "-C", str(repo_root), "remote", "get-url", "origin"])
    except (OSError, subprocess.SubprocessError):
        return repo_root.name
    if getattr(cp, "returncode", 1) != 0:
        return repo_root.name
    slug = _slug_from_remote_url(cp.stdout or "")
    return slug or repo_root.name


def _repo_identity(repo_root: Path, runner: Runner = _run) -> str:
    """`owner/name` from the origin remote, or '' when it cannot be read.

    The session-grant binding (ruling 3's "one repo"). Stricter than _repo_slug on
    purpose: no directory-name fallback (a worktree dir named like another repo
    would satisfy it) and the OWNER is part of the identity (two repos named
    `maw` from different owners are different repos). A repo with no readable
    origin cannot be armed or amended by grant; Touch ID still arms it. The HOST
    is not part of the identity: the fleet is GitHub-only, so owner/name is
    unique; revisit if a second forge ever hosts a seat's repo."""
    try:
        cp = runner(["git", "-C", str(repo_root), "remote", "get-url", "origin"])
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(cp, "returncode", 1) != 0:
        return ""
    u = (cp.stdout or "").strip()
    # A hosted remote only: scheme URL (https://, ssh://, git://) or scp-style
    # user@host:path. A local-path origin names no forge repo, so it refuses.
    m = re.fullmatch(r"[a-z][a-z0-9+.-]*://[^/]+/(.+)", u) \
        or re.fullmatch(r"[^/@:\s]+@[^/:\s]+:(.+)", u)
    if not m:
        return ""
    # Slashes first: 'repo.git/' is the same repo as 'repo.git', and without
    # this it bound the token to 'owner/repo.git', which gh then cannot find.
    path = m.group(1).rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [x for x in path.split("/") if x]
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9._-]+", parts[-1]) \
            or not re.fullmatch(r"[A-Za-z0-9._-]+", parts[-2]):
        return ""
    # GitHub owner and repo names are case-insensitive.
    return f"{parts[-2]}/{parts[-1]}".lower()


MERGE_METHODS = ("merge", "squash", "rebase")


def _pr_head_ref(pr: int, runner: Runner = _run,
                 gh_repo: str = "") -> tuple[str, str]:
    """(head_branch, error) from `gh`. Needed for worktree-safe branch cleanup."""
    try:
        cp = runner(["gh", "pr", "view", str(pr), "--json", "headRefName",
                     *_gh_repo_args(gh_repo)])
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"gh unreachable: {e}"
    if getattr(cp, "returncode", 1) != 0 or not (cp.stdout or "").strip():
        return "", f"gh pr view {pr} failed: {(cp.stderr or '').strip()}"
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return "", f"gh returned unparseable JSON: {e}"
    ref = (data.get("headRefName") or "").strip() if isinstance(data, dict) else ""
    if not ref:
        return "", f"gh did not report a head branch for PR {pr}"
    return ref, ""


def _default_branch(runner: Runner = _run, gh_repo: str = "") -> tuple[str, str]:
    """(default_branch, error) from `gh` — the branch a real merge MUST target.

    `gh` ground truth, same posture as gh_pr_facts: an unverifiable default branch
    means the base guard cannot run, so the caller REFUSES (an unverifiable merge
    is not a mergeable merge)."""
    try:
        cp = runner(["gh", "repo", "view", *([gh_repo] if gh_repo else []),
                     "--json", "defaultBranchRef"])
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"gh unreachable: {e}"
    if getattr(cp, "returncode", 1) != 0 or not (cp.stdout or "").strip():
        return "", f"gh repo view failed: {(cp.stderr or '').strip()}"
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        return "", f"gh returned unparseable JSON: {e}"
    ref = data.get("defaultBranchRef") if isinstance(data, dict) else None
    name = ref.get("name", "") if isinstance(ref, dict) else ""
    if not isinstance(name, str) or not name.strip():
        return "", "gh did not report a default branch"
    return name.strip(), ""


def _sha_matches(expected: str, actual: str) -> bool:
    """True if `expected` (possibly an abbreviated SHA the advisor typed) matches
    `actual` (the PR's live full head). Prefix match, case-insensitive, min 7 —
    a shorter fragment is too weak to bind a verdict to."""
    e, a = expected.strip().lower(), actual.strip().lower()
    if len(e) < 7 or not a:
        return False
    return a.startswith(e) or e.startswith(a)


def _retarget_children(
    base_branch: str, default_branch: str, out: list[str], runner: Runner = _run,
    gh_repo: str = "",
) -> bool:
    """Retarget open PRs whose base is `base_branch` onto `default_branch` BEFORE
    that (now-merged) branch is deleted — deleting a merged parent auto-CLOSES a
    stacked child (#185 / the #181 auto-close hit this run).

    Returns True ONLY when we AFFIRMATIVELY know the branch is safe to delete:
    the child list resolved AND every child retargeted cleanly (zero children is
    safe). Returns False if the list failed OR any retarget failed — the caller
    then HOLDS the delete (advisor ruling: a dangling branch costs a warning line
    + a manual delete; a proceeding delete costs auto-closing a child, the #181
    bite plus a re-file + verdict-rebind). A held delete names the child(ren) and
    the manual step, loudly."""
    try:
        cp = runner(["gh", "pr", "list", "--base", base_branch, "--state", "open",
                     "--json", "number", *_gh_repo_args(gh_repo)])
    except (OSError, subprocess.SubprocessError) as e:
        out.append(f"[cleanup] child-retarget list FAILED ({e}) — cannot confirm no "
                   f"child bases on {base_branch}; HOLDING delete")
        return False
    if getattr(cp, "returncode", 1) != 0:
        out.append(f"[cleanup] child-retarget: gh pr list failed ({(cp.stderr or '').strip()}) "
                   f"— HOLDING delete of {base_branch}")
        return False
    try:
        children = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        out.append(f"[cleanup] child-retarget: unparseable gh output — HOLDING delete of {base_branch}")
        return False
    all_ok = True
    for child in children if isinstance(children, list) else []:
        num = child.get("number") if isinstance(child, dict) else None
        if num is None:
            continue
        try:
            ec = runner(["gh", "pr", "edit", str(num), "--base", default_branch,
                         *_gh_repo_args(gh_repo)])
            if getattr(ec, "returncode", 1) == 0:
                out.append(f"[cleanup] retargeted child PR #{num} onto {default_branch} "
                           f"(before deleting {base_branch})")
            else:
                all_ok = False
                out.append(f"[cleanup] child PR #{num} retarget FAILED ({(ec.stderr or '').strip()}) "
                           f"— will HOLD the delete so #{num} is not auto-closed")
        except (OSError, subprocess.SubprocessError) as e:
            all_ok = False
            out.append(f"[cleanup] child PR #{num} retarget errored ({e}) — will HOLD the delete")
    return all_ok


_SHA_RE = re.compile(r"[0-9a-f]{40}")
_STAT_FILE_LINES = 40


_GIT_TOKEN_RE = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+|AKIA[0-9A-Z]{12,}")


def _fetch_error_text(raw: str) -> str:
    """git's fetch error on one line, safe to print and to keep in a log: a
    remote URL can carry credentials (https://user:token@host/...), so every
    word holding a URL or an `@` is dropped whole, token-shaped strings are
    dropped wherever they sit, non-printable characters are replaced, and the
    result is capped."""
    words = ["<redacted>" if ("://" in w or "@" in w) else _GIT_TOKEN_RE.sub("<redacted>", w)
             for w in raw.split()]
    flat = " ".join(words)
    return "".join(ch if ch.isprintable() else "?" for ch in flat)[:200]


def merge_scope(repo_root: Path, default_branch: str, head_sha: str, pr: int,
                runner: Runner = _run) -> list[str]:
    """#355 — what this merge carries, read BEFORE it lands: the three-dot
    diff-stat (the PR's own change since its merge-base), whether the PR's base
    is behind origin/<default> (the suite ran on a tree without those commits),
    the `git merge-tree` result, and — on a current base — the #322 silent-revert
    sniff at the PR head (the `reset --soft` trap a green suite carries).

    ADVISORY: it never refuses. Every git failure reads as "[scope] unchecked"
    and the merge proceeds on the gate's own verdict — this surfaces scope, it
    is not a new bound.

    A failed `git fetch origin` is REPORTED: the local origin/<default> may be
    stale, so the block opens with a WARN, the merge-tree line is marked, and
    "not behind" reads as unchecked rather than as silence.
    """
    root = str(repo_root)

    def git(*args: str) -> "subprocess.CompletedProcess[str] | None":
        try:
            return runner(["git", "-C", root, *args])
        except (OSError, subprocess.SubprocessError):
            return None

    def out(*args: str) -> str:
        cp = git(*args)
        return (cp.stdout or "").strip() if cp is not None and cp.returncode == 0 else ""

    fetch_timed_out = False
    try:
        fetched = runner(["git", "-C", root, "fetch", "origin", "--quiet"])
    except subprocess.TimeoutExpired:
        fetched, fetch_timed_out = None, True
    except (OSError, subprocess.SubprocessError):
        fetched = None
    stale = fetched is None or fetched.returncode != 0
    # `ref` is the printed name; every git argument takes `gref`. git resolves
    # a short name through refs/tags/ first, so a pushed tag named
    # origin/<default> would otherwise be what this block measures against.
    ref = f"origin/{default_branch}"
    gref = f"refs/remotes/origin/{default_branch}"
    ref_sha = out("rev-parse", "--verify", "--quiet", f"{gref}^{{commit}}")
    head = head_sha.strip().lower()
    if not _SHA_RE.fullmatch(head):
        return [f"[scope] unchecked: PR head {head_sha!r} is not a full SHA"]
    if out("rev-parse", "--verify", "--quiet", f"{head}^{{commit}}") != head:
        git("fetch", "origin", "--quiet", f"pull/{pr}/head")
    if not _SHA_RE.fullmatch(ref_sha):
        return [f"[scope] unchecked: no {ref} in {root} (offline/local-only?)"]
    if out("rev-parse", "--verify", "--quiet", f"{head}^{{commit}}") != head:
        return [f"[scope] unchecked: PR head {head[:7]} is not available locally"]

    lines: list[str] = []
    if stale:
        why = "timed out" if fetch_timed_out else "could not run git" if fetched is None else (
            f"rc={fetched.returncode}: "
            f"{_fetch_error_text(fetched.stderr or fetched.stdout or '') or 'no output'}")
        lines.append(
            f"[scope] WARN git fetch origin FAILED ({why}) — everything below is measured "
            f"against the LOCAL {ref}@{ref_sha[:7]}, which may be stale: commits merged "
            f"since the last good fetch are not counted.")
    stale_note = " (against a possibly STALE ref: fetch failed)" if stale else ""
    stat = out("diff", "--stat", f"{gref}...{head}").splitlines()
    lines.append(f"[scope] git diff --stat {ref}...{head[:7]} (what the PR changes):")
    if not stat:
        lines.append("  (no changes)")
    else:
        files, summary = stat[:-1], stat[-1]
        lines += [f"  {ln.strip()}" for ln in files[:_STAT_FILE_LINES]]
        if len(files) > _STAT_FILE_LINES:
            lines.append(f"  ... +{len(files) - _STAT_FILE_LINES} more file(s)")
        lines.append(f"  {summary.strip()}")

    behind_raw = out("rev-list", "--count", f"{head}..{gref}")
    behind = int(behind_raw) if behind_raw.isdigit() else -1
    if behind > 0:
        lines.append(
            f"[scope] WARN PR base is {behind} commit(s) behind {ref}@{ref_sha[:7]} — the "
            f"reviewed tree and its suite run lack them, and the merge result is a tree "
            f"nobody tested. Restack (net.worktree restack) and re-review, or accept it "
            f"knowingly (see merge-tree below).")
    elif behind < 0:
        lines.append(f"[scope] unchecked: could not count commits behind {ref}")
    elif stale:
        lines.append(f"[scope] unchecked: whether the PR base is behind {ref} (fetch "
                     f"failed; 0 behind the local copy proves nothing)")

    mt = git("merge-tree", "--write-tree", "--name-only", "--no-messages", gref, head)
    if mt is not None and mt.returncode == 0:
        lines.append(f"[scope] merge-tree: clean{stale_note}")
    elif mt is not None and mt.returncode == 1:
        conflicted = [ln.strip() for ln in (mt.stdout or "").splitlines()[1:] if ln.strip()]
        lines.append(f"[scope] merge-tree: CONFLICTS in "
                     f"{', '.join(conflicted) or '(unnamed)'}{stale_note}")
    else:
        lines.append("[scope] merge-tree: unchecked")

    if behind == 0:
        try:
            from review import freshness  # lazy: the guard must load without review/

            def frunner(cmd: list[str], cwd: str | None = None):
                return runner(["git", "-C", root, *cmd[1:]])

            reverts = freshness.sniff_reverts(gref, frunner, None, head=head)
        except Exception as e:  # advisory: a sniff failure never blocks a merge
            lines.append(f"[scope] revert sniff unchecked: {e}")
            reverts = []
        for r in reverts:
            lines.append(f"[scope] WARN REVERTS recent {ref} work: {r.describe()} — the "
                         f"`reset --soft` trap (parent current, tree old); a green suite "
                         f"does not catch it")
    return lines


def _merge_pr_body(
    pr: int,
    repo_root: Path,
    *,
    echo_id: str | None = None,
    method: str = "merge",
    dry_run: bool = False,
    runner: Runner = _run,
    repo: str | None = None,
    emit: "Callable[[str], None] | None" = None,
) -> tuple[bool, str]:
    """#88 — ONE audited merge step, replacing the ~5 hand-assembled commands a
    merge took under the session-auth posture (a sibling pair's 2026-07-17 wish):

        check() -> gh pr merge -> worktree-safe branch cleanup ->
        repin.check sync (deploy) -> post-merge readback.

    It RELAXES NOTHING: `check()` (the full fail-closed gate) runs first and a
    refusal aborts before anything mutating. The wrapper owns the process
    boundary the #85 wiring fix lives at, and it gives the audit log a single
    canonical merge event. `dry_run` runs check() read-only, then prints the
    steps it WOULD take.

    Branch cleanup is a REMOTE delete (`git push origin --delete`) — that is the
    worktree-safe form; `gh pr merge --delete-branch`'s LOCAL delete is the part
    that fails from a per-seat worktree. The repin sync is the step an app-merge
    silently skips (MERGED != deployed); it is loud on failure but non-fatal
    since the merge already landed.

    `emit`, when given, receives the [auth] and [scope] lines on a REAL merge
    BEFORE `gh pr merge` runs, so the operator reads what lands before it
    lands. The returned message still carries every line; a caller that passed
    `emit` prints only what follows the emitted text.
    """
    if method not in MERGE_METHODS:
        return False, f"unknown merge method {method!r}; expected one of {MERGE_METHODS}"
    slug = repo or _repo_slug(repo_root, runner=runner)
    out: list[str] = []

    # 1. AUTHORIZATION — the same gate as `check`, before anything mutating.
    # `authorized` is the head SHA the verdict covered; it is carried to the
    # merge itself, so no later read of the head can widen what merges.
    gh_repo = _repo_identity(repo_root, runner)
    ok, refusal, authorized = _authorize(pr, slug, repo_root, echo_id=echo_id,
                                         runner=runner, gh_repo=gh_repo)
    if not ok:
        assert refusal is not None
        return False, refusal.line()
    exp = _expiry_line(repo_root)
    out.append(f"[auth] OK — PR #{pr} in {slug!r}" + (f" — {exp}" if exp else ""))

    # 1.5 PR FACTS + BASE GUARD (#185). Resolve base/head/title from gh, and
    # REFUSE unless the PR targets the default branch. A stacked/mistargeted PR
    # would 'merge' into an UNPROTECTED branch under the full merge ceremony —
    # success-shaped output for an action that never touched main (the live
    # incident this run). The stacked flow is: retarget first, then merge.
    facts, ferr = gh_pr_facts(pr, runner, gh_repo=gh_repo)
    if facts is None:
        return False, "\n".join(out + [Refusal("pr-unverifiable", ferr).line()])
    default_branch, def_err = _default_branch(runner, gh_repo=gh_repo)
    if not default_branch:
        return False, "\n".join(
            out + [Refusal("base-unverifiable",
                           f"cannot resolve the default branch: {def_err}").line()])
    if facts.base_ref != default_branch:
        return False, "\n".join(out + [Refusal(
            "base-not-default",
            f"PR #{pr} ({facts.title!r}) targets {facts.base_ref!r}, not the default "
            f"branch {default_branch!r} — a merge here would land in an UNPROTECTED "
            f"branch under full merge ceremony. Retarget first: "
            f"gh pr edit {pr} --base {default_branch}, then merge.").line()])
    if facts.head_sha != authorized:
        return False, "\n".join(out + [Refusal(
            "sha-drift",
            f"PR #{pr}'s head moved to {facts.head_sha[:7]} after authorization "
            f"covered {authorized[:7]} — the verdict does not cover this code; "
            f"re-review").line()])
    head_ref = facts.head_ref
    if not head_ref:
        out.append("[warn] head branch unresolved; cleanup will be skipped")

    # 1.6 SCOPE (#355): what this merge carries, read before anything lands.
    try:
        out.extend(merge_scope(repo_root, default_branch, authorized, pr, runner))
    except Exception as e:  # advisory block: never let it break or block a merge
        out.append(f"[scope] unchecked: {e}")

    if dry_run:
        out.append(f"DRY-RUN — auth passed; PR #{pr} ({facts.title!r}) -> {default_branch}; would now:")
        out.append(f"  1. gh pr merge {pr} --{method} --match-head-commit {authorized}")
        out.append(f"  2. retarget open children of {head_ref or '<head-branch>'} onto "
                   f"{default_branch}, then git push origin --delete it (worktree-safe cleanup)")
        out.append("  3. maw-run repin.check sync    (deploy: ff pinned worktree + recopy cache)")
        out.append("  4. maw-run repin.check check   (readback: cache in sync with main)")
        out.append("  5. git -C <repo> fetch origin     (local worktree learns the new main)")
        return True, "\n".join(out)

    if emit is not None:
        try:
            emit("\n".join(out))
        except Exception:  # advisory: a closed stdout never blocks a merge
            pass

    # 2. MERGE, conditioned on the authorized head: GitHub refuses the merge
    # if the head moved after our last read, closing the window we cannot.
    try:
        cp = runner(["gh", "pr", "merge", str(pr), f"--{method}",
                     "--match-head-commit", authorized, *_gh_repo_args(gh_repo)])
    except (OSError, subprocess.SubprocessError) as e:
        return False, "\n".join(out + [f"[merge] FAILED to invoke gh: {e}"])
    if getattr(cp, "returncode", 1) != 0:
        return False, "\n".join(
            out + [f"[merge] gh pr merge {pr} FAILED: {(cp.stderr or cp.stdout or '').strip()}"]
        )
    audit(repo_root, {"ts": _iso(_now_s()), "event": "merge", "pr": pr,
                      "repo": slug, "method": method, "base": default_branch,
                      "sha": authorized})
    out.append(f"[merge] PR #{pr} ({facts.title!r}) merged into {default_branch} (--{method})")

    # 3. BRANCH CLEANUP — first RETARGET open children onto the default branch so
    # deleting this merged branch does not auto-CLOSE a stacked child (#185 / the
    # #181 auto-close hit this run), THEN remote-delete (worktree-safe). The delete
    # is HELD unless we affirmatively know it is safe (list ok AND all children
    # retargeted): a dangling branch is cheaper than an auto-closed child PR.
    if head_ref:
        safe_to_delete = _retarget_children(head_ref, default_branch, out, runner,
                                            gh_repo=gh_repo)
        if not safe_to_delete:
            out.append(f"[cleanup] HELD delete of {head_ref!r} — a child could still "
                       f"base on it. Delete by hand once confirmed clear: "
                       f"git push origin --delete {head_ref}")
        else:
            try:
                dc = runner(["git", "-C", str(repo_root), "push", "origin", "--delete", head_ref])
                if getattr(dc, "returncode", 1) == 0:
                    out.append(f"[cleanup] deleted remote branch {head_ref!r}")
                else:
                    out.append(f"[cleanup] branch {head_ref!r} not deleted "
                               f"(likely auto-deleted on merge): {(dc.stderr or '').strip()}")
            except (OSError, subprocess.SubprocessError) as e:
                out.append(f"[cleanup] skipped ({e})")

    # 4. DEPLOY — repin sync (the step an app-merge silently skips).
    try:
        sc = runner([MAW_RUN, "repin.check", "sync"])
        if getattr(sc, "returncode", 1) == 0:
            out.append("[deploy] repin.check sync OK")
        else:
            out.append(f"[deploy] WARN repin.check sync rc={sc.returncode}: "
                       f"{(sc.stderr or sc.stdout or '').strip()} — run it by hand")
    except (OSError, subprocess.SubprocessError) as e:
        out.append(f"[deploy] WARN repin.check sync failed to invoke: {e}")

    # 5. READBACK + local fetch so the seat's worktree learns the new main.
    try:
        rb = runner([MAW_RUN, "repin.check", "check"])
        rb_out = (rb.stdout or "").strip()
        out.append(f"[readback] {rb_out.splitlines()[-1] if rb_out else 'no output'}")
    except (OSError, subprocess.SubprocessError) as e:
        out.append(f"[readback] WARN could not read back: {e}")
    try:
        runner(["git", "-C", str(repo_root), "fetch", "origin"])
    except (OSError, subprocess.SubprocessError):
        pass

    return True, "\n".join(out)


_TAG_RE = re.compile(r"\[([a-z][a-z0-9-]*)\]")


def _audit_refusal(repo_root: Path, subject: dict, repo: str, dry_run: bool,
                   msg: str, now_s: float | None = None) -> None:
    """Gate 8: a merge rail that refuses silently cannot be proven to have
    worked. `check` always audited its verdict; the merge wrappers returned
    their own refusals (remote-exists, base-not-default, non-ff, a failed
    merge...) with no record (#206)."""
    last = (msg or "").strip().splitlines()[-1] if (msg or "").strip() else ""
    tag = _TAG_RE.search(last)
    now = now_s if now_s is not None else _now_s()
    audit(repo_root, {"ts": _iso(now), "event": "merge-refused", **subject,
                      "repo": repo, "dry_run": dry_run,
                      "bound": tag.group(1) if tag else "unknown",
                      "detail": last[:500]})


def merge_local(
    branch: str,
    repo_root: Path,
    *,
    dry_run: bool = False,
    session_id: str | None = None,
    pid: int | None = None,
    now_s: float | None = None,
    runner: Runner = _run,
    repo: str | None = None,
) -> "tuple[bool, str]":
    slug = repo or _repo_slug(repo_root, runner=runner)
    ok, msg = _merge_local_body(branch, repo_root, dry_run=dry_run,
                                session_id=session_id, pid=pid, now_s=now_s,
                                runner=runner, repo=slug)
    if not ok:
        _audit_refusal(repo_root, {"local_branch": branch, "protection": LOCAL_TAG},
                       slug, dry_run, msg, now_s)
    return ok, msg


def merge_pr(
    pr: int,
    repo_root: Path,
    *,
    echo_id: str | None = None,
    method: str = "merge",
    dry_run: bool = False,
    runner: Runner = _run,
    repo: str | None = None,
    emit: "Callable[[str], None] | None" = None,
) -> tuple[bool, str]:
    slug = repo or _repo_slug(repo_root, runner=runner)
    ok, msg = _merge_pr_body(pr, repo_root, echo_id=echo_id, method=method,
                             dry_run=dry_run, runner=runner, repo=slug, emit=emit)
    if not ok:
        _audit_refusal(repo_root, {"pr": pr}, slug, dry_run, msg)
    return ok, msg


_PR_ARG_RE = re.compile(r"#?[1-9][0-9]*")


def _parse_prs_arg(raw: "str | None") -> "tuple[list[int] | None, str]":
    """Comma-separated PR numbers, each `N` or `#N`, ASCII digits only."""
    if raw is None:
        return None, ""
    parts = [p.strip() for p in raw.split(",")]
    if not parts or not all(p.isascii() and _PR_ARG_RE.fullmatch(p) for p in parts):
        return None, "--prs must be comma-separated PR numbers like 12,#15"
    return [int(p.lstrip("#")) for p in parts], ""


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="guards.session_auth")
    parser.add_argument("--repo-root", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_arm = sub.add_parser(
        "arm", help="one Touch ID (or one session.arm passkey grant) arms this session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("exit codes: 0 armed; 1 refused before or without Touch ID (scope, "
                "seat, grant, ...);\n"
                "  3 declined     the Touch ID sheet was shown and not completed\n"
                "  4 timeout      the prompt timed out unanswered\n"
                "  5 unreachable  the sheet was never shown (sandboxed sudo, no GUI "
                "session,\n                 pam_tid not configured, tmux without "
                "pam_reattach)\n"
                "  6 unprovable   sudo needs no authentication here (NOPASSWD)\n"
                "Every nonzero exit writes no token."))
    p_arm.add_argument("--scope", required=True)
    p_arm.add_argument("--prs", default=None, metavar="N[,N...]",
                       help="enforced PR set; with --grant-file it must equal the "
                            "grant's prs. Without it the scope text is advisory")
    p_arm.add_argument("--grant-file", default=None,
                       help="arm with a session.arm passkey grant instead of Touch ID "
                            "(operator ruling 2026-09-23): it must name this repo, seat, "
                            "session and scope")

    p_amend = sub.add_parser("amend", help="amend the live token's scope with a scope.amend grant "
                                 "(phone passkey, or Touch ID via guards.mint --session-file)")
    p_amend.add_argument("--scope", required=True, help="the NEW scope")
    p_amend.add_argument("--grant-file", required=True)
    p_amend.add_argument("--prs", default=None, metavar="N[,N...]",
                         help="replace the enforced PR set (the grant must carry it)")

    p_narrow = sub.add_parser("narrow", help="shrink the live token's enforced PR set "
                                             "(no grant: narrowing needs no consent)")
    p_narrow.add_argument("--prs", required=True, metavar="N[,N...]")

    p_req = sub.add_parser("grant-request",
                           help="print the unsigned session object + instruction a phone "
                                "arm/amend must sign (JSON)")
    p_req.add_argument("kind", choices=("session.arm", "scope.amend"))
    p_req.add_argument("--scope", required=True)
    p_req.add_argument("--prs", default=None, metavar="N[,N...]",
                       help="enforced PR set to carry in the session object")
    p_req.add_argument("--batch", action="store_true",
                       help="session.arm only: print this seat's line for a one-Face-ID "
                            "batch arm (goal from the run-goal artifact) and record "
                            "that this seat asked")

    p_goal = sub.add_parser("goal-record",
                            help="record this seat's run goal (<= 3 bullets of <= 120 "
                                 "ASCII chars, one per line) for a batch arm")
    p_goal.add_argument("--file", required=True, help="one bullet per non-empty line")

    # NOTE the absent flags. --pr-author / --acting-seat / --signed-verdict were
    # removed deliberately: every one of them let the governed party assert the
    # fact that governs it. They are derived now (gh, the token, the verdict
    # artifact). Do not add them back "for testing" — inject `runner` instead.
    p_check = sub.add_parser("check", help="fail-closed merge authorization")
    p_check.add_argument("--pr", type=int, default=None)
    p_check.add_argument("--local-branch", default=None, metavar="BRANCH",
                         help="authorize a LOCAL branch merge instead of a PR — "
                              "for repos with NO remote (#146). Refuses if any "
                              "remote exists; every such merge is stamped "
                              f"{LOCAL_TAG!r}.")
    p_check.add_argument("--repo", default=None)
    p_check.add_argument("--echo-id", default=None)

    p_verdict = sub.add_parser(
        "record-verdict", help="record a signed verdict, bound to the reviewed SHA"
    )
    p_verdict.add_argument("--pr", type=int, default=None)
    p_verdict.add_argument("--local-branch", default=None, metavar="BRANCH",
                           help="record the verdict against a LOCAL branch head "
                                "instead of a PR (#146, no-remote repos)")
    p_verdict.add_argument("--verdict", required=True,
                           help="approved | changes-requested | ...")
    p_verdict.add_argument("--expect-sha", default=None,
                           help="the head SHA you actually reviewed (>=7 chars); the "
                                "record is REFUSED if the PR's live head differs — "
                                "binds the verdict to the code, not just the PR number (#185)")

    p_echo = sub.add_parser("record-echo", help="persist the operator's literal echo")
    p_echo.add_argument("--prs", required=True, help="comma-separated PR numbers")
    p_echo.add_argument("--response", required=True, help="the operator's verbatim reply")

    p_merge = sub.add_parser(
        "merge",
        help="audited merge: check -> gh merge -> branch cleanup -> repin sync -> readback",
    )
    p_merge.add_argument("--pr", type=int, default=None)
    p_merge.add_argument("--local-branch", default=None, metavar="BRANCH",
                         help="fast-forward a LOCAL branch into the default branch "
                              "under the same armed-session bounds (#146). Refuses "
                              "if any remote exists.")
    p_merge.add_argument("--repo", default=None)
    p_merge.add_argument("--echo-id", default=None)
    p_merge.add_argument("--method", default="merge", choices=MERGE_METHODS)
    p_merge.add_argument("--dry-run", action="store_true",
                         help="run check() read-only, then print the steps it would take")

    sub.add_parser(
        "status",
        help="report whether a LIVE armed token exists (scope + expiry), or why "
             "not — a stale token from a dead session reads as NOT ARMED",
    )

    sub.add_parser(
        "peer-status",
        help="EXECUTOR: has this repo's registered advisor armed? (read-only; "
             "exit 0 = armed by that seat's own process)",
    )

    p_verdicts = sub.add_parser(
        "verdicts", help="list recorded verdicts + their bound SHAs (read-only)"
    )
    p_verdicts.add_argument("--pr", type=int, default=None,
                            help="filter to one PR (default: all)")

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else default_repo_root()

    # --pr and --local-branch pick the RAIL. Exactly one, enforced here so no
    # verb can forget it: a merge that silently picked a rail would be the worst
    # possible ambiguity on this surface.
    if args.cmd in ("check", "record-verdict", "merge"):
        has_pr = getattr(args, "pr", None) is not None
        has_local = bool(getattr(args, "local_branch", None))
        if has_pr and has_local:
            parser.error("--pr and --local-branch name different rails; pass one")
        if not has_pr and not has_local:
            parser.error("pass --pr N (the PR rail) or --local-branch NAME "
                         "(no-remote repos, #146)")

    if args.cmd == "arm":
        grant = None
        if args.grant_file:
            grant, why = _read_grant_file(args.grant_file)
            if grant is None:
                audit(repo_root, {"ts": _iso(_now_s()), "event": "arm-refused",
                                  "via": "grant", "grant_id": "", "reason": why})
                print(f"refusing to arm: {why}", file=sys.stderr)
                return DENY
        prs, bad = _parse_prs_arg(args.prs)
        if bad:
            print(f"refusing to arm: {bad}", file=sys.stderr)
            return DENY
        ok, msg = arm(args.scope, repo_root, grant=grant, prs=prs)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return PASS if ok else getattr(msg, "exit_code", DENY)

    if args.cmd == "amend":
        grant, why = _read_grant_file(args.grant_file)
        if grant is None:
            audit(repo_root, {"ts": _iso(_now_s()), "event": "amend-refused",
                              "grant_id": "", "reason": why})
            print(f"refusing to amend: {why}", file=sys.stderr)
            return DENY
        prs, bad = _parse_prs_arg(args.prs)
        if bad:
            print(f"refusing to amend: {bad}", file=sys.stderr)
            return DENY
        ok, msg = amend(args.scope, repo_root, grant, prs=prs)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return PASS if ok else DENY

    if args.cmd == "goal-record":
        try:
            bullets = Path(args.file).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            print(f"refusing to record the run goal: {e}", file=sys.stderr)
            return DENY
        ok, msg = record_goal(repo_root, bullets)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return PASS if ok else DENY

    if args.cmd == "grant-request":
        prs, bad = _parse_prs_arg(args.prs)
        if bad:
            print(f"grant-request refused: {bad}", file=sys.stderr)
            return DENY
        if args.batch:
            if args.kind != "session.arm":
                print("grant-request refused: --batch is for session.arm only",
                      file=sys.stderr)
                return DENY
            line, why = batch_request(args.scope, repo_root, prs=prs)
            if line is None:
                print(f"grant-request refused: {why}", file=sys.stderr)
                return DENY
            _record_arm_wait(str(line.get("session_id", "")),
                             str(line.get("repo", "")), repo_root)
            print(json.dumps({"line": line}, indent=2))
            return PASS
        req, why = session_request(args.kind, args.scope, repo_root, prs=prs)
        if req is None:
            print(f"grant-request refused: {why}", file=sys.stderr)
            return DENY
        if args.kind == "session.arm":
            sess = req.get("session", {})
            _record_arm_wait(str(sess.get("session_id", "")),
                             str(sess.get("repo", "")), repo_root)
        print(json.dumps(req, indent=2))
        return PASS

    if args.cmd == "narrow":
        prs, bad = _parse_prs_arg(args.prs)
        if bad or prs is None:
            print(f"refusing to narrow: {bad or '--prs is required'}", file=sys.stderr)
            return DENY
        ok, msg = narrow(repo_root, prs)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return PASS if ok else DENY

    if args.cmd == "peer-status":
        ok, msg = peer_status(registry.git_common_dir(), registry.who())
        print(msg)
        return PASS if ok else DENY

    if args.cmd == "status":
        # A report, not a gate: the line always goes to stdout (armed OR not),
        # and the exit code doubles as a script predicate.
        ok, msg = status(repo_root)
        print(msg)
        return PASS if ok else DENY

    if args.cmd == "verdicts":
        rows = list_verdicts(repo_root, args.pr)
        if not rows:
            scope = f" for PR #{args.pr}" if args.pr is not None else ""
            print(f"no recorded verdicts{scope}")
            return PASS
        for r in rows:
            sha = str(r.get("sha", ""))[:7]
            print(f"PR #{r.get('pr')}  {sha}  {r.get('verdict')}  "
                  f"by {r.get('seat')}  {r.get('ts')}")
        return PASS

    if args.cmd == "record-echo":
        prs = [int(x) for x in args.prs.split(",") if x.strip()]
        _id, msg = record_echo(repo_root, prs, args.response)
        print(msg)
        return PASS

    if args.cmd == "record-verdict":
        if args.local_branch:
            ok, msg = record_verdict_local(repo_root, args.local_branch,
                                           args.verdict,
                                           expect_sha=args.expect_sha)
        else:
            ok, msg = record_verdict(repo_root, args.pr, args.verdict,
                                     expect_sha=args.expect_sha)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return PASS if ok else DENY

    if args.cmd == "merge":
        if args.local_branch:
            ok, msg = merge_local(args.local_branch, repo_root,
                                  dry_run=args.dry_run, repo=args.repo)
        else:
            shown: list[str] = []

            def emit(text: str) -> None:
                # flush: a piped stdout is block-buffered, and the point is
                # that these lines are out before gh pr merge runs.
                print(text, flush=True)
                shown.append(text)

            ok, msg = merge_pr(
                args.pr, repo_root, echo_id=args.echo_id, method=args.method,
                dry_run=args.dry_run, repo=args.repo, emit=emit,
            )
            if shown and msg.startswith(shown[0]):
                msg = msg[len(shown[0]):].lstrip("\n")
        if msg:
            print(msg, file=sys.stdout if ok else sys.stderr)
        return PASS if ok else DENY

    repo = args.repo or _repo_slug(repo_root)
    if args.local_branch:
        ok, refusal = check_local(args.local_branch, repo, repo_root)
        subject = {"local_branch": args.local_branch, "protection": LOCAL_TAG}
        label = f"local branch {args.local_branch!r}"
    else:
        ok, refusal = check(
            pr=args.pr,
            repo=repo,
            repo_root=repo_root,
            echo_id=args.echo_id,
        )
        subject = {"pr": args.pr}
        label = f"PR #{args.pr}"
    rec = {
        "ts": _iso(_now_s()), "event": "check", **subject, "repo": repo,
        "result": "pass" if ok else "refuse",
        "bound": None if ok else refusal.bound,
    }
    audit(repo_root, rec)
    if ok:
        print(f"merge auth OK — {label} in {repo}")
        exp = _expiry_line(repo_root)
        if exp:
            print(exp)
        return PASS
    print(refusal.line(), file=sys.stderr)
    return DENY


if __name__ == "__main__":
    raise SystemExit(main())
