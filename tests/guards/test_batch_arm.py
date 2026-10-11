"""Batch arm: one Face ID arms N advisor seats (run 1002b, docs/design/batch-arm.md).

One falsifier per design row (R1-R21, N1). Hermetic like test_session_grant:
keys, the grant ledger and repo_root are tmpdirs; time, identity and the git
runner are injected; the signer is a fixed TEST key. Grants are built through
the production assembler (phone_mint.mint_session.assemble_unsigned_grant).
No Touch ID, Face ID or gh call can fire: gate_fn fails the test if reached.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guards import human_grant as hg  # noqa: E402
from guards import session_auth as sa  # noqa: E402
from guards import typed_approval as ta  # noqa: E402
from phone_mint import mint_session as ms  # noqa: E402
from tests.guards.test_session_grant import (  # noqa: E402
    ROGUE_D, SCOPE, Base, _git, _iso, _sign, setUpModule, tearDownModule,
)

__all__ = ["setUpModule", "tearDownModule"]

GOAL = ["Batch arm grant class and per-line arm", "Relay page cards, CoS tooling"]
OTHER = [
    {"repo": "example-org/alpha", "seat": "advisor", "session_id": "advisor-alpha",
     "scope": "alpha run: lanes 1-2", "goal": ["Ship lane 1"]},
    {"repo": "example-org/beta", "seat": "advisor", "session_id": "advisor-beta",
     "scope": "beta run: the search fix", "goal": ["Fix search"]},
]


class BatchBase(Base):
    def setUp(self) -> None:
        super().setUp()
        ok, msg = sa.record_goal(self.root, GOAL, session_id=self.sid)
        self.assertTrue(ok, msg)

    def request(self, scope: str = SCOPE, at: "float | None" = None) -> dict:
        line, why = sa.batch_request(scope, self.root, runner=_git("maw"),
                                     now_s=self.now - 60 if at is None else at,
                                     session_id=self.sid, seat="advisor")
        self.assertIsNotNone(line, why)
        return line

    def batch_grant(self, batch: list, *, d: int = 0, issued: "float | None" = None,
                    ttl: int = 900, record: bool = True, sign: bool = True,
                    grant_id: "str | None" = None) -> dict:
        g = ms.assemble_unsigned_grant(
            instruction=hg.batch_instruction(batch), scope_class=hg.BATCH_CLASS,
            target="advisor", ttl_seconds=ttl,
            now_iso=_iso(self.now if issued is None else issued),
            pubkey_id="dp-test", batch=batch)
        if grant_id is not None:
            g["grant_id"] = grant_id
        if sign:
            digest = int.from_bytes(hashlib.sha256(hg.canonical_bytes(g)).digest(), "big")
            g["sig"] = _sign(digest, d) if d else _sign(digest)
        if record:
            hg.record_issued(g)
        return g

    def arm_batch(self, g: object, scope: str = SCOPE, now: "float | None" = None):
        return sa.arm(scope, self.root, session_id=self.sid, pid=self.pid,
                      now_s=self.now if now is None else now,
                      gate_fn=lambda _p: self.fail("Touch ID must not run on the grant path"),
                      seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                      runner=_git("maw"), grant=g)

    def record_path(self) -> Path:
        return sa._request_path("example-org/maw", "advisor", self.sid)


class HappyPath(BatchBase):
    def test_one_signature_arms_this_seats_line_only(self) -> None:
        mine = self.request()
        g = self.batch_grant([OTHER[0], mine, OTHER[1]])
        ok, msg = self.arm_batch(g)
        self.assertTrue(ok, msg)
        tok = self.token()
        self.assertEqual(tok["armed_via"], f"grant-batch:{g['grant_id']}:1")
        goal_sha = hashlib.sha256(json.dumps(GOAL, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(tok["goal_sha256"], goal_sha)
        last = self.audit()[-1]
        self.assertEqual((last["event"], last["via"], last["goal_sha256"]),
                         ("arm", f"grant-batch:{g['grant_id']}:1", goal_sha))
        self.assertTrue(hg.is_consumed(hg.line_claim_id(g["grant_id"], 1)))
        self.assertFalse(hg.is_consumed(g["grant_id"]), "the base grant id is never consumed")
        for i in (0, 2):
            self.assertFalse(hg.is_consumed(hg.line_claim_id(g["grant_id"], i)))
        self.assertFalse(self.record_path().exists(), "the request record is spent")

    def test_peer_and_own_status_show_the_goal_hash(self) -> None:
        g = self.batch_grant([self.request()])
        self.assertTrue(self.arm_batch(g)[0])
        ok, line = sa.status(self.root, now_s=self.now)
        self.assertTrue(ok, line)
        self.assertIn(f"goal={self.token()['goal_sha256'][:12]}", line)


class FactsBindTheLine(BatchBase):
    def test_r1_a_scope_the_seat_did_not_ask_for_refuses(self) -> None:
        line = dict(self.request(), scope=SCOPE + "!")
        self.assert_refused_and_audited(self.arm_batch(self.batch_grant([line])),
                                        "arm-refused", "scope grant=")

    def test_r4_an_edited_goal_refuses(self) -> None:
        line = dict(self.request(), goal=["Something the advisor never wrote"])
        self.assert_refused_and_audited(self.arm_batch(self.batch_grant([line])),
                                        "arm-refused", "goal grant=")

    def test_r4_a_goal_artifact_changed_after_the_request_refuses(self) -> None:
        g = self.batch_grant([self.request()])
        sa.record_goal(self.root, ["A different goal now"], session_id=self.sid)
        self.assert_refused_and_audited(self.arm_batch(g), "arm-refused", "goal grant=")

    def test_r4_the_goal_artifact_is_keyed_by_session(self) -> None:
        g = self.batch_grant([self.request()])
        path = sa._run_goal_path(self.root, self.sid)
        path.rename(sa._run_goal_path(self.root, "advisor-restarted"))
        self.assert_refused_and_audited(self.arm_batch(g), "arm-refused", "no run-goal artifact")

    def test_r7_another_seats_line_is_not_claimable_here(self) -> None:
        self.request()
        g = self.batch_grant([OTHER[0]])
        self.assert_refused_and_audited(self.arm_batch(g), "arm-refused", "session_id grant=")
        self.assertFalse(hg.is_consumed(hg.line_claim_id(g["grant_id"], 0)),
                         "the other seat's line is still claimable by it")

    def test_r10_a_restarted_seat_has_a_dead_line(self) -> None:
        g = self.batch_grant([self.request()])
        self.sid = "advisor-s2"  # registry session id after a restart
        sa.record_goal(self.root, GOAL, session_id=self.sid)
        self.assert_refused_and_audited(self.arm_batch(g), "arm-refused", "session_id grant=")


class RequestRecordIsAConfusionGate(BatchBase):
    def test_r5_no_request_record_refuses(self) -> None:
        line = self.request()
        self.record_path().unlink()
        self.assert_refused_and_audited(self.arm_batch(self.batch_grant([line])),
                                        "arm-refused", "no matching batch request record")

    def test_r5_a_record_for_another_line_refuses_and_is_spent(self) -> None:
        self.request(scope=SCOPE)  # the seat asked for scope A
        other = SCOPE + " and the docs tail"
        line_b, _ = sa._batch_facts(other, self.root, self.sid, "advisor", None, _git("maw"))
        ok, msg = self.arm_batch(self.batch_grant([line_b]), scope=other)
        self.assertFalse(ok, msg)
        self.assertIn("no matching batch request record", msg)
        self.assertFalse(self.record_path().exists(), "a signature-valid refusal spends it")

    def test_r5_a_record_newer_than_the_mint_refuses(self) -> None:
        line = self.request(at=self.now + 30)
        self.assert_refused_and_audited(self.arm_batch(self.batch_grant([line])),
                                        "arm-refused", "does not predate the mint")

    def test_r5_a_stale_record_refuses(self) -> None:
        line = self.request(at=self.now - sa.REQUEST_MAX_AGE_S - 1)
        self.assert_refused_and_audited(self.arm_batch(self.batch_grant([line])),
                                        "arm-refused", "does not predate the mint")

    def test_r5_a_signature_valid_refusal_spends_the_record(self) -> None:
        line = self.request(at=self.now + 30)
        self.arm_batch(self.batch_grant([line]))
        self.assertFalse(self.record_path().exists())

    def test_n1_an_unsigned_file_cannot_destroy_the_genuine_record(self) -> None:
        line = self.request()
        for forged in (self.batch_grant([line], sign=False, record=False),
                       self.batch_grant([line], d=ROGUE_D)):
            ok, msg = self.arm_batch(forged)
            self.assertFalse(ok, msg)
            self.assertTrue(self.record_path().exists(), "a forged file deleted the record")
        ok, msg = self.arm_batch(self.batch_grant([line]))
        self.assertTrue(ok, msg)


class SameSessionIdInTwoRepos(BatchBase):
    """#338, effect (b): two seats in two repos that file a grant-request
    under the same session id (registered in the same second, pre-fix) must
    not overwrite each other's request record."""

    def peer_request(self) -> "tuple[Path, dict]":
        peer = self.root.parent / "peer-repo"
        peer.mkdir()
        ok, msg = sa.record_goal(peer, ["Ship the example planner"], session_id=self.sid)
        self.assertTrue(ok, msg)
        line, why = sa.batch_request("peer-repo run: the example planner", peer,
                                     runner=_git("peer-repo"), now_s=self.now - 60,
                                     session_id=self.sid, seat="advisor")
        self.assertIsNotNone(line, why)
        return peer, line

    def test_peer_request_in_the_same_second_does_not_clobber_this_seats(self) -> None:
        mine = self.request()
        _peer, theirs = self.peer_request()  # same sid, same second, other repo
        self.assertEqual(theirs["session_id"], mine["session_id"])
        ok, msg = self.arm_batch(self.batch_grant([mine]))
        self.assertTrue(ok, msg)
        peer_path = sa._request_path("example-org/peer-repo", "advisor", self.sid)
        self.assertTrue(peer_path.exists(), "this seat's arm spent the peer's record")
        self.assertEqual(json.loads(peer_path.read_text())["line"], theirs)

    def test_a_pre_338_record_of_this_seats_own_line_still_arms(self) -> None:
        mine = self.request()
        legacy = sa._legacy_request_path(self.sid)
        self.record_path().replace(legacy)
        ok, msg = self.arm_batch(self.batch_grant([mine]))
        self.assertTrue(ok, msg)
        self.assertFalse(legacy.exists(), "the legacy record is spent")

    def test_a_pre_338_record_of_another_seats_line_is_neither_used_nor_spent(self) -> None:
        mine = self.request()
        self.record_path().unlink()
        legacy = sa._legacy_request_path(self.sid)
        legacy.write_text(json.dumps({"line": dict(mine, repo="example-org/peer-repo"),
                                      "created_at": _iso(self.now - 60)}), encoding="utf-8")
        ok, msg = self.arm_batch(self.batch_grant([mine]))
        self.assertFalse(ok)
        self.assertIn("no matching batch request record", msg)
        self.assertTrue(legacy.exists(), "another seat's legacy record was deleted")


