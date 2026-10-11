"""Tests for guards.session_auth — the session-scoped merge authorization.

Hermetic: repo_root is a tmpdir, time is injected via now_s, the Touch ID gate
is a fake. Every test asserts a DECISION, never live I/O.

The gate is FAIL-CLOSED, so the load-bearing tests are the REFUSALS. A test that
only proves the happy path passes would let every bound rot into dead code while
the suite stayed green.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import session_auth  # noqa: E402
from net import registry  # noqa: E402


def _yes(_prompt: str) -> bool:
    return True


def _no(_prompt: str) -> bool:
    return False


SHA_A = "aaaaaaa1111111111111111111111111111111111"
SHA_B = "bbbbbbb2222222222222222222222222222222222"


ORIGIN = "git@github.com:example-org/maw.git"
REPO_ID = "example-org/maw"


def _git_ok(gh_runner):
    """Answer `git remote get-url origin` normally and hand every gh call to
    `gh_runner`, so a test aimed at gh's failure branch reaches it."""
    git = _gh()

    def run(cmd, *a, **k):
        if cmd[:1] == ["git"]:
            return git(cmd, *a, **k)
        return gh_runner(cmd, *a, **k)
    return run


def _gh(author: str = "executor", sha: str = SHA_A, fail: bool = False,
        base: str = "main", default_branch: str = "main",
        head_ref: str = "fix/x", title: str = "a PR", origin: "str | None" = ORIGIN,
        calls: "list | None" = None):
    """A fake `gh`. The REAL gh is the ground truth the acting agent can't forge;
    here it is injected so tests assert decisions, never live I/O. Dispatches on
    the subcommand: `gh repo view` -> default branch; `gh pr view` -> PR facts
    (author/head/base/title). `base` defaults to the default branch so the merge
    base guard (#185) passes unless a test deliberately mismatches them."""

    def run(cmd, *a, **k):
        if calls is not None:
            calls.append(cmd)
        if cmd[:1] == ["git"] and "get-url" in cmd:
            if origin is None:
                return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="no origin")
            return subprocess.CompletedProcess(cmd, 0, stdout=origin + "\n", stderr="")
        if fail:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: no auth")
        if "repo" in cmd and "view" in cmd:
            payload = json.dumps({"defaultBranchRef": {"name": default_branch}})
        else:
            payload = json.dumps({
                "author": {"login": author}, "headRefOid": sha,
                "baseRefName": base, "headRefName": head_ref, "title": title,
            })
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    return run


class SessionAuthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.now = time.time()
        self.sid = "sess-abc"
        self.pid = os.getpid()

    def arm_ok(self, scope: str = "merge the session-auth arc PRs", **kw) -> None:
        kw.setdefault("seat", "advisor")
        # A consistent launch env by default; seatcheck must see no contradiction.
        kw.setdefault("env", {"MAW_BRIDGE_ROLE": kw["seat"]})
        kw.setdefault("runner", _gh())  # answers `git remote get-url origin`
        kw.setdefault("gate_fn", _yes)
        ok, msg = session_auth.arm(
            scope, self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, **kw
        )
        self.assertTrue(ok, msg)

    def sign(self, pr: int = 1, sha: str = SHA_A, verdict: str = "approved",
             seat: str = "advisor") -> None:
        """Record a verdict the way the advisor would at review time."""
        ok, msg = session_auth.record_verdict(
            self.root, pr, verdict, seat=seat,
            runner=_gh(sha=sha), now_s=self.now,
        )
        self.assertTrue(ok, msg)

    def check(self, **kw):
        runner = kw.pop("runner", None) or _gh()
        base = dict(
            pr=1, repo="maw", repo_root=self.root,
            session_id=self.sid, pid=self.pid, now_s=self.now, runner=runner,
        )
        base.update(kw)
        return session_auth.check(**base)

    def token(self) -> dict:
        return json.loads((self.root / session_auth.TOKEN_REL).read_text())

    def write_token(self, **over) -> None:
        t = self.token()
        t.update(over)
        (self.root / session_auth.TOKEN_REL).write_text(json.dumps(t))


