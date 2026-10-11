"""Fleet-board slot protocol — the maw seat's half (#156).

The advisor (the pair PRINCIPAL) reports its run to the operator's fleet board at
real events: CLAIM the repo's slot at /goal-confirm, UPDATE at each merge, WRAP
at session close. Sessions write DATA fragments; the board's renderer owns HTML
(state/board/SCHEMA.md).

Design (the operator/CoS-ruled 2026-07-21):
  - src="seat" on EVERY write — the provenance flip is the crux (a seat's own
    report is ground truth vs a CoS point-in-time pane glance).
  - Always send the four board.sh REQUIRES (name, kind, status, headline) or the
    write is silently lost under fire-and-forget. build_fragment guarantees them.
  - name = the repo slug (one slot per repo).
  - armed_until / goal_armed_at come from guards.session_auth's OWN token loader
    (single source of truth), never a hand-read of the token file.
  - FIRE-AND-FORGET: board.sh absent / non-zero / any error → silent no-op that
    NEVER raises and NEVER blocks repo work (the SCHEMA failure posture). The CoS
    pane glance is the labeled fallback when a seat doesn't report.

The board is the operator's, not maw's: its script and fragment dir come from
`opconf` (board_sh / board_dir: env MAW_BOARD_SH / MAW_BOARD_DIR, then
~/.maw/operator.toml). With neither configured the board is off and every write
is a no-op. Both are also injectable, so the whole assemble/merge path is
CI-testable with no board present.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import opconf

_REQUIRED = ("name", "kind", "status", "headline")  # board.sh rejects a write missing any

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _board_sh() -> "Path | None":
    return opconf.get_path("board_sh")


def _board_dir() -> "Path | None":
    return opconf.get_path("board_dir")


def _run(cmd: list[str], stdin_text: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=15)


def _now_utc() -> str:
    """The progress-event timestamp (UTC, Z-suffixed). Its own function so a test
    can pin it — the F10 progress-age render needs a stampable 'now'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _candidate_roots(repo_root: str | None = None) -> list[Path]:
    """Roots to check for a live arm. The arm may have run from a subdir (this
    repo runs the pair from `maw/`, so the token can live at <root>/maw/.maw/ or
    <root>/.maw/), and the board command's cwd need not match — so check the hint,
    cwd, the git toplevel, and the git-toplevel/maw subdir, deduped in order."""
    roots: list[Path] = []
    if repo_root:
        roots.append(Path(repo_root))
    roots.append(Path.cwd())
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            r = Path(top.stdout.strip())
            roots.append(r)
            roots.append(r / "maw")
    except Exception:  # noqa: BLE001
        pass
    out: list[Path] = []
    seen: set = set()
    for r in roots:
        try:
            rr = r.resolve()
        except Exception:  # noqa: BLE001
            rr = r
        if rr not in seen:
            seen.add(rr)
            out.append(rr)
    return out


def _arm_fields(repo_root: str | None) -> tuple[str | None, str | None]:
    """(goal_armed_at, armed_until) from a LIVE session_auth token.

    Uses session_auth's VALIDATED status() — shape + time window + arming-pid
    liveness — so a stale/dead-session or expired token that session_auth would
    refuse NEVER yields an armed_until (the display would otherwise show a live
    countdown for a dead arm, the #156 gate-10 defect). Because the arm may live
    under a different root than the board command's cwd, each candidate root is
    checked and the FIRST that status() reports LIVE wins. Returns (None, None)
    when nothing is live — the renderer then shows 'not armed'."""
    try:
        from guards import session_auth
        for root in _candidate_roots(repo_root):
            try:
                ok, _line = session_auth.status(root)
            except Exception:  # noqa: BLE001
                continue
            if ok:
                token, _refusal = session_auth._load_token(root)
                if token:
                    return token.get("armed_at"), token.get("expires_at")
    except Exception:  # noqa: BLE001 — arm state is best-effort, never fatal
        pass
    return None, None


def _repo_slug(cwd: str | None = None) -> str:
    """The repo slug that keys the slot (one slot per repo)."""
    try:
        from net import worktree
        return worktree.logical_repo_slug(cwd)
    except Exception:  # noqa: BLE001
        return Path(cwd or Path.cwd()).name


