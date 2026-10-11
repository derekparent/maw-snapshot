"""Bridge-as-source-of-truth for the wake line (backlog item 5).

The switchboard wake (transport.py) is fire-and-forget: a pushed turn used to
be BORN CONSUMED because the tmux paste "already carried it" into the peer's
TUI. If the peer's pane was busy/modal/crashed, the body was LOST — `recv`
could never recover it. Under the median-model principle that is a real hole:
the Phase-3 advisor /loop drains the bridge, so a lost executor post corrupts a
verdict.

This module makes the durable bridge (bridge/format.py — base64+sha256,
ordered, flock'd) the SOURCE OF TRUTH for every inter-seat message. `net send`
writes a bridge turn FIRST; the wake then carries only a POINTER. A wake
failure can no longer lose the message — it is already durable on the bridge.

Keying — the load-bearing decision (two different keys, deliberately):

  * The BRIDGE is per LOGICAL repo: repo_root -> slug ->
    ~/.claude/bridge/<slug>/from-<role>.md. A pair (advisor + executor) of one
    repo SHARES that bridge dir; the sender writes from-<sender_role>.md, which
    the recipient reads as its own "from-<other>.md".
  * The SWITCHBOARD wake is per MAW_NET_DIR (the registry of who is live and
    which tmux pane to paste into).

So send->turn and recv<-turn align only when the two seats are the advisor +
executor pair of ONE logical repo. We derive the shared slug from the SENDER's
registry entry (its repo_root / repo_common_dir), reusing the bridge's own slug
resolver so bridge identity and ours cannot diverge. When the sender/recipient
are NOT that same-repo advisor/executor pair (cross-repo, or a third name like
'planner'), there is no shared bridge to land in — `eligible_pair` returns
None and the caller falls back to the legacy wire-only path.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bridge.format import (
    Turn,
    find_unconsumed_oldest,
    make_turn,
    mark_consumed,
    parse_turns,
)
from bridge.livemodel import live_model_for_cwd
from net.registry import Entry, same_repo

BRIDGE_ROLES = ("advisor", "executor")


def _other_role(role: str) -> str:
    return "executor" if role == "advisor" else "advisor"


def _bridge_dir(slug: str) -> Path:
    # Mirror bridge.cli._bridge_dir (KD12 namespacing). Re-derived here rather
    # than imported so a Home/$HOME monkeypatch in either module's tests can't
    # bind the path at import time.
    return Path.home() / ".claude" / "bridge" / slug


def _shared_slug(entry: Entry) -> str:
    """The bridge slug shared by the pair, from the entry's repo identity.

    Single source of slug truth: delegates to bridge.cli._resolve_repo_slug (the
    SAME resolver /maw:bridge-in uses) so send / net recv / bridge-in cannot
    diverge on a worktree pair. That resolver honors .maw/config.yaml
    `repo_slug:` first, else the worktree-aware logical-repo basename.

    We prefer the AUTHORITATIVE shared-repo identity the registry recorded
    (repo_common_dir — the one shared .git for every seat of one logical repo)
    over a per-seat re-derivation off repo_root, so a per-seat config.yaml
    cannot fork the shared bridge: the common dir's parent basename is the same
    for both seats regardless of which worktree they sit in.
    """
    from bridge.cli import _resolve_repo_slug

    if entry.repo_common_dir:
        common = Path(entry.repo_common_dir)
        logical = common.parent if common.name == ".git" else common
        return _resolve_repo_slug(logical)
    repo_root = Path(entry.repo_root) if entry.repo_root else Path.cwd()
    return _resolve_repo_slug(repo_root)


@dataclass(frozen=True)
class PairLink:
    """A resolved durable channel for one sender->recipient send.

    file: the bridge file the sender WRITES (from-<sender_role>.md) and that the
    recipient READS as its inbound. sender_role/recipient_role are the bridge
    roles (advisor|executor). slug is the shared logical-repo slug.
    """

    slug: str
    sender_role: str
    recipient_role: str
    file: Path


def eligible_pair(sender: Entry, recipient: Entry) -> PairLink | None:
    """Resolve the shared durable bridge channel, or None if there isn't one.

    Durable-by-default applies only to the advisor/executor pair of ONE logical
    repo — that is the only configuration with a shared bridge dir whose
    from-<role>.md files line up for both `net recv` and `/maw:bridge-in`.
    Returns None (caller falls back to the legacy wire-only path) when:
      - either role is not one of the two bridge roles, or
      - the two roles are not the advisor+executor complement, or
      - the two seats are not the same logical repo.
    """
    s_role, r_role = sender.role, recipient.role
    if s_role not in BRIDGE_ROLES or r_role not in BRIDGE_ROLES:
        return None
    if r_role != _other_role(s_role):
        return None
    if not same_repo(sender, recipient):
        return None
    slug = _shared_slug(sender)
    file = _bridge_dir(slug) / f"from-{s_role}.md"
    return PairLink(slug=slug, sender_role=s_role, recipient_role=r_role, file=file)


def _next_turn_id(file: Path) -> int:
    highest = 0
    for t in parse_turns(file):
        if t.turn_id > highest:
            highest = t.turn_id
    return highest + 1


def write_durable_turn(link: PairLink, sender: Entry, body: str, grant_b64: str = "") -> Turn:
    """Append the body as a durable bridge turn (the source of truth).

    Reuses bridge/format directly (base64 + sha256 + flock'd append). An outer
    .idlock makes read-max-id + append atomic, matching bridge.cli.cmd_out and
    net.wire — without it two concurrent sends to the same from-<role>.md can
    mint the same turn_id. The turn is born UNCONSUMED: it is consumed only when
    the recipient actually pulls it (net recv / bridge-in), which is what makes
    consumed_at an honest read receipt instead of a born-consumed fiction.
    """
    from bridge.format import _acquire_lock, _release_lock, append_turn

    link.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    id_lock = _acquire_lock(link.file.parent / ".idlock")
    try:
        # B2: stamp the model that ACTUALLY produced this turn — live-first
        # (the sender's own transcript), pinned model only as a fallback.
        model = live_model_for_cwd(sender.cwd) or sender.model
        turn = make_turn(
            turn_id=_next_turn_id(link.file),
            role=link.sender_role,
            session_id=sender.session_id,
            body=body,
            model=model,
            grant=grant_b64,
        )
        append_turn(link.file, turn)
    finally:
        _release_lock(id_lock)
    return turn


def inbound_file(recipient: Entry, sender_role: str) -> Path:
    """The bridge file a recipient pulls from for a given sender role."""
    slug = _shared_slug_for(recipient)
    return _bridge_dir(slug) / f"from-{sender_role}.md"


def _shared_slug_for(entry: Entry) -> str:
    """Slug from any entry's own repo identity (recipient side of recv)."""
    return _shared_slug(entry)


def next_inbound(recipient: Entry) -> tuple[Path, Turn] | None:
    """Oldest unconsumed durable turn addressed to `recipient`, or None.

    The recipient reads from-<other_role>.md (the sender's file). FIFO,
    oldest-first, matching /maw:bridge-in's default selector so a peer draining
    via either surface sees the same order. Does NOT consume — caller renders
    then calls consume_inbound.
    """
    if recipient.role not in BRIDGE_ROLES:
        return None
    other = _other_role(recipient.role)
    file = inbound_file(recipient, other)
    if not file.exists():
        return None
    turn = find_unconsumed_oldest(file)
    if turn is None:
        return None
    return file, turn


def inbound_turn_ids(recipient: Entry) -> list[int]:
    """The distinct unconsumed durable turn ids waiting for `recipient`,
    oldest first.

    Read-only. Revisions of one turn_id count once (matching what a drain
    delivers). Lets `net recv --check` name the waiting turns, so a watcher
    can tell "a new turn landed" from "I consumed one" (#347): a count alone
    drops on the seat's own consume and stays level when one turn is consumed
    and another lands in the same poll.
    """
    if recipient.role not in BRIDGE_ROLES:
        return []
    file = inbound_file(recipient, _other_role(recipient.role))
    if not file.exists():
        return []
    return sorted({t.turn_id for t in parse_turns(file) if t.consumed_at is None})


def count_inbound(recipient: Entry) -> int:
    """How many distinct unconsumed durable turns wait for `recipient`."""
    return len(inbound_turn_ids(recipient))


def consume_inbound(file: Path, turn: Turn) -> bool:
    """Mark ALL unconsumed revisions of a pulled turn consumed.

    A /maw:bridge-out --revise'd turn carries revision>1 of the SAME turn_id.
    find_unconsumed_oldest returns the highest revision, but marking ONLY that
    revision leaves the superseded rev-1 block unconsumed — so a re-query in
    the recv loop would re-emit the stale body (double-delivery). Consuming the
    whole turn_id once treats a turn_id as fully delivered the moment any
    revision is pulled, which is what 'delivered exactly once (latest)' means.

    Returns True if at least one block was marked consumed, False if none were
    (e.g. a concurrent recv already drained it) so the caller can stop instead
    of re-fetching the same oldest turn.
    """
    marked_any = False
    # mark_consumed targets the highest unconsumed revision per call; loop to
    # sweep every remaining revision of this turn_id.
    while mark_consumed(file, turn_id=turn.turn_id):
        marked_any = True
    return marked_any


def sent_durable(sender: Entry) -> list[Turn]:
    """All durable turns this sender wrote, oldest-first (the receipt source).

    consumed_at on these is the unambiguous 'did they get it' receipt: it is
    set only when the recipient pulls the turn, never at send time.
    """
    if sender.role not in BRIDGE_ROLES:
        return []
    slug = _shared_slug(sender)
    file = _bridge_dir(slug) / f"from-{sender.role}.md"
    if not file.exists():
        return []
    return sorted(parse_turns(file), key=lambda t: t.turn_id)