class SingleUseAndClasses(BatchBase):
    def test_r6_a_line_cannot_be_claimed_twice(self) -> None:
        line = self.request()
        g = self.batch_grant([line])
        self.assertTrue(self.arm_batch(g)[0])
        sa._runtime_path(self.root, sa.TOKEN_REL).unlink()
        self.request()
        self.assert_refused_and_audited(self.arm_batch(g), "arm-refused", "already claimed")

    def test_r6_concurrent_line_claims_have_one_winner(self) -> None:
        import subprocess
        claim = hg.line_claim_id("g-x", 0)
        maw = str(Path(__file__).resolve().parents[2] / "maw")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from guards import human_grant as hg;"
                "print(hg.consume(sys.argv[2], 'n'))")
        procs = [subprocess.Popen([sys.executable, "-c", code, maw, claim], env=dict(os.environ),
                                  stdout=subprocess.PIPE, text=True) for _ in range(8)]
        wins = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(sorted(wins), ["False"] * 7 + ["True"])

    def test_r8_a_batch_grant_never_passes_the_single_ceremony(self) -> None:
        g = self.batch_grant([self.request()])
        facts = {"repo": "example-org/maw", "seat": "advisor", "session_id": self.sid,
                 "scope": SCOPE}
        gid, why = sa._session_grant_ceremony(g, "session.arm", facts, self.now)
        self.assertEqual(gid, "")
        self.assertIn("session.arm.batch", why)

    def test_r8_a_single_grant_never_passes_the_batch_ceremony(self) -> None:
        single = self.grant("session.arm", self.arm_session())
        gid, i, why = sa._batch_grant_ceremony(single, self.request(), self.now)
        self.assertEqual((gid, i), ("", -1))
        self.assertIn("needs 'session.arm.batch'", why)

    def test_r16_verify_refuses_a_batch_grant_outside_the_ceremony(self) -> None:
        g = self.batch_grant([self.request()])
        res = hg.verify(g, hg.batch_instruction(g["batch"]), _iso(self.now))
        self.assertFalse(res.ok)
        self.assertIn("only inside the session_auth ceremony", res.reason)
        self.assertNotIn(hg.BATCH_CLASS, hg.INSTRUCTION_CLASSES)

    def test_r16_a_typed_scope_amend_verifies_only_inside_the_ceremony(self) -> None:
        from unittest import mock
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": self.sid,
             "from_scope": SCOPE, "scope": SCOPE + " plus docs"}
        instruction = hg.session_instruction("scope.amend", s)
        with mock.patch.object(ta, "derive_recorder", return_value=("cos", os.getpid())):
            rec = ta.record(instruction=instruction, scope_class="scope.amend",
                            target="advisor", dp_text="yes", channel="remote-control",
                            ttl_seconds=600, now_iso=_iso(self.now), session=s)
        res = ta.verify(rec, instruction, _iso(self.now))
        self.assertFalse(res.ok)
        self.assertIn("only inside the session_auth ceremony", res.reason)
        self.assertTrue(ta.verify(rec, instruction, _iso(self.now), ceremony=True).ok)

    def test_r21_a_live_token_is_never_re_armed_by_a_batch_line(self) -> None:
        self.assertTrue(self.arm_batch(self.batch_grant([self.request()]))[0])
        before = self.token()
        self.request()
        ok, msg = self.arm_batch(self.batch_grant([self.request()]))
        self.assertFalse(ok, msg)
        self.assertIn("already holds a live token", msg)
        self.assertEqual(self.token(), before)