class TestArm(SessionAuthTestCase):
    def test_arm_writes_bound_token(self) -> None:
        self.arm_ok()
        t = self.token()
        self.assertEqual(t["session_id"], self.sid)
        self.assertEqual(t["pid"], self.pid)
        self.assertTrue(t["nonce"])

    def test_token_is_0600(self) -> None:
        self.arm_ok()
        mode = (self.root / session_auth.TOKEN_REL).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_scope_reaches_the_touchid_prompt(self) -> None:
        # TARGET 10 (mine): the operator's fingerprint authorizes THE SCOPE, and the
        # prompt is the ONLY place he sees it. A scope that never renders is a
        # blind grant — and nothing else in the system would notice.
        seen: list[str] = []

        def capture(prompt: str) -> bool:
            seen.append(prompt)
            return True

        session_auth.arm(
            "merge PRs 81 and 82 only", self.root, session_id=self.sid,
            pid=self.pid, now_s=self.now, gate_fn=capture, seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"},
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("merge PRs 81 and 82 only", seen[0])

    def test_scope_survives_the_real_gate_prompt_cap(self) -> None:
        # The capture above stops at gate_fn. The real gate caps its prompt, and
        # it used to cap it at 120 chars, below the arm line's own boilerplate:
        # The operator's 2026-09-25 scope rendered as "scope: maw". Go through gate() itself.
        from guards import human_gate
        prompts: list[str] = []

        def sudo(cmd: list[str], _timeout: int) -> "subprocess.CompletedProcess[str]":
            if cmd[:2] == ["sudo", "-p"]:
                prompts.append(cmd[2])
            rc = 1 if cmd[:2] == ["sudo", "-n"] else 0
            return subprocess.CompletedProcess(cmd, rc, "", "")

        scope = ("example readiness run: L0 guard-defect fixes from the outside review "
                 "(one PR each) + L1 cuts and upgrades per the dated review "
                 "notes in this repo (daily-use surface cuts still asked first) + "
                 "export to a release directory + a new private repository "
                 "for it + release notes; no visibility changes, no renames; "
                 "plus advisor run-record/docs PRs")
        ok, _ = session_auth.arm(
            scope, self.root, session_id=self.sid, pid=self.pid, now_s=self.now,
            gate_fn=lambda a: human_gate.gate(a, runner=sudo), seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"},
        )
        self.assertTrue(ok)
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        # The scope's leading words render, not just its first word...
        self.assertIn("scope: example readiness run: L0 guard-defect fixes from the "
                      "outside review (one PR each) + L1 cuts and upgrades", prompt)
        # ...the cut is visible, never silent...
        self.assertIn("…", prompt)
        # ...and the enforced bound and the 6h cap still show.
        self.assertIn("PRs=any", prompt)
        self.assertIn("6h", prompt)

    def test_short_scope_renders_whole_without_ellipsis(self) -> None:
        from guards import human_gate
        prompts: list[str] = []

        def sudo(cmd: list[str], _timeout: int) -> "subprocess.CompletedProcess[str]":
            if cmd[:2] == ["sudo", "-p"]:
                prompts.append(cmd[2])
            rc = 1 if cmd[:2] == ["sudo", "-n"] else 0
            return subprocess.CompletedProcess(cmd, rc, "", "")

        session_auth.arm(
            "merge PRs 81 and 82 only", self.root, session_id=self.sid,
            pid=self.pid, now_s=self.now,
            gate_fn=lambda a: human_gate.gate(a, runner=sudo), seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"},
        )
        self.assertEqual(len(prompts), 1)
        self.assertIn("scope: merge PRs 81 and 82 only", prompts[0])
        self.assertNotIn("…", prompts[0])

    def test_refused_fingerprint_writes_no_token(self) -> None:
        ok, msg = session_auth.arm(
            "merge the arc", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=_no,
        )
        self.assertFalse(ok)
        self.assertFalse((self.root / session_auth.TOKEN_REL).exists())

    def test_refuses_unbounded_scope(self) -> None:
        # Scope is a security boundary now; "improve maw" is a blank check.
        for blank in ("", "   ", "all", "maw"):
            ok, msg = session_auth.arm(
                blank, self.root, session_id=self.sid, pid=self.pid,
                now_s=self.now, gate_fn=_yes,
            )
            self.assertFalse(ok, f"armed on unbounded scope {blank!r}")

    def test_refuses_without_session_id(self) -> None:
        ok, _ = session_auth.arm(
            "merge the arc PRs", self.root, session_id="", pid=self.pid,
            now_s=self.now, gate_fn=_yes,
        )
        self.assertFalse(ok)


class TestArmAttestsTheSeat(SessionAuthTestCase):
    """MAJOR 1: the operator's fingerprint must attest to WHO merges, not only to WHAT.

    The seat claim starts in the launch env, which is agent-reachable — so
    deriving it and calling it "derived" would just move the caller-supplied hole
    one layer down. Two things make it trustworthy, and both are tested here.
    """

    def test_seat_is_in_the_touchid_prompt(self) -> None:
        # THE fix: the one human act in the design must cover the field that
        # no-self-merge rests on. Scope alone authorizes WHAT, never WHO.
        seen: list[str] = []

        def capture(prompt: str) -> bool:
            seen.append(prompt)
            return True

        session_auth.arm(
            "merge PRs 81 and 82", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=capture, seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"},
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("seat=advisor", seen[0])

    def test_seat_env_contradiction_refuses_to_arm(self) -> None:
        # The %5 raw-pane-paste class, and the exact input that defeats
        # no-self-merge: a session mis-seated as 'executor' bakes that into the
        # token under the operator's finger, and then the advisor may merge its own PRs.
        ok, msg = session_auth.arm(
            "merge the arc PRs", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=_yes, seat="advisor",
            env={"MAW_BRIDGE_ROLE": "executor"},
        )
        self.assertFalse(ok)
        self.assertIn("CONTRADICTION", msg)
        self.assertFalse((self.root / session_auth.TOKEN_REL).exists())

    def test_contradiction_refuses_before_the_fingerprint(self) -> None:
        # The operator must never be asked to fingerprint a seat we already know is
        # disputed — a refusal after the prompt trains him to authorize noise.
        prompted: list[str] = []

        def capture(prompt: str) -> bool:
            prompted.append(prompt)
            return True

        session_auth.arm(
            "merge the arc PRs", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=capture, seat="advisor",
            env={"MAW_BRIDGE_ROLE": "executor"},
        )
        self.assertEqual(prompted, [], "the operator was prompted on a disputed seat")

    def test_refuses_to_arm_without_a_seat(self) -> None:
        ok, msg = session_auth.arm(
            "merge the arc PRs", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=_yes, seat="", env={},
        )
        self.assertFalse(ok)
        self.assertIn("seat", msg)


class TestGrantBoundary(SessionAuthTestCase):
    """MAJOR 3: only seats the operator granted merge authority to may arm.

    The operator 2026-07-16: "the advisor merges in the reccomended order"; "the executor
    no longer merges at all". Bounding merging only by author != seat made that
    ruling a coincidence rather than a rule.
    """

    def test_advisor_may_arm(self) -> None:
        self.arm_ok(seat="advisor")
        self.assertEqual(self.token()["seat"], "advisor")

    def test_executor_may_not_arm(self) -> None:
        # (a) The executor arming and merging ADVISOR-authored PRs passes
        # author != seat — it is the arrangement the operator RETIRED, resurrected through
        # the new mechanism.
        ok, msg = session_auth.arm(
            "merge the arc PRs", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=_yes, seat="executor",
            env={"MAW_BRIDGE_ROLE": "executor"},
        )
        self.assertFalse(ok)
        self.assertIn("not authorized to merge", msg)
        self.assertFalse((self.root / session_auth.TOKEN_REL).exists())

    def test_watcher_seat_may_not_arm(self) -> None:
        # (b) The cos hole: 'cos' is NOT in seatcheck.ROLES, so it skipped the
        # seat-env check entirely; then author != 'cos' passed for EVERY PR in
        # the repo. The seat whose defining property is that it does not build
        # could have merged everything. role='cos' is real — WATCHER_ROLES
        # shipped in #80.
        ok, msg = session_auth.arm(
            "merge the arc PRs", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=_yes, seat="cos",
            env={"MAW_NET_NAME": "cos"},
        )
        self.assertFalse(ok)
        self.assertIn("not authorized to merge", msg)

    def test_unauthorized_seat_refuses_before_the_fingerprint(self) -> None:
        # The operator must never be asked to fingerprint a grant he did not make. The
        # prompt renders seat=, so he COULD catch it — but a control that relies
        # on a human spotting the wrong word in a familiar prompt is the
        # click-through failure this module refuses elsewhere. Attestation is the
        # backstop; the refusal is the gate.
        prompted: list[str] = []

        def capture(prompt: str) -> bool:
            prompted.append(prompt)
            return True

        session_auth.arm(
            "merge the arc PRs", self.root, session_id=self.sid, pid=self.pid,
            now_s=self.now, gate_fn=capture, seat="cos",
            env={"MAW_NET_NAME": "cos"},
        )
        self.assertEqual(prompted, [], "the operator was prompted for an ungranted seat")

    def test_the_grant_matches_dps_ruling(self) -> None:
        # Widening this is a Layer-1 scope-of-authority change and needs the operator on
        # the trusted path. If this test fails, someone widened the grant — that
        # is the point of the test, not an inconvenience to update.
        self.assertEqual(session_auth.MERGE_AUTHORIZED_SEATS, ("advisor",))


class TestSessionBind(SessionAuthTestCase):
    def test_happy_path_passes(self) -> None:
        self.arm_ok()
        self.sign()
        ok, refusal = self.check()
        self.assertTrue(ok, refusal.line() if refusal else "")

    def test_target1_token_does_not_survive_its_session(self) -> None:
        # A stale token that still authorizes is the whole ballgame.
        self.arm_ok()
        dead = 999999  # not alive
        self.write_token(pid=dead)
        ok, refusal = self.check(pid=dead)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "session-gone")

    def test_target2_another_session_cannot_ride_token(self) -> None:
        self.arm_ok()
        ok, refusal = self.check(session_id="sess-other")
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "wrong-session")

    def test_target5_pid_reuse_does_not_resurrect(self) -> None:
        # A recycled pid must not inherit a dead session's token: the pid in the
        # token must match the ACTING pid, so a different live process refuses
        # even though its own pid is alive.
        self.arm_ok()
        ok, refusal = self.check(pid=self.pid + 1)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "wrong-pid")


class TestExpiryCap(SessionAuthTestCase):
    """TARGET 7: the cap must be enforced ON CHECK, not merely computed at ARM."""

    def test_boundary_just_inside_passes(self) -> None:
        self.arm_ok()
        self.sign()
        ok, refusal = self.check(now_s=self.now + 6 * 3600 - 1)
        self.assertTrue(ok, refusal.line() if refusal else "")

    def test_boundary_just_outside_refuses(self) -> None:
        self.arm_ok()
        ok, refusal = self.check(now_s=self.now + 6 * 3600 + 1)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "cap-reached")

    def test_expired_refuses_even_with_pristine_token_and_live_session(self) -> None:
        # The exact no-op this target exists to catch: everything else perfect,
        # only the clock past the cap. If expires_at were computed-but-never-read
        # this passes and the suite still goes green.
        self.arm_ok()
        ok, refusal = self.check(now_s=self.now + 7 * 3600)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "cap-reached")

    def test_refusal_names_which_bound_tripped(self) -> None:
        self.arm_ok()
        _, refusal = self.check(now_s=self.now + 7 * 3600)
        self.assertIn("cap", refusal.line().lower())
        self.assertIn("re-arm", refusal.line())

    def test_clock_backward_refuses_not_extends(self) -> None:
        self.arm_ok()
        ok, refusal = self.check(now_s=self.now - 3600)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "clock-backward")


class TestFailClosedShapes(SessionAuthTestCase):
    """TARGET 4: missing / corrupt / unreadable / absent-fields -> REFUSE."""

    def test_no_token_refuses(self) -> None:
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "no-token")

    def test_corrupt_json_refuses(self) -> None:
        self.arm_ok()
        (self.root / session_auth.TOKEN_REL).write_text("{not json")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "corrupt-token")

    def test_non_object_refuses(self) -> None:
        self.arm_ok()
        (self.root / session_auth.TOKEN_REL).write_text("[1,2,3]")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "corrupt-token")

    def test_missing_field_refuses(self) -> None:
        self.arm_ok()
        t = self.token()
        del t["expires_at"]
        (self.root / session_auth.TOKEN_REL).write_text(json.dumps(t))
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "malformed-token")

    def test_unparseable_timestamps_refuse(self) -> None:
        self.arm_ok()
        self.write_token(expires_at="not-a-time")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "corrupt-token")