def build_fragment(
    *,
    name: str,
    status: str,
    headline: str,
    kind: str = "pair",
    detail: str = "",
    goal_plain: str | None = None,
    goal_armed_at: str | None = None,
    armed_until: str | None = None,
    shipped: "list[dict] | None" = None,
    last_progress_at: str | None = None,
    last_evidence_ref: str | None = None,
) -> dict:
    """Assemble a board fragment with src='seat' and the four required keys always
    present. Optional keys are included only when set (a bare fragment still
    passes board.sh's required-field gate).

    last_progress_at / last_evidence_ref carry PROGRESS AGE (#149 F10 addendum 2):
    the timestamp + reference of the last REAL work event (run start, merge), so
    the board can render 'evidence 47s ago' vs '4h ago' — a stuck seat and a
    productive seat no longer render identically off fragment freshness alone."""
    frag: dict = {
        "name": name,
        "kind": kind,
        "status": status,
        "src": "seat",       # the provenance flip — always a seat self-report here
        "headline": headline,
    }
    if detail:
        frag["detail"] = detail
    if goal_plain:
        frag["goal_plain"] = goal_plain
    if goal_armed_at:
        frag["goal_armed_at"] = goal_armed_at
    if armed_until:
        frag["armed_until"] = armed_until
    if shipped is not None:
        frag["shipped"] = shipped
    if last_progress_at:
        frag["last_progress_at"] = last_progress_at
    if last_evidence_ref:
        frag["last_evidence_ref"] = last_evidence_ref
    return frag


def _existing(name: str, board_dir: "Path | None" = None) -> dict:
    """The current fragment for `name` (to accumulate shipped / carry goal_plain),
    or {} if absent/unreadable."""
    d = board_dir if board_dir is not None else _board_dir()
    if d is None:
        return {}
    f = d / f"{name}.json"
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def write(fragment: dict, *, runner: Runner = _run, board_sh: "Path | None" = None) -> bool:
    """Pipe a fragment to `board.sh update <name>`. FIRE-AND-FORGET: returns True
    on a clean write, False on ANY failure (absent script, non-zero, missing
    required field, timeout) — never raises, never blocks."""
    for req in _REQUIRED:
        if not fragment.get(req):
            return False  # board.sh would reject it; don't even try
    sh = board_sh if board_sh is not None else _board_sh()
    if sh is None or not sh.exists():
        return False
    try:
        cp = runner([str(sh), "update", fragment["name"]], json.dumps(fragment))
        return cp.returncode == 0
    except Exception:  # noqa: BLE001 — fire-and-forget: a board failure never blocks work
        return False


def claim(
    *,
    name: str | None = None,
    goal_plain: str,
    kind: str = "pair",
    evidence_ref: str | None = None,
    repo_root: str | None = None,
    runner: Runner = _run,
    board_sh: "Path | None" = None,
) -> bool:
    """CLAIM the repo's slot at /goal-confirm: a fresh working fragment with the
    run's plain-language goal and the armed window. Resets shipped (new run) and
    anchors progress-age at the run start (last_progress_at=now); evidence_ref is
    the optional starting anchor (e.g. the base SHA)."""
    slot = name or _repo_slug(repo_root)
    armed_at, armed_until = _arm_fields(repo_root)
    frag = build_fragment(
        name=slot, kind=kind, status="working",
        headline=f"Run started: {goal_plain}",
        goal_plain=goal_plain, goal_armed_at=armed_at, armed_until=armed_until,
        shipped=[],
        last_progress_at=_now_utc(), last_evidence_ref=evidence_ref,
    )
    return write(frag, runner=runner, board_sh=board_sh)