class Expiry(BatchBase):
    def test_r9_r11_a_line_must_be_claimed_inside_the_ttl(self) -> None:
        line = self.request()
        late = self.batch_grant([line], ttl=600)
        self.assert_refused_and_audited(self.arm_batch(late, now=self.now + 601),
                                        "arm-refused", "expired")
        self.assertFalse(hg.is_consumed(hg.line_claim_id(late["grant_id"], 0)))
        self.request()
        ok, msg = self.arm_batch(self.batch_grant([self.request()], ttl=600), now=self.now + 599)
        self.assertTrue(ok, msg)

    def test_r13_a_changed_keyset_refuses_and_consumes_nothing(self) -> None:
        g = self.batch_grant([self.request()])
        (self.keys / "dp-new.pub").write_text((self.keys / "dp-test.pub").read_text()
                                              .replace("dp-test", "dp-new"), encoding="utf-8")
        ok, msg = self.arm_batch(g)
        self.assertFalse(ok, msg)
        self.assertIn("key", msg.lower())
        self.assertFalse(hg.is_consumed(hg.line_claim_id(g["grant_id"], 0)))
        self.assertTrue(self.record_path().exists(), "an alarm refusal mutated state")


class Shape(BatchBase):
    def line(self, **over) -> dict:
        return {"repo": "example-org/x", "seat": "advisor", "session_id": "s-x",
                "scope": "a long enough scope", "goal": ["one"], **over}

    def assert_refused_both(self, batch: list, needle: str) -> None:
        with self.assertRaisesRegex(ValueError, needle):
            ms.assemble_unsigned_grant(instruction=hg.batch_instruction(batch),
                                       scope_class=hg.BATCH_CLASS, target="advisor",
                                       ttl_seconds=600, now_iso=_iso(self.now),
                                       pubkey_id="dp-test", batch=batch)
        self.assertIn(needle.replace("\\", ""), hg.batch_shape_error(hg.BATCH_CLASS,
                                                                     {"batch": batch}) or "")

    def test_r12_caps(self) -> None:
        many = [self.line(repo=f"o/r{i}", session_id=f"s{i}") for i in range(hg.MAX_BATCH_LINES + 1)]
        self.assert_refused_both(many, "1..8 lines")
        self.assert_refused_both([self.line(scope="x" * (hg.BATCH_SCOPE_MAX + 1))], "the cap is 200")
        self.assert_refused_both([self.line(goal=["a", "b", "c", "d"])], "1..3 bullets")
        self.assert_refused_both([self.line(goal=["x" * (hg.GOAL_BULLET_MAX + 1)])], "the cap is 120")
        self.assertIsNone(hg.batch_shape_error(hg.BATCH_CLASS, {"batch": [
            self.line(scope="x" * hg.BATCH_SCOPE_MAX, goal=["y" * hg.GOAL_BULLET_MAX] * 3)]}))

    def test_a_line_naming_a_seat_that_cannot_merge_refuses(self) -> None:
        self.assert_refused_both([self.line(seat="executor")], "not a seat that may merge")
        self.assertEqual(hg.BATCH_SEATS, sa.MERGE_AUTHORIZED_SEATS)

    def test_r14_duplicate_repos_or_sessions_refuse(self) -> None:
        self.assert_refused_both([self.line(), self.line(session_id="s-y")], "repeat a repo")
        self.assert_refused_both([self.line(), self.line(repo="o/y")], "repeat a session_id")

    def test_non_ascii_and_unknown_line_keys_refuse(self) -> None:
        self.assert_refused_both([self.line(scope="lanes 1—2 only")], "U\\+2014")
        self.assert_refused_both([self.line(extra="x")], "must have exactly")

    def test_f3_batch_key_is_refused_on_every_other_class(self) -> None:
        for value in (None, [self.line()]):
            with self.subTest(value=value):
                g = self.grant("session.arm", self.arm_session())
                g = {**g, "batch": value}
                res = hg.verify(g, hg.session_instruction("session.arm", g["session"]),
                                _iso(self.now), ceremony=True)
                self.assertFalse(res.ok)
                self.assertIn("batch present on a non-batch grant", res.reason)

    def test_f4_grant_ids_outside_the_relay_rule_refuse(self) -> None:
        for bad in ("g#1", "line:g:0", "g 1", "x" * 81):
            with self.subTest(gid=bad):
                g = self.batch_grant([self.request()], grant_id=bad)
                res = hg.verify(g, hg.batch_instruction(g["batch"]), _iso(self.now),
                                ceremony=True)
                self.assertFalse(res.ok)
                self.assertIn("grant_id must match", res.reason)

    def test_the_batch_class_is_passkey_and_phone_only(self) -> None:
        self.assertIn("passkey only", ta.class_refusal(hg.BATCH_CLASS) or "")
        from guards import mint as gmint
        batch = [self.line()]
        with self.assertRaisesRegex(ValueError, "phone-only"):
            gmint.mint(hg.batch_instruction(batch), hg.BATCH_CLASS, "advisor", 600, "dp-test",
                       _iso(self.now), lambda *_a, **_k: self.fail("the Touch ID signer ran"),
                       show=lambda _t: None, record=False)


class Cli(BatchBase):
    def run_main(self, *argv: str) -> "tuple[int, str]":
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = sa.main(["--repo-root", str(self.root), *argv])
        return rc, err.getvalue()

    def test_batch_is_for_session_arm_only(self) -> None:
        rc, err = self.run_main("grant-request", "scope.amend", "--scope", SCOPE, "--batch")
        self.assertNotEqual(rc, 0)
        self.assertIn("--batch is for session.arm only", err)

    def test_goal_record_refuses_an_unreadable_file(self) -> None:
        rc, err = self.run_main("goal-record", "--file", str(self.root / "missing.txt"))
        self.assertNotEqual(rc, 0)
        self.assertIn("refusing to record the run goal", err)

    def test_goal_record_refuses_four_bullets(self) -> None:
        f = self.root / "goal.txt"
        f.write_text("a\nb\nc\nd\n", encoding="utf-8")
        ok, msg = sa.record_goal(self.root, f.read_text().splitlines(), session_id=self.sid)
        self.assertFalse(ok)
        self.assertIn("1..3 bullets", msg)


if __name__ == "__main__":
    unittest.main()