class TestGhFactsRequired(SessionAuthTestCase):
    """gh is the ground truth for the reviewed SHA — an unverifiable PR refuses.

    No-self-merge was RETIRED 2026-07-17 (the arm is the authorization; PR
    seat-authorship is not a boundary the operator wants enforced), so the tests that
    asserted a self-merge refusal are gone. What survives: gh must be reachable
    and well-formed to prove the reviewed SHA, or check() fail-closes.
    """

    def test_gh_nonzero_rc_refuses(self) -> None:
        # gh cannot confirm the PR (and thus its reviewed SHA) -> refuse. An
        # unverifiable PR is not a mergeable PR.
        self.arm_ok()
        self.sign()
        ok, refusal = self.check(runner=_gh(fail=True))
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "unknown-author")

    def test_gh_raising_refuses(self) -> None:
        # MINOR 2: the nonzero-rc case does NOT exercise the except branch. The
        # advisor proved the gap by replacing that branch with forged fail-open
        # facts — and the suite stayed green. Correct code with nothing pinning
        # it is one refactor from fail-open, which is this module's whole thesis.
        def raiser(cmd, *a, **k):
            raise OSError("gh: command not found")

        self.arm_ok()
        self.sign()
        ok, refusal = self.check(runner=_git_ok(raiser))
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "unknown-author")

    def test_gh_timeout_refuses(self) -> None:
        def timeout(cmd, *a, **k):
            raise subprocess.TimeoutExpired(cmd, 30)

        self.arm_ok()
        self.sign()
        ok, refusal = self.check(runner=_git_ok(timeout))
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "unknown-author")

    def test_gh_garbage_output_refuses(self) -> None:
        def garbage(cmd, *a, **k):
            return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

        self.arm_ok()
        self.sign()
        ok, refusal = self.check(runner=_git_ok(garbage))
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "unknown-author")

    def test_gh_missing_author_refuses(self) -> None:
        def no_author(cmd, *a, **k):
            payload = json.dumps({"author": {}, "headRefOid": SHA_A})
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

        self.arm_ok()
        self.sign()
        ok, refusal = self.check(runner=_git_ok(no_author))
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "unknown-author")


class TestSignedVerdict(SessionAuthTestCase):
    """BLOCKER 1: the verdict must be an ARTIFACT, not a self-asserted flag.

    On a no-echo repo this is THE ENTIRE GATE. A gate whose subject reports its
    own compliance is telemetry, not enforcement.
    """

    def test_target3_unrecorded_pr_refuses(self) -> None:
        self.arm_ok()
        ok, refusal = self.check()  # no verdict recorded
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "no-verdict")

    def test_changes_requested_is_not_approval(self) -> None:
        self.arm_ok()
        self.sign(verdict="changes-requested")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "not-approved")

    def test_escalate_verdict_holds_the_merge(self) -> None:
        # Lane B (2026-09-22): /maw:escalate --pr records `escalate`; the merge
        # stays refused until the advisor signs a real verdict.
        self.arm_ok()
        self.sign(verdict="escalate")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "not-approved")
        self.sign(verdict="approved")
        self.assertTrue(self.check()[0])

    def test_verdict_records_the_reviewed_sha_from_gh(self) -> None:
        # The binding is only worth something if the SHA is DERIVED. A verdict
        # that took the SHA from its caller would let a seat sign SHA B while
        # reading SHA A.
        self.sign(sha=SHA_A)
        rec = session_auth.load_verdict(self.root, 1)
        self.assertEqual(rec["sha"], SHA_A)

    def test_major3_verdict_does_not_follow_the_code(self) -> None:
        # I review A; the executor pushes B; my verdict still "exists". It must
        # not authorize B.
        self.arm_ok()
        self.sign(sha=SHA_A)
        ok, refusal = self.check(runner=_gh(sha=SHA_B))
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "sha-drift")

    def test_verdict_covering_current_head_passes(self) -> None:
        self.arm_ok()
        self.sign(sha=SHA_A)
        ok, refusal = self.check(runner=_gh(sha=SHA_A))
        self.assertTrue(ok, refusal.line() if refusal else "")

    def test_unbound_verdict_refuses(self) -> None:
        self.arm_ok()
        self.sign()
        p = self.root / session_auth.VERDICT_REL
        p.write_text(json.dumps({"pr": 1, "verdict": "approved", "seat": "advisor"}) + "\n")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "verdict-unbound")

    def test_latest_verdict_wins(self) -> None:
        # A re-review after a push must be able to re-authorize.
        self.arm_ok()
        self.sign(sha=SHA_A)
        self.sign(sha=SHA_B)
        ok, refusal = self.check(runner=_gh(sha=SHA_B))
        self.assertTrue(ok, refusal.line() if refusal else "")

    def test_torn_verdict_line_is_not_an_authorization(self) -> None:
        self.arm_ok()
        p = self.root / session_auth.VERDICT_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json\n")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "no-verdict")

    def test_expect_sha_mismatch_refuses(self) -> None:
        # #185 defect 2: --expect-sha binds the advisor's INTENT. A verdict on a
        # PR whose live head is NOT the SHA the advisor reviewed (wrong PR number,
        # or the head moved) is REFUSED before it is recorded.
        ok, msg = session_auth.record_verdict(
            self.root, 1, "approved", seat="advisor",
            runner=_gh(sha=SHA_A), now_s=self.now, expect_sha=SHA_B)
        self.assertFalse(ok)
        self.assertIn("expect-sha", msg)
        self.assertIsNone(session_auth.load_verdict(self.root, 1))  # nothing recorded

    def test_expect_sha_match_records(self) -> None:
        # A matching --expect-sha (abbreviated is fine) records normally.
        ok, msg = session_auth.record_verdict(
            self.root, 1, "approved", seat="advisor",
            runner=_gh(sha=SHA_A), now_s=self.now, expect_sha=SHA_A[:10])
        self.assertTrue(ok, msg)
        self.assertEqual(session_auth.load_verdict(self.root, 1)["sha"], SHA_A)

    def test_expect_sha_too_short_refuses(self) -> None:
        # A fragment shorter than 7 chars is too weak to bind a verdict.
        ok, msg = session_auth.record_verdict(
            self.root, 1, "approved", seat="advisor",
            runner=_gh(sha=SHA_A), now_s=self.now, expect_sha=SHA_A[:4])
        self.assertFalse(ok)

    def test_verdict_confirmation_echoes_title(self) -> None:
        ok, msg = session_auth.record_verdict(
            self.root, 1, "approved", seat="advisor",
            runner=_gh(sha=SHA_A, title="lane 2d"), now_s=self.now)
        self.assertTrue(ok, msg)
        self.assertIn("lane 2d", msg)