def update(
    *,
    name: str | None = None,
    headline: str,
    what: str | None = None,
    proof: str | None = None,
    detail: str = "",
    status: str = "working",
    evidence_ref: str | None = None,
    repo_root: str | None = None,
    runner: Runner = _run,
    board_sh: "Path | None" = None,
    board_dir: "Path | None" = None,
) -> bool:
    """UPDATE at each merge: a plain-language headline + an appended shipped entry
    (what + proof). Carries the existing goal_plain/kind and refreshes armed_until.

    Progress-age (#149 F10): a REAL work event (a shipped entry via what/proof, or
    an explicit evidence_ref) re-stamps last_progress_at=now and records the
    evidence; a bare status ping (no what/proof/evidence_ref) CARRIES FORWARD the
    prior progress stamp — so a seat that only pings 'still working' does NOT
    reset its progress age, and a stall stays derivable."""
    slot = name or _repo_slug(repo_root)
    prev = _existing(slot, board_dir)
    shipped = list(prev.get("shipped") or [])
    if what:
        shipped.append({"what": what, "proof": proof or ""})
    _armed_at, armed_until = _arm_fields(repo_root)
    is_progress = bool(what or proof or evidence_ref)
    if is_progress:
        last_progress_at = _now_utc()
        last_evidence_ref = evidence_ref or proof or what
    else:
        last_progress_at = prev.get("last_progress_at")
        last_evidence_ref = prev.get("last_evidence_ref")
    frag = build_fragment(
        name=slot, kind=prev.get("kind", "pair"), status=status,
        headline=headline, detail=detail,
        goal_plain=prev.get("goal_plain"),
        goal_armed_at=prev.get("goal_armed_at"), armed_until=armed_until,
        shipped=shipped,
        last_progress_at=last_progress_at, last_evidence_ref=last_evidence_ref,
    )
    return write(frag, runner=runner, board_sh=board_sh)


def wrap(
    *,
    name: str | None = None,
    headline: str,
    detail: str = "",
    repo_root: str | None = None,
    runner: Runner = _run,
    board_sh: "Path | None" = None,
    board_dir: "Path | None" = None,
) -> bool:
    """WRAP at session close: status='wrapped' frees the slot, keeping the run's
    shipped history and goal_plain for the final render. A wrap is not new
    progress — it CARRIES FORWARD the last real progress stamp so the final render
    reflects the last merge, not the close."""
    slot = name or _repo_slug(repo_root)
    prev = _existing(slot, board_dir)
    frag = build_fragment(
        name=slot, kind=prev.get("kind", "pair"), status="wrapped",
        headline=headline, detail=detail,
        goal_plain=prev.get("goal_plain"),
        shipped=prev.get("shipped"),
        last_progress_at=prev.get("last_progress_at"),
        last_evidence_ref=prev.get("last_evidence_ref"),
    )
    return write(frag, runner=runner, board_sh=board_sh)


# --- CLI ---------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="net.board",
        description="fleet-board slot protocol (fire-and-forget; #156)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("claim", help="claim the repo slot at /goal-confirm")
    c.add_argument("--name", default=None, help="slot name (default: repo slug)")
    c.add_argument("--goal", required=True, help="one plain-language sentence")
    c.add_argument("--kind", default="pair")
    c.add_argument("--evidence-ref", default=None,
                   help="optional starting progress anchor (e.g. the base SHA)")

    u = sub.add_parser("update", help="update the slot at a merge")
    u.add_argument("--name", default=None)
    u.add_argument("--headline", required=True)
    u.add_argument("--what", default=None, help="a shipped-entry summary")
    u.add_argument("--proof", default=None, help="proof for the shipped entry (SHA/suite)")
    u.add_argument("--detail", default="")
    u.add_argument("--status", default="working")
    u.add_argument("--evidence-ref", default=None,
                   help="progress evidence (SHA/suite/merge); re-stamps progress-age. "
                        "Falls back to --proof/--what when omitted")

    w = sub.add_parser("wrap", help="wrap the slot at session close")
    w.add_argument("--name", default=None)
    w.add_argument("--headline", required=True)
    w.add_argument("--detail", default="")

    ns = p.parse_args(argv)
    if ns.cmd == "claim":
        ok = claim(name=ns.name, goal_plain=ns.goal, kind=ns.kind,
                   evidence_ref=ns.evidence_ref)
    elif ns.cmd == "update":
        ok = update(name=ns.name, headline=ns.headline, what=ns.what,
                    proof=ns.proof, detail=ns.detail, status=ns.status,
                    evidence_ref=ns.evidence_ref)
    else:
        ok = wrap(name=ns.name, headline=ns.headline, detail=ns.detail)
    # Fire-and-forget: a board miss is NEVER a work-blocking error. Report on
    # stderr for visibility, but always exit 0 so a seat script never halts on it.
    if not ok:
        print("board: write skipped (board.sh absent or failed) — continuing",
              file=__import__("sys").stderr)
    else:
        slug = ns.name or _repo_slug()
        print(f"board: {slug} updated", file=__import__("sys").stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
