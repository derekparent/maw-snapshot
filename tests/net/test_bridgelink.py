"""Tests for net.bridgelink — bridge-as-source-of-truth keying (item 5).

The load-bearing decision under test: durable-by-default applies ONLY to the
advisor/executor pair of ONE logical repo (the only config with a shared bridge
dir whose from-<role>.md files line up for send and recv). Everything else
(cross-repo, non-pair roles) returns None so the caller falls back to the legacy
wire-only path.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

import json  # noqa: E402

from bridge import livemodel  # noqa: E402
from bridge.format import parse_turns  # noqa: E402
from net import bridgelink, registry  # noqa: E402


def _entry(name: str, role: str, repo_root: str, *, common: str | None = None) -> registry.Entry:
    now = "2026-06-12T00:00:00Z"
    return registry.Entry(
        name=name, role=role, repo_root=repo_root, cwd=repo_root, pid=os.getpid(),
        tty=None, tmux_pane="%1", session_id=f"{name}-1", started_at=now,
        last_seen=now, transport={"tmux_push": True},
        repo_common_dir=(common if common is not None else repo_root + "/.git"),
    )


class EligiblePairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".maw").mkdir(parents=True)
        (self.repo / ".maw" / "config.yaml").write_text("repo_slug: shared-slug\n")
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_advisor_executor_same_repo_is_eligible(self) -> None:
        ex = _entry("executor", "executor", str(self.repo))
        adv = _entry("advisor", "advisor", str(self.repo))
        link = bridgelink.eligible_pair(ex, adv)
        self.assertIsNotNone(link)
        # Sender writes from-<sender_role>.md under the SHARED slug.
        self.assertEqual(link.slug, "shared-slug")
        self.assertTrue(str(link.file).endswith("shared-slug/from-executor.md"))

    def test_non_pair_third_role_not_eligible(self) -> None:
        # A 'planner' (not a bridge role) has no shared bridge channel.
        ex = _entry("executor", "executor", str(self.repo))
        planner = _entry("planner", "planner", str(self.repo))
        self.assertIsNone(bridgelink.eligible_pair(ex, planner))
        self.assertIsNone(bridgelink.eligible_pair(planner, ex))

    def test_same_role_not_eligible(self) -> None:
        # Two executors are not a complement — no from-advisor.md alignment.
        a = _entry("executor", "executor", str(self.repo))
        b = _entry("executor2", "executor", str(self.repo))
        self.assertIsNone(bridgelink.eligible_pair(a, b))

    def test_cross_repo_pair_not_eligible(self) -> None:
        # Same roles, DIFFERENT logical repos: no shared bridge dir.
        other_repo = Path(self.tmp.name) / "other"
        (other_repo / ".maw").mkdir(parents=True)
        ex = _entry("executor", "executor", str(self.repo))
        adv = _entry("advisor", "advisor", str(other_repo))
        self.assertIsNone(bridgelink.eligible_pair(ex, adv))

    def test_worktree_pair_shares_logical_slug(self) -> None:
        # Two per-seat worktrees (distinct repo_root basenames) of ONE logical
        # repo must resolve the SAME slug via the shared .git basename, not the
        # worktree's own basename. Use a config-free logical root so this hits
        # the basename-fallback path (a config'd root is covered separately).
        logical = Path(self.tmp.name) / "logrepo"
        logical.mkdir()
        common = str(logical) + "/.git"
        wt_ex = Path(self.tmp.name) / "logrepo-executor"
        wt_adv = Path(self.tmp.name) / "logrepo-advisor"
        wt_ex.mkdir()
        wt_adv.mkdir()
        ex = _entry("executor", "executor", str(wt_ex), common=common)
        adv = _entry("advisor", "advisor", str(wt_adv), common=common)
        link = bridgelink.eligible_pair(ex, adv)
        self.assertIsNotNone(link)
        # No .maw/config.yaml on the logical root -> slug falls to the LOGICAL
        # repo basename ('logrepo'), not the worktree basename ('logrepo-executor').
        self.assertEqual(link.slug, "logrepo")

    def test_worktree_pair_uses_main_root_config_slug(self) -> None:
        # MAJOR 3 / related minor: a per-seat config must NOT fork the bridge.
        # The slug is keyed off the AUTHORITATIVE shared identity (the main
        # worktree's .maw/config.yaml, reached via repo_common_dir's parent),
        # NOT a per-worktree config. Both seats resolve the main root's slug
        # even if one worktree carries a divergent (stale) repo_slug.
        common = str(self.repo) + "/.git"  # self.repo has repo_slug: shared-slug
        wt_ex = Path(self.tmp.name) / "repo-executor"
        wt_adv = Path(self.tmp.name) / "repo-advisor"
        (wt_ex / ".maw").mkdir(parents=True)
        # A divergent per-seat config that MUST be ignored in favor of the
        # shared main-root identity.
        (wt_ex / ".maw" / "config.yaml").write_text("repo_slug: forked-by-seat\n")
        wt_adv.mkdir()
        ex = _entry("executor", "executor", str(wt_ex), common=common)
        adv = _entry("advisor", "advisor", str(wt_adv), common=common)
        link_ex = bridgelink.eligible_pair(ex, adv)
        link_adv = bridgelink.eligible_pair(adv, ex)
        self.assertEqual(link_ex.slug, "shared-slug")
        self.assertEqual(link_adv.slug, "shared-slug")  # both seats agree


class DurableRoundtripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".maw").mkdir(parents=True)
        (self.repo / ".maw" / "config.yaml").write_text("repo_slug: rt\n")
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.ex = _entry("executor", "executor", str(self.repo))
        self.adv = _entry("advisor", "advisor", str(self.repo))

    def test_write_then_recipient_pulls_and_consumes(self) -> None:
        link = bridgelink.eligible_pair(self.ex, self.adv)
        t = bridgelink.write_durable_turn(link, self.ex, "hello advisor")
        self.assertEqual(t.turn_id, 1)
        self.assertIsNone(t.consumed_at)  # born unconsumed

        nxt = bridgelink.next_inbound(self.adv)
        self.assertIsNotNone(nxt)
        bfile, bturn = nxt
        self.assertEqual(bturn.body, "hello advisor")
        self.assertEqual(bturn.role, "executor")

        self.assertTrue(bridgelink.consume_inbound(bfile, bturn))
        # Now consumed -> next_inbound returns None and sent() shows the receipt.
        self.assertIsNone(bridgelink.next_inbound(self.adv))
        sent = bridgelink.sent_durable(self.ex)
        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(sent[0].consumed_at)

    def test_integrity_preserved_base64_sha256(self) -> None:
        link = bridgelink.eligible_pair(self.ex, self.adv)
        bridgelink.write_durable_turn(link, self.ex, "integrity check body")
        # parse_turns verifies body_sha256 against the decoded base64 body.
        turns = list(parse_turns(link.file))
        self.assertEqual(turns[0].body, "integrity check body")

    def test_fifo_oldest_first(self) -> None:
        link = bridgelink.eligible_pair(self.ex, self.adv)
        bridgelink.write_durable_turn(link, self.ex, "first")
        bridgelink.write_durable_turn(link, self.ex, "second")
        _, t = bridgelink.next_inbound(self.adv)
        self.assertEqual(t.body, "first")  # oldest-first

    def test_count_inbound_counts_distinct_unconsumed_turns(self) -> None:
        link = bridgelink.eligible_pair(self.ex, self.adv)
        self.assertEqual(bridgelink.count_inbound(self.adv), 0)
        bridgelink.write_durable_turn(link, self.ex, "first")
        bridgelink.write_durable_turn(link, self.ex, "second")
        self.assertEqual(bridgelink.count_inbound(self.adv), 2)
        self.assertEqual(bridgelink.count_inbound(self.ex), 0)  # own outbound
        bfile, bturn = bridgelink.next_inbound(self.adv)
        bridgelink.consume_inbound(bfile, bturn)
        self.assertEqual(bridgelink.count_inbound(self.adv), 1)

    def test_durable_turn_stamps_live_model_over_pinned(self) -> None:
        # CR3: a bridge turn records what ACTUALLY ran, not the pinned intent.
        # A fallback session (pinned fable, live opus) must stamp opus.
        proj_root = Path(self.tmp.name) / "projects"
        proj = livemodel.project_dir(str(self.repo), proj_root)
        proj.mkdir(parents=True)
        (proj / "s.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}}) + "\n"
        )
        self.ex.model = "claude-fable-5"  # pinned intent (fallback fired)
        with mock.patch.object(livemodel, "PROJECTS_ROOT", proj_root):
            link = bridgelink.eligible_pair(self.ex, self.adv)
            t = bridgelink.write_durable_turn(link, self.ex, "review verdict")
        self.assertEqual(t.model, "claude-opus-4-8")  # LIVE wins over pinned

    def test_durable_turn_falls_back_to_pinned_when_no_transcript(self) -> None:
        proj_root = Path(self.tmp.name) / "empty-projects"
        self.ex.model = "claude-opus-4-8"
        with mock.patch.object(livemodel, "PROJECTS_ROOT", proj_root):
            link = bridgelink.eligible_pair(self.ex, self.adv)
            t = bridgelink.write_durable_turn(link, self.ex, "no transcript here")
        self.assertEqual(t.model, "claude-opus-4-8")  # pinned fallback


if __name__ == "__main__":
    unittest.main()