class TestReviewerRole(SessionAuthTestCase):
    """L0 #3: only a reviewer seat's verdict authorizes a merge. The role is
    validated when the verdict is recorded AND when it is consumed, and on the
    CLI path it is DERIVED from the lineage-verified registry entry, not read
    from the env the caller controls."""

    def _write_raw_verdict(self, seat: str, pr: int = 1, sha: str = SHA_A) -> None:
        path = self.root / session_auth.VERDICT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-09-25T00:00:00Z", "pr": pr, "sha": sha,
                                "verdict": "approved", "seat": seat}) + "\n")

    def _entry(self, role: str):
        return registry.Entry(
            name=role, role=role, repo_root=str(self.root), cwd=str(self.root),
            pid=self.pid, tty=None, tmux_pane=None, session_id=self.sid,
            started_at="2026-09-25T00:00:00Z", last_seen="2026-09-25T00:00:00Z")

    def test_a_non_reviewer_seat_cannot_record_a_verdict(self) -> None:
        ok, msg = session_auth.record_verdict(
            self.root, 1, "approved", seat="executor",
            runner=_gh(sha=SHA_A), now_s=self.now)
        self.assertFalse(ok, msg)
        self.assertIsNone(session_auth.load_verdict(self.root, 1))

    def test_an_executor_verdict_does_not_authorize_a_merge(self) -> None:
        # Codex repro: an executor-authored approval passed authorization.
        self.arm_ok()
        self._write_raw_verdict("executor")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "verdict-not-reviewer")

    def test_a_verdict_with_no_seat_does_not_authorize_a_merge(self) -> None:
        self.arm_ok()
        self._write_raw_verdict("")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "verdict-not-reviewer")

    def test_cli_path_takes_the_role_from_the_registry_not_the_env(self) -> None:
        # The env CLAIMS advisor; the lineage-verified registry entry says
        # executor. The derived role wins, and it is not a reviewer.
        with mock.patch.dict(os.environ, {"MAW_BRIDGE_ROLE": "advisor",
                                          "MAW_NET_NAME": "advisor"}), \
             mock.patch.object(session_auth, "_registry_identity",
                               return_value=((self.sid, self.pid), "")), \
             mock.patch.object(session_auth.registry, "load_entry",
                               return_value=self._entry("executor")):
            ok, msg = session_auth.record_verdict(
                self.root, 1, "approved", runner=_gh(sha=SHA_A), now_s=self.now)
        self.assertFalse(ok, msg)
        self.assertIsNone(session_auth.load_verdict(self.root, 1))

    def test_cli_path_refuses_when_lineage_does_not_reach_the_seat(self) -> None:
        with mock.patch.dict(os.environ, {"MAW_BRIDGE_ROLE": "advisor",
                                          "MAW_NET_NAME": "advisor"}), \
             mock.patch.object(session_auth, "_registry_identity",
                               return_value=(None, "caller lineage does not reach")):
            ok, msg = session_auth.record_verdict(
                self.root, 1, "approved", runner=_gh(sha=SHA_A), now_s=self.now)
        self.assertFalse(ok, msg)
        self.assertIn("lineage", msg)

    def test_cli_path_records_the_derived_reviewer_and_its_session(self) -> None:
        with mock.patch.dict(os.environ, {"MAW_BRIDGE_ROLE": "advisor",
                                          "MAW_NET_NAME": "advisor"}), \
             mock.patch.object(session_auth, "_registry_identity",
                               return_value=((self.sid, self.pid), "")), \
             mock.patch.object(session_auth.registry, "load_entry",
                               return_value=self._entry("advisor")):
            ok, msg = session_auth.record_verdict(
                self.root, 1, "approved", runner=_gh(sha=SHA_A), now_s=self.now)
        self.assertTrue(ok, msg)
        rec = session_auth.load_verdict(self.root, 1)
        self.assertEqual(rec["seat"], "advisor")
        self.assertEqual(rec["session_id"], self.sid)


class TestListVerdicts(SessionAuthTestCase):
    """#146: the read counterpart to record_verdict — list recorded verdicts."""

    def _write(self, *recs) -> None:
        p = self.root / session_auth.VERDICT_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def test_empty_when_no_file(self) -> None:
        self.assertEqual(session_auth.list_verdicts(self.root), [])

    def test_lists_all_oldest_first(self) -> None:
        self._write(
            {"pr": 1, "sha": "a" * 40, "verdict": "approved", "seat": "advisor", "ts": "t1"},
            {"pr": 2, "sha": "b" * 40, "verdict": "changes-requested", "seat": "advisor", "ts": "t2"},
        )
        self.assertEqual([r["pr"] for r in session_auth.list_verdicts(self.root)], [1, 2])

    def test_filter_by_pr(self) -> None:
        self._write(
            {"pr": 1, "sha": "a" * 40, "verdict": "approved", "seat": "advisor", "ts": "t1"},
            {"pr": 2, "sha": "b" * 40, "verdict": "approved", "seat": "advisor", "ts": "t2"},
            {"pr": 1, "sha": "c" * 40, "verdict": "approved", "seat": "advisor", "ts": "t3"},
        )
        rows = session_auth.list_verdicts(self.root, pr=1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["pr"] == 1 for r in rows))

    def test_torn_line_skipped(self) -> None:
        p = self.root / session_auth.VERDICT_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"pr":1,"verdict":"approved"}\n{not json\n', encoding="utf-8")
        self.assertEqual(len(session_auth.list_verdicts(self.root)), 1)

    def test_cli_verdicts_verb(self) -> None:
        self._write({"pr": 5, "sha": "d" * 40, "verdict": "approved",
                     "seat": "advisor", "ts": "2026-07-21T07:00:00Z"})
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = session_auth.main(["--repo-root", str(self.root), "verdicts"])
        self.assertEqual(rc, session_auth.PASS)
        self.assertIn("PR #5", buf.getvalue())
        self.assertIn("ddddddd", buf.getvalue())  # sha[:7]

    def test_cli_verdicts_empty(self) -> None:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = session_auth.main(["--repo-root", str(self.root), "verdicts", "--pr", "9"])
        self.assertEqual(rc, session_auth.PASS)
        self.assertIn("no recorded verdicts for PR #9", buf.getvalue())


class TestCliSurface(SessionAuthTestCase):
    def test_cli_exposes_no_assertable_facts(self) -> None:
        """The BLOCKERs were CLI flags. This is the regression guard.

        `--pr-author`, `--acting-seat` and `--signed-verdict` each let the
        governed party assert the fact that governs it. If someone re-adds one
        "for testing", the gate silently becomes a convention again and every
        other test in this file still passes.
        """
        src = (Path(__file__).resolve().parents[2] / "maw" / "guards"
               / "session_auth.py").read_text()
        for flag in ("--pr-author", "--acting-seat", "--signed-verdict"):
            self.assertNotIn(
                f'"{flag}"', src,
                f"{flag} is back: the governed party can assert its own gate",
            )


class TestUnclassifiedEchoGuard(SessionAuthTestCase):
    """The echo path survives ONLY as the ECHO_REQUIRED_REPOS opt-in.

    The operator 2026-07-16 narrowed the echo to one production repo; the operator 2026-07-18
    removed even that (every repo behaves the same); the operator 2026-07-24 retired the
    unclassified-slug typo guard too (every pair launched works the same) — the
    default is now no-echo for EVERY repo. The echo machinery
    stays live for an explicit ECHO_REQUIRED_REPOS entry, so these exercise it
    by patching a fake slug into that tuple to keep the path covered.
    """

    UNCLASSIFIED = "an-unclassified-repo"

    def echo_required(self) -> "contextlib.AbstractContextManager[object]":
        return mock.patch.object(
            session_auth, "ECHO_REQUIRED_REPOS", (self.UNCLASSIFIED,)
        )

    def listed_branch(self, *extra: str) -> "contextlib.AbstractContextManager[object]":
        # With the default flipped to echo-required, only the NO_ECHO_REPOS
        # listing can make a slug no-echo, so these tests exercise the listed
        # branch rather than passing through the 2026-07-24 default.
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(session_auth, "DEFAULT_REQUIRES_ECHO", True))
        if extra:
            stack.enter_context(mock.patch.object(
                session_auth, "NO_ECHO_REPOS", session_auth.NO_ECHO_REPOS + extra))
        return stack

    def test_all_classified_repos_are_no_echo(self) -> None:
        # Reads the real tuple, so the test names no repo itself.
        with self.listed_branch("a-listed-repo"):
            self.assertIn("a-listed-repo", session_auth.NO_ECHO_REPOS)
            for repo in session_auth.NO_ECHO_REPOS:
                self.assertFalse(session_auth.requires_echo(repo), repo)
            self.assertTrue(session_auth.requires_echo("not-listed"))  # control

    def test_a_classified_production_repo_is_now_no_echo(self) -> None:
        # The 2026-07-18 ruling, as CODE: a classified repo whose merges deploy
        # to real users no longer requires a Y.
        repo = "a-production-repo"
        with self.listed_branch(repo):
            self.assertFalse(session_auth.requires_echo(repo))
            self.arm_ok()
            self.sign()
            ok, _ = self.check(repo=repo, echo_id=None)
        self.assertTrue(ok)

    def test_unclassified_slug_is_no_echo(self) -> None:
        # The 2026-07-24 ruling, as CODE: no typo guard — default is no-echo.
        self.assertFalse(session_auth.requires_echo(self.UNCLASSIFIED))

    def test_unclassified_passes_without_echo(self) -> None:
        # Valid token + signed verdict now suffice on ANY slug (2026-07-24).
        self.arm_ok()
        self.sign()
        ok, refusal = self.check(repo=self.UNCLASSIFIED, echo_id=None)
        self.assertTrue(ok, refusal.line() if refusal else "")

    def test_echo_required_opt_in_still_refuses_without_echo(self) -> None:
        # The opt-back-in: an ECHO_REQUIRED_REPOS entry still fails closed.
        self.arm_ok()
        self.sign()
        with self.echo_required():
            self.assertTrue(session_auth.requires_echo(self.UNCLASSIFIED))
            ok, refusal = self.check(repo=self.UNCLASSIFIED, echo_id=None)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "echo-required")

    def test_maw_passes_without_echo(self) -> None:
        self.arm_ok()
        self.sign()
        ok, _ = self.check(repo="maw", echo_id=None)
        self.assertTrue(ok)

    def test_opt_in_passes_with_covering_echo(self) -> None:
        self.arm_ok()
        self.sign()
        echo_id, _ = session_auth.record_echo(
            self.root, [7, 8], "Y — merge 7 then 8", now_s=self.now
        )
        self.sign(pr=7)
        with self.echo_required():
            ok, refusal = self.check(
                repo=self.UNCLASSIFIED, pr=7, echo_id=echo_id
            )
        self.assertTrue(ok, refusal.line() if refusal else "")

    def test_echo_does_not_cover_a_different_pr(self) -> None:
        self.arm_ok()
        self.sign()
        echo_id, _ = session_auth.record_echo(
            self.root, [7, 8], "Y", now_s=self.now
        )
        self.sign(pr=9)
        with self.echo_required():
            ok, refusal = self.check(
                repo=self.UNCLASSIFIED, pr=9, echo_id=echo_id
            )
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "echo-mismatch")

    def test_unknown_echo_id_refuses(self) -> None:
        self.arm_ok()
        self.sign()
        with self.echo_required():
            ok, refusal = self.check(repo=self.UNCLASSIFIED, echo_id="deadbeef")
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "echo-mismatch")

    def test_torn_echo_line_is_not_an_authorization(self) -> None:
        self.arm_ok()
        self.sign()
        p = self.root / session_auth.ECHO_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json\n")
        with self.echo_required():
            ok, refusal = self.check(repo=self.UNCLASSIFIED, echo_id="whatever")
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "echo-mismatch")


class TestRecordEcho(SessionAuthTestCase):
    def test_records_dp_verbatim(self) -> None:
        echo_id, _ = session_auth.record_echo(
            self.root, [1, 2], "Y — merge #1 then #2", now_s=self.now
        )
        rec = json.loads((self.root / session_auth.ECHO_REL).read_text().strip())
        self.assertEqual(rec["echo_id"], echo_id)
        self.assertEqual(rec["prs"], [1, 2])
        self.assertEqual(rec["response"], "Y — merge #1 then #2")


class TestAudit(SessionAuthTestCase):
    """GATE #8: a gate that refuses silently cannot be proven to have worked."""

    def test_arm_is_audited(self) -> None:
        self.arm_ok()
        log = (self.root / session_auth.AUDIT_REL).read_text()
        self.assertIn('"event": "arm"', log)

    def test_audit_survives_unwritable_dir(self) -> None:
        # Best-effort by design: the verdict is the security property, not the
        # log. It must not raise.
        session_auth.audit(Path("/nonexistent-xyz"), {"a": 1})


class TestMergeBindsTheAuthorizedSha(SessionAuthTestCase):
    """L0 #1: the SHA check() authorized is the SHA that merges. check() reads
    the head once; the wrapper must not merge whatever the head became after."""

    def _runner(self, heads: "list[str]"):
        """gh fake whose `pr view` head advances through `heads` (last one sticks)."""
        self.calls: list[list[str]] = []
        seq = list(heads)

        def run(cmd, *a, **k):
            self.calls.append(cmd)
            if cmd[:1] == ["git"] and "get-url" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=ORIGIN + "\n", stderr="")
            if cmd[:3] == ["gh", "pr", "view"]:
                head = seq.pop(0) if len(seq) > 1 else seq[0]
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "author": {"login": "executor"}, "headRefOid": head,
                    "baseRefName": "main", "headRefName": "fix/x", "title": "a PR"}),
                    stderr="")
            if cmd[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
                    {"defaultBranchRef": {"name": "main"}}), stderr="")
            if cmd[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return run

    def _merge(self, runner):
        with mock.patch.object(session_auth, "_session_bounds",
                               return_value=({"repo": REPO_ID}, None)):
            return session_auth.merge_pr(1, self.root, runner=runner, repo="maw")

    def _merge_calls(self) -> "list[list[str]]":
        return [c for c in self.calls if c[:3] == ["gh", "pr", "merge"]]

    def test_the_merge_is_conditioned_on_the_authorized_head(self) -> None:
        self.sign(sha=SHA_A)
        ok, msg = self._merge(self._runner([SHA_A]))
        self.assertTrue(ok, msg)
        merges = self._merge_calls()
        self.assertEqual(len(merges), 1, merges)
        cmd = merges[0]
        self.assertIn("--match-head-commit", cmd)
        self.assertEqual(cmd[cmd.index("--match-head-commit") + 1], SHA_A)

    def test_a_head_that_moves_after_check_is_refused_before_merge(self) -> None:
        # Codex repro: head A passes check(), the next lookup shows head B, and
        # the wrapper still merged.
        self.sign(sha=SHA_A)
        ok, msg = self._merge(self._runner([SHA_A, SHA_B]))
        self.assertFalse(ok, msg)
        self.assertIn("sha-drift", msg)
        self.assertEqual(self._merge_calls(), [], "must not merge a head nobody reviewed")


class TestCanonicalRepoIdentity(SessionAuthTestCase):
    """L0 #6: the repo --repo-root selects (token, verdicts) is the repo every gh
    call queries and merges. One identity, derived from the origin remote."""

    def test_gh_queries_are_bound_to_the_repo_roots_origin(self) -> None:
        self.arm_ok()
        self.sign()
        calls: list = []
        ok, refusal = self.check(runner=_gh(calls=calls))
        self.assertTrue(ok, refusal)
        gh = [c for c in calls if c[:1] == ["gh"]]
        self.assertTrue(gh)
        for c in gh:
            self.assertIn("--repo", c, c)
            self.assertEqual(c[c.index("--repo") + 1], REPO_ID, c)

    def test_a_verdict_recorded_for_another_repo_does_not_authorize(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.record_verdict(
            self.root, 1, "approved", seat="advisor",
            runner=_gh(origin="git@github.com:someone/fork.git"), now_s=self.now)
        self.assertTrue(ok, msg)
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "verdict-repo-mismatch")

    def test_the_verdict_records_the_repo_identity(self) -> None:
        self.sign()
        self.assertEqual(session_auth.load_verdict(self.root, 1)["repo"], REPO_ID)

    def test_no_readable_origin_refuses(self) -> None:
        self.arm_ok()
        self.sign()
        ok, refusal = self.check(runner=_gh(origin=None))
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "repo-unverifiable")

    def test_a_token_armed_for_another_repo_does_not_authorize(self) -> None:
        self.arm_ok()
        self.sign()
        self.write_token(repo="someone/fork")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "token-repo-mismatch")

    def test_a_token_with_no_repo_does_not_authorize(self) -> None:
        # Tokens armed before the repo binding existed fail closed.
        self.arm_ok()
        self.sign()
        t = self.token()
        del t["repo"]
        (self.root / session_auth.TOKEN_REL).write_text(json.dumps(t))
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "token-repo-mismatch")

    def test_a_verdict_with_no_repo_does_not_authorize(self) -> None:
        self.arm_ok()
        path = self.root / session_auth.VERDICT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pr": 1, "sha": SHA_A, "verdict": "approved",
                                    "seat": "advisor"}) + "\n")
        ok, refusal = self.check()
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "verdict-repo-mismatch")

    def test_the_arm_prompt_shows_the_repo(self) -> None:
        seen: list = []
        session_auth.arm("merge PRs 1 and 2 only", self.root, session_id=self.sid,
                         pid=self.pid, now_s=self.now, seat="advisor",
                         env={"MAW_BRIDGE_ROLE": "advisor"}, runner=_gh(),
                         gate_fn=lambda p: seen.append(p) or True)
        self.assertIn(f"repo={REPO_ID}", seen[0])

    def test_identity_needs_a_hosted_remote_and_ignores_case(self) -> None:
        for url, want in (
                ("git@github.com:Example-Org/MAW.git", "example-org/maw"),
                ("https://github.com/example-org/maw", "example-org/maw"),
                ("ssh://git@github.com/example-org/maw.git", "example-org/maw"),
                ("https://github.com/example-org/maw.git/", "example-org/maw"),
                ("https://github.com/example-org/maw/", "example-org/maw"),
                ("git@github.com:example-org/maw.git/", "example-org/maw"),
                ("https://github.com/example-org/.git", ""),
                ("/tmp/somewhere/owner/maw.git", ""),
                ("../owner/maw", ""),
                ("file:///srv/owner/maw.git", "")):
            with self.subTest(url):
                self.assertEqual(
                    session_auth._repo_identity(self.root, _gh(origin=url)), want)

    def test_the_slug_and_the_identity_name_the_same_repo_for_one_remote(self) -> None:
        """Two parsers read one remote. They may differ in shape (bare name
        against owner/name) but never in which repo they mean."""
        for url in ("git@github.com:example-org/maw.git", "https://github.com/example-org/maw",
                    "https://github.com/example-org/maw.git/", "https://github.com/example-org/maw/",
                    "ssh://git@github.com/example-org/maw.git"):
            with self.subTest(url):
                self.assertEqual(session_auth._slug_from_remote_url(url), "maw")
                self.assertEqual(session_auth._repo_identity(self.root, _gh(origin=url)),
                                 "example-org/maw")

    def test_every_merge_step_gh_call_names_the_repo(self) -> None:
        self.sign()
        calls: list = []
        with mock.patch.object(session_auth, "_session_bounds",
                               return_value=({"repo": REPO_ID}, None)):
            ok, msg = session_auth.merge_pr(1, self.root, runner=_gh(calls=calls),
                                            repo="maw")
        self.assertTrue(ok, msg)
        gh = [c for c in calls if c[:1] == ["gh"]]
        self.assertTrue(any(c[:3] == ["gh", "pr", "merge"] for c in gh), gh)
        for c in gh:
            if c[:3] == ["gh", "repo", "view"]:
                self.assertEqual(c[3], REPO_ID, c)
                continue
            self.assertIn("--repo", c, c)
            self.assertEqual(c[c.index("--repo") + 1], REPO_ID, c)


class TestScopeIsAdvisoryUnlessAPrSetIsArmed(SessionAuthTestCase):
    """L0 #5: free-text scope is ADVISORY (the operator reads it at arm time; no
    code parses it). An optional --prs set is the enforced bound."""

    def test_an_armed_pr_set_refuses_a_pr_outside_it(self) -> None:
        # Codex repro: a session scoped to PR 1 authorized PR 999.
        self.arm_ok(scope="merge PR 1 only", prs=[1])
        self.sign(pr=999)
        ok, refusal = self.check(pr=999)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "out-of-scope")

    def test_a_pr_inside_the_armed_set_passes(self) -> None:
        self.arm_ok(scope="merge PRs 1 and 2", prs=[1, 2])
        self.sign(pr=2)
        ok, refusal = self.check(pr=2)
        self.assertTrue(ok, refusal)

    def test_the_pr_set_reaches_the_arm_prompt(self) -> None:
        seen: list = []
        self.arm_ok(scope="merge PRs 7 and 9", prs=[9, 7],
                    gate_fn=lambda p: seen.append(p) or True)
        self.assertIn("PRs=#7,#9", seen[0])

    def test_without_a_pr_set_the_arm_says_the_scope_is_advisory(self) -> None:
        seen: list = []
        ok, msg = session_auth.arm(
            "merge the session-auth arc PRs", self.root, session_id=self.sid,
            pid=self.pid, now_s=self.now, seat="advisor",
            env={"MAW_BRIDGE_ROLE": "advisor"}, runner=_gh(),
            gate_fn=lambda p: seen.append(p) or True)
        self.assertTrue(ok, msg)
        self.assertIn("advisory", msg)
        self.assertIn("advisory", seen[0])

    def test_free_text_scope_alone_bounds_nothing(self) -> None:
        # Pins the documented behaviour: with no --prs, the scope text is what
        # the operator read at arm time, and it does not stop PR 999.
        self.arm_ok(scope="merge PR 1 only")
        self.sign(pr=999)
        ok, refusal = self.check(pr=999)
        self.assertTrue(ok, refusal)

    def test_narrow_shrinks_the_set_and_refuses_to_widen(self) -> None:
        self.arm_ok(scope="merge PRs 1 2 3", prs=[1, 2, 3])
        ok, msg = session_auth.narrow(self.root, [2], session_id=self.sid,
                                      pid=self.pid, now_s=self.now, runner=_gh())
        self.assertTrue(ok, msg)
        self.assertEqual(self.token()["prs"], [2])
        ok, msg = session_auth.narrow(self.root, [2, 4], session_id=self.sid,
                                      pid=self.pid, now_s=self.now, runner=_gh())
        self.assertFalse(ok, msg)
        self.assertIn("widening", msg)
        self.sign(pr=1)
        ok, refusal = self.check(pr=1)
        self.assertFalse(ok)
        self.assertEqual(refusal.bound, "out-of-scope")
        self.assertIn("#1", refusal.detail)

    def test_narrow_can_bound_a_session_armed_without_a_set(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.narrow(self.root, [5], session_id=self.sid,
                                      pid=self.pid, now_s=self.now, runner=_gh())
        self.assertTrue(ok, msg)
        self.sign(pr=6)
        self.assertEqual(self.check(pr=6)[1].bound, "out-of-scope")

    def test_the_session_object_carries_the_pr_set(self) -> None:
        from guards import human_grant as hg
        s = {"repo": REPO_ID, "seat": "advisor", "session_id": "s",
             "scope": "merge two PRs", "prs": "#7,#9"}
        self.assertIsNone(hg.session_shape_error("session.arm", s))
        self.assertIn('"prs":"#7,#9"', hg.session_instruction("session.arm", s))
        for bad in ("7,9", "#7, #9", "#0", "#07", ""):
            with self.subTest(bad):
                self.assertIsNotNone(hg.session_shape_error("session.arm", dict(s, prs=bad)))

    def test_a_token_whose_pr_set_is_not_a_list_of_ints_refuses_at_load(self) -> None:
        self.arm_ok(scope="merge PRs 1 and 2", prs=[1, 2])
        self.sign(pr=1)
        for bad in (["1"], "1,2", [1.0], [True], [0], {"1": 1}):
            with self.subTest(bad):
                self.write_token(prs=bad)
                ok, refusal = self.check(pr=1)
                self.assertFalse(ok)
                self.assertEqual(refusal.bound, "malformed-token")
                self.assertIn("prs", refusal.detail)

    def test_the_cli_pr_list_parser_is_strict_ascii(self) -> None:
        self.assertEqual(session_auth._parse_prs_arg("#1,2, #30"), ([1, 2, 30], ""))
        for bad in ("1_0", "\u0661", "0", "1,,2", "-3", "1.0", "#"):
            with self.subTest(bad):
                prs, why = session_auth._parse_prs_arg(bad)
                self.assertIsNone(prs)
                self.assertTrue(why)

    def test_status_shows_the_pr_set(self) -> None:
        self.arm_ok(scope="merge PRs 4 and 6", prs=[6, 4])
        ok, line = session_auth.status(self.root, now_s=self.now)
        self.assertIn("prs=#4,#6", line)

    def test_a_bad_pr_set_refuses_to_arm(self) -> None:
        for bad in ([], [0], [-3], ["7"], [True]):
            with self.subTest(bad):
                ok, msg = session_auth.arm(
                    "merge some PRs", self.root, session_id=self.sid, pid=self.pid,
                    now_s=self.now, seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                    runner=_gh(), gate_fn=_yes, prs=bad)
                self.assertFalse(ok, msg)


class TestMergeWrapper(unittest.TestCase):
    """#88: merge_pr orchestration. check() is PATCHED — it has its own suite;
    these prove the wrapper's sequencing and that a refusal aborts before merge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[list[str]] = []

    def _runner(self, merge_rc: int = 0, base: str = "main", default_branch: str = "main",
                children: "list[int] | None" = None):
        children = children or []

        def run(cmd, *a, **k):
            self.calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "author": {"login": "executor"}, "headRefOid": "deadbeefcafe",
                    "baseRefName": base, "headRefName": "fix/x", "title": "a PR"}), stderr="")
            if cmd[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"defaultBranchRef": {"name": default_branch}}), stderr="")
            if cmd[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps([{"number": n} for n in children]), stderr="")
            if cmd[:3] == ["gh", "pr", "edit"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(
                    cmd, merge_rc, stdout="merged", stderr="merge failed")
            if cmd[:3] == [session_auth.MAW_RUN, "repin.check", "sync"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="synced", stderr="")
            if cmd[:3] == [session_auth.MAW_RUN, "repin.check", "check"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="repin: OK — in sync", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return run

    def _did(self, *prefix) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)

    def test_refusal_aborts_before_any_merge(self) -> None:
        with mock.patch.object(
            session_auth, "_authorize",
            return_value=(False, session_auth.Refusal("no-token", "arm first"), ""),
        ):
            ok, msg = session_auth.merge_pr(88, self.root, runner=self._runner(), repo="maw")
        self.assertFalse(ok)
        self.assertIn("no-token", msg)
        self.assertFalse(self._did("gh", "pr", "merge"), "must not merge when auth refuses")

    def test_happy_path_sequences_merge_cleanup_deploy_readback(self) -> None:
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(88, self.root, runner=self._runner(), repo="maw")
        self.assertTrue(ok, msg)
        self.assertTrue(self._did("gh", "pr", "merge", "88"))
        self.assertTrue(self._did(
            "git", "-C", str(self.root), "push", "origin", "--delete", "fix/x"))
        self.assertTrue(self._did(session_auth.MAW_RUN, "repin.check", "sync"))
        self.assertTrue(self._did(session_auth.MAW_RUN, "repin.check", "check"))

    def test_merge_failure_reports_and_skips_deploy(self) -> None:
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(
                88, self.root, runner=self._runner(merge_rc=1), repo="maw")
        self.assertFalse(ok)
        self.assertIn("FAILED", msg)
        self.assertFalse(self._did(session_auth.MAW_RUN, "repin.check", "sync"),
                         "no deploy after a failed merge")

    def test_dry_run_runs_check_but_does_not_merge(self) -> None:
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(
                88, self.root, runner=self._runner(), dry_run=True, repo="maw")
        self.assertTrue(ok)
        self.assertIn("DRY-RUN", msg)
        self.assertFalse(self._did("gh", "pr", "merge"), "dry-run must not merge")

    def _refusals(self) -> list[dict]:
        path = self.root / session_auth.AUDIT_REL
        if not path.exists():
            return []
        recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in recs if r.get("event") == "merge-refused"]

    def test_auth_refusal_is_audited(self) -> None:
        """#206 papercut 1: merge_pr's refusals were never audited."""
        with mock.patch.object(
            session_auth, "_authorize",
            return_value=(False, session_auth.Refusal("no-token", "arm first"), ""),
        ):
            session_auth.merge_pr(88, self.root, runner=self._runner(), repo="maw")
        [rec] = self._refusals()
        self.assertEqual((rec["pr"], rec["repo"], rec["bound"]), (88, "maw", "no-token"))

    def test_base_not_default_refusal_is_audited(self) -> None:
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, _ = session_auth.merge_pr(
                88, self.root, runner=self._runner(base="feature/parent"), repo="maw")
        self.assertFalse(ok)
        [rec] = self._refusals()
        self.assertEqual(rec["bound"], "base-not-default")

    def test_failed_merge_is_audited(self) -> None:
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            session_auth.merge_pr(88, self.root, runner=self._runner(merge_rc=1), repo="maw")
        [rec] = self._refusals()
        self.assertEqual(rec["bound"], "merge")
        self.assertIn("FAILED", rec["detail"])

    def test_successful_and_dry_run_merges_write_no_refusal(self) -> None:
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            session_auth.merge_pr(88, self.root, runner=self._runner(), repo="maw")
            session_auth.merge_pr(88, self.root, runner=self._runner(), dry_run=True, repo="maw")
        self.assertEqual(self._refusals(), [])

    def test_unknown_method_refused(self) -> None:
        ok, msg = session_auth.merge_pr(
            88, self.root, method="frobnicate", runner=self._runner(), repo="maw")
        self.assertFalse(ok)
        self.assertIn("unknown merge method", msg)

    def test_base_not_default_refuses_before_merge(self) -> None:
        # #185 defect 1: a stacked/mistargeted PR (base != default branch) must
        # REFUSE — a merge would land in an unprotected branch under full ceremony.
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(
                88, self.root, repo="maw",
                runner=self._runner(base="reliability/stuck-while-working"))
        self.assertFalse(ok)
        self.assertIn("base-not-default", msg)
        self.assertIn("gh pr edit 88 --base main", msg)  # tells you the fix
        self.assertFalse(self._did("gh", "pr", "merge"), "must not merge a mistargeted PR")

    def test_base_unverifiable_refuses(self) -> None:
        # If the default branch can't be resolved, fail-closed (unverifiable base).
        def runner(cmd, *a, **k):
            self.calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "author": {"login": "e"}, "headRefOid": "deadbeefcafe", "baseRefName": "main",
                    "headRefName": "fix/x", "title": "t"}), stderr="")
            if cmd[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh down")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(88, self.root, repo="maw", runner=runner)
        self.assertFalse(ok)
        self.assertIn("base-unverifiable", msg)
        self.assertFalse(self._did("gh", "pr", "merge"))

    def test_retargets_children_before_deleting_branch(self) -> None:
        # #185 hardening: open children of the merged branch are retargeted onto
        # the default branch BEFORE the branch is deleted, so gh does not auto-close
        # them (the #181 auto-close this run).
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(
                88, self.root, repo="maw", runner=self._runner(children=[190, 191]))
        self.assertTrue(ok, msg)
        self.assertTrue(self._did("gh", "pr", "edit", "190", "--base", "main"))
        self.assertTrue(self._did("gh", "pr", "edit", "191", "--base", "main"))
        # retarget must precede the branch delete
        edit_idx = min(i for i, c in enumerate(self.calls) if c[:3] == ["gh", "pr", "edit"])
        del_idx = min(i for i, c in enumerate(self.calls)
                      if c[:5] == ["git", "-C", str(self.root), "push", "origin"])
        self.assertLess(edit_idx, del_idx, "retarget children before deleting the branch")

    def test_merge_confirmation_echoes_title_and_base(self) -> None:
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(88, self.root, repo="maw", runner=self._runner())
        self.assertTrue(ok, msg)
        self.assertIn("a PR", msg)          # the title, echoed to catch a wrong-PR
        self.assertIn("into main", msg)

    def _runner_child(self, *, list_rc: int = 0, edit_rc: int = 0, children=(190,)):
        """A runner whose child-list / child-retarget outcomes are tunable, to
        prove the delete is HELD when either fails (#187 target-4 ruling)."""
        def run(cmd, *a, **k):
            self.calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "view"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                    "author": {"login": "e"}, "headRefOid": "deadbeefcafe", "baseRefName": "main",
                    "headRefName": "fix/x", "title": "t"}), stderr="")
            if cmd[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"defaultBranchRef": {"name": "main"}}), stderr="")
            if cmd[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(
                    cmd, list_rc, stdout=json.dumps([{"number": n} for n in children]),
                    stderr="list boom")
            if cmd[:3] == ["gh", "pr", "edit"]:
                return subprocess.CompletedProcess(cmd, edit_rc, stdout="", stderr="edit boom")
            if cmd[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return run

    def test_child_retarget_failure_holds_delete(self) -> None:
        # A retarget that FAILS must HOLD the branch delete (else the child auto-
        # closes — the #181 bite). The merge still succeeds; only the delete holds.
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(
                88, self.root, repo="maw", runner=self._runner_child(edit_rc=1))
        self.assertTrue(ok, msg)
        self.assertTrue(self._did("gh", "pr", "merge", "88"))
        self.assertFalse(
            self._did("git", "-C", str(self.root), "push", "origin", "--delete"),
            "delete must be held when a child retarget failed")
        self.assertIn("HELD delete", msg)

    def test_child_list_failure_holds_delete(self) -> None:
        # If we cannot even LIST children, we cannot confirm the branch is safe to
        # delete -> HOLD (fail-closed on the delete).
        with mock.patch.object(session_auth, "_authorize", return_value=(True, None, "deadbeefcafe")):
            ok, msg = session_auth.merge_pr(
                88, self.root, repo="maw", runner=self._runner_child(list_rc=1))
        self.assertTrue(ok, msg)
        self.assertFalse(
            self._did("git", "-C", str(self.root), "push", "origin", "--delete"),
            "delete must be held when the child list failed")
        self.assertIn("HOLDING delete", msg)


class TestExpiryVisibility(SessionAuthTestCase):
    """#103: a PASS echoes the token's remaining window (warning, not policy)."""

    def test_expiry_line_shows_remaining(self) -> None:
        self.arm_ok()
        line = session_auth._expiry_line(self.root, now_s=self.now)
        self.assertIn("token expires", line)
        self.assertIn("left", line)
        self.assertRegex(line, r"[56]h \d+m left")  # 6h cap, ~0 elapsed

    def test_expiry_line_flags_expired(self) -> None:
        self.arm_ok()
        line = session_auth._expiry_line(self.root, now_s=self.now + 7 * 3600)
        self.assertIn("EXPIRED", line)

    def test_expiry_line_empty_without_token(self) -> None:
        self.assertEqual(session_auth._expiry_line(self.root, now_s=self.now), "")


class TestStatus(SessionAuthTestCase):
    """`session_auth status` — the read-only 'is a LIVE token armed?' report. The
    load-bearing property: a token whose arming pid is DEAD reads NOT ARMED, and
    the report can never disagree with check() on the time window (shared
    _time_bounds)."""

    def test_no_token_reads_not_armed(self) -> None:
        ok, msg = session_auth.status(self.root, now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("NOT ARMED", msg)
        self.assertIn("no-token", msg)

    def test_live_armed_token_reports_scope_and_expiry(self) -> None:
        self.arm_ok(scope="merge the arc PRs")
        ok, msg = session_auth.status(self.root, now_s=self.now)
        self.assertTrue(ok, msg)
        self.assertIn("ARMED", msg)
        self.assertIn("merge the arc PRs", msg)
        self.assertIn("expires", msg)          # the expiry line rides along
        self.assertIn(str(self.pid), msg)

    def test_stale_token_from_dead_pid_reads_not_armed(self) -> None:
        # THE run-3 bite: a token left by a session that has since died must NOT
        # read 'armed'. 999999 is not a live pid (os.kill -> ProcessLookupError).
        self.arm_ok()
        self.write_token(pid=999999)
        ok, msg = session_auth.status(self.root, now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("session-gone", msg)
        self.assertIn("STALE", msg)

    def test_expired_token_reads_not_armed(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.status(self.root, now_s=self.now + 7 * 3600)
        self.assertFalse(ok)
        self.assertIn("cap-reached", msg)

    def test_backward_clock_reads_not_armed(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.status(self.root, now_s=self.now - 100)
        self.assertFalse(ok)
        self.assertIn("clock-backward", msg)

    def test_corrupt_json_reads_not_armed(self) -> None:
        (self.root / session_auth.TOKEN_REL).parent.mkdir(parents=True, exist_ok=True)
        (self.root / session_auth.TOKEN_REL).write_text("{not json")
        ok, msg = session_auth.status(self.root, now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("corrupt-token", msg)

    def test_missing_field_reads_not_armed(self) -> None:
        self.arm_ok()
        t = self.token()
        del t["scope"]
        (self.root / session_auth.TOKEN_REL).write_text(json.dumps(t))
        ok, msg = session_auth.status(self.root, now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("malformed-token", msg)

    def test_non_integer_pid_reads_not_armed(self) -> None:
        self.arm_ok()
        self.write_token(pid="not-a-pid")
        ok, msg = session_auth.status(self.root, now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("corrupt-token", msg)

    def test_status_and_check_agree_on_the_time_window(self) -> None:
        # Anti-divergence: the whole reason _time_bounds is shared. An EXPIRED
        # token must trip the SAME bound in both status() and check().
        self.arm_ok()
        self.sign(pr=1)
        late = self.now + 7 * 3600
        s_ok, s_msg = session_auth.status(self.root, now_s=late)
        c_ok, refusal = self.check(now_s=late)
        self.assertFalse(s_ok)
        self.assertFalse(c_ok)
        self.assertIn("cap-reached", s_msg)
        self.assertEqual(refusal.bound, "cap-reached")

    def test_cli_status_exit_codes_and_stdout(self) -> None:
        # No token -> DENY; armed -> PASS. status() uses real time, and arm_ok
        # arms at time.time() with this live pid, so a real-now read is ARMED.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc_none = session_auth.main(["--repo-root", str(self.root), "status"])
        self.assertEqual(rc_none, session_auth.DENY)
        self.assertIn("NOT ARMED", out.getvalue())

        self.arm_ok()
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            rc_armed = session_auth.main(["--repo-root", str(self.root), "status"])
        self.assertEqual(rc_armed, session_auth.PASS)
        self.assertIn("ARMED", out2.getvalue())


class PeerStatusTest(SessionAuthTestCase):
    """The executor's confirm-start check: derived from the registry + the
    advisor's own token, never from a relayed claim."""

    COMMON = "/tmp/example/.git"

    def entry(self, **over):
        e = mock.Mock(name="entry")
        e.name, e.role, e.repo_root = "advisor", "advisor", str(self.root)
        e.repo_common_dir, e.pid, e.session_id = self.COMMON, self.pid, self.sid
        for k, v in over.items():
            setattr(e, k, v)
        return e

    def test_armed_advisor_of_this_repo_passes(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.peer_status(self.COMMON, [self.entry()], now_s=self.now)
        self.assertTrue(ok, msg)
        self.assertIn("advisor ARMED", msg)

    def test_no_token_refuses(self) -> None:
        ok, msg = session_auth.peer_status(self.COMMON, [self.entry()], now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("no-token", msg)

    def test_no_advisor_registered_refuses(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.peer_status(self.COMMON, [], now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("no-advisor", msg)

    def test_an_executor_entry_does_not_count(self) -> None:
        self.arm_ok()
        ok, _ = session_auth.peer_status(self.COMMON, [self.entry(role="executor")], now_s=self.now)
        self.assertFalse(ok)

    def test_another_repos_advisor_does_not_count(self) -> None:
        self.arm_ok()
        ok, _ = session_auth.peer_status(
            self.COMMON, [self.entry(repo_common_dir="/tmp/other/.git")], now_s=self.now)
        self.assertFalse(ok)

    def test_an_empty_common_dir_matches_nothing(self) -> None:
        self.arm_ok()
        ok, _ = session_auth.peer_status("", [self.entry(repo_common_dir="")], now_s=self.now)
        self.assertFalse(ok)

    def test_a_token_armed_by_another_process_refuses(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.peer_status(
            self.COMMON, [self.entry(pid=self.pid + 1)], now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("not-this-seat", msg)

    def test_an_expired_arm_refuses(self) -> None:
        self.arm_ok()
        ok, _ = session_auth.peer_status(
            self.COMMON, [self.entry()], now_s=self.now + 7 * 3600)
        self.assertFalse(ok)

    def test_a_dead_arming_session_refuses(self) -> None:
        self.arm_ok()
        with mock.patch.object(session_auth, "pid_alive", return_value=False):
            ok, msg = session_auth.peer_status(self.COMMON, [self.entry()], now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("session-gone", msg)

    def test_a_token_from_another_session_refuses(self) -> None:
        self.arm_ok()
        ok, msg = session_auth.peer_status(
            self.COMMON, [self.entry(session_id="other-sess")], now_s=self.now)
        self.assertFalse(ok)
        self.assertIn("not-this-seat", msg)

    def test_a_string_pid_in_the_token_refuses(self) -> None:
        self.arm_ok()
        self.write_token(pid=str(self.pid))
        ok, _ = session_auth.peer_status(self.COMMON, [self.entry()], now_s=self.now)
        self.assertFalse(ok)

    def test_a_non_advisor_seat_token_refuses(self) -> None:
        self.arm_ok()
        self.write_token(seat="executor")
        ok, _ = session_auth.peer_status(self.COMMON, [self.entry()], now_s=self.now)
        self.assertFalse(ok)

    def test_the_output_names_the_scope_to_compare_the_goal_against(self) -> None:
        self.arm_ok(scope="merge the session-auth arc PRs")
        ok, msg = session_auth.peer_status(self.COMMON, [self.entry()], now_s=self.now)
        self.assertTrue(ok, msg)
        self.assertIn("merge the session-auth arc PRs", msg)

    def test_an_empty_session_id_on_both_sides_refuses(self) -> None:
        self.arm_ok()
        self.write_token(session_id="")
        ok, _ = session_auth.peer_status(
            self.COMMON, [self.entry(session_id="")], now_s=self.now)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
