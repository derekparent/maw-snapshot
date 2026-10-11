"""guards.typed_approval — the operator's typed backup approval (refit R3c; DECISIONS.md
2026-09-23, ruling 4).

The load-bearing tests are the refusals: session.arm and merge.approval are
passkey only, a typed record never verifies as a signed grant, and it renders
Operator-typed (never OPERATOR-AUTHORIZED) through the real send/recv path.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MAW = Path(__file__).resolve().parents[2] / "maw"
sys.path.insert(0, str(MAW))

from guards import human_grant as hg  # noqa: E402
from guards import session_auth as sa  # noqa: E402
from guards import typed_approval as ta  # noqa: E402
from net import registry  # noqa: E402

SCOPE = "maw refit run: R1, R2 and R3 lanes, then the advisor-goal tail"
NEW_SCOPE = "maw refit run: R1-R3 lanes plus T1 duplicate-pair guard only"


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _git(owner_repo: str = "example-org/maw"):
    def run(cmd, *a, **k):
        if cmd[:1] == ["git"] and "get-url" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"git@github.com:{owner_repo}.git\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")
    return run


class Base(unittest.TestCase):
    """A hermetic switchboard (HOME + MAW_NET_DIR) where THIS process is
    registered as `cos` (the ancestor lookup is pinned to our own pid)."""

    lineage_role = "cos"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root / "repo"
        self.root.mkdir()
        (root / "home").mkdir()
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR", "TMUX", "MAW_"))}
        clean.update(HOME=str(root / "home"), MAW_NET_DIR=str(root / "net"),
                     MAW_GRANTS_DIR=str(root / "grants"), MAW_GRANT_KEYS_DIR=str(root / "keys"))
        env = mock.patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)
        anc = mock.patch.object(registry, "find_claude_ancestor", return_value=(os.getpid(), None))
        anc.start()
        self.addCleanup(anc.stop)
        (root / "keys").mkdir()
        hg.ack_keyset()
        self.now = time.time()
        if self.lineage_role:
            registry.register(name=self.lineage_role, role=self.lineage_role)

    def ledger(self) -> list[dict]:
        return hg._ledger_events()

    def rec(self, scope_class: str = "goal.stage", instruction: str = "stage the goal doc",
            **kw) -> dict:
        args = dict(instruction=instruction, scope_class=scope_class, target="maw-advisor",
                    dp_text="yes, stage it", channel="remote-control", ttl_seconds=600,
                    now_iso=_iso(self.now))
        args.update(kw)
        return ta.record(**args)


class RecordAndVerify(Base):
    def test_the_four_kinds_record_and_verify(self) -> None:
        for klass in ("goal.stage", "prompt.relay", "unblock.instruct"):
            with self.subTest(klass):
                r = self.rec(klass)
                self.assertTrue(ta.verify(r, "stage the goal doc", _iso(self.now)).ok)
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": "s",
             "from_scope": SCOPE, "scope": NEW_SCOPE}
        r = self.rec("scope.amend", instruction=hg.session_instruction("scope.amend", s), session=s)
        self.assertTrue(ta.verify(r, hg.session_instruction("scope.amend", s), _iso(self.now),
                                  ceremony=True).ok)

    def test_passkey_only_and_unlisted_kinds_refuse_at_record(self) -> None:
        for klass, needle in (("session.arm", "passkey only"), ("merge.approval", "passkey only"),
                              ("seat.standdown", "not a typed-approval kind")):
            with self.subTest(klass), self.assertRaisesRegex(ValueError, needle):
                self.rec(klass)

    def test_passkey_only_kinds_refuse_at_verify_even_if_hand_built(self) -> None:
        for klass in ("session.arm", "merge.approval"):
            with self.subTest(klass):
                r = dict(self.rec(), scope_class=klass)
                res = ta.verify(r, "stage the goal doc", _iso(self.now))
                self.assertFalse(res.ok)
                self.assertIn("passkey only", res.reason)

    def test_record_refuses_email_and_blank_dp_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "emailed code"):
            self.rec(channel="email")
        with self.assertRaisesRegex(ValueError, "dp_text"):
            self.rec(dp_text="  ")
        with self.assertRaisesRegex(ValueError, "ttl"):
            self.rec(ttl_seconds=hg.MAX_TTL_SECONDS + 1)

    def test_typed_record_never_verifies_as_a_signed_grant(self) -> None:
        r = self.rec()
        res = hg.verify(r, "stage the goal doc", _iso(self.now))
        self.assertFalse(res.ok)
        self.assertIn("not a signed grant", res.reason)

    def test_expired_replayed_unrecorded_and_mismatched_refuse(self) -> None:
        r = self.rec()
        self.assertIn("expired", ta.verify(r, "stage the goal doc", _iso(self.now + 601)).reason)
        self.assertIn("hash mismatch", ta.verify(r, "stage a DIFFERENT goal", _iso(self.now)).reason)
        self.assertTrue(hg.consume(r["grant_id"], r["nonce"]))
        self.assertIn("replay", ta.verify(r, "stage the goal doc", _iso(self.now)).reason)
        ghost = self.rec(write=False)
        self.assertIn("issued ledger", ta.verify(ghost, "stage the goal doc", _iso(self.now)).reason)

    def test_issued_line_carries_the_derived_recorder(self) -> None:
        r = self.rec()
        self.assertEqual(r["typed"]["recorded_by"], "cos")
        line = [e for e in self.ledger() if e.get("grant_id") == r["grant_id"]][0]
        self.assertEqual((line["recorded_by"], line["recorder_role"], line["recorder_pid"]),
                         ("cos", "cos", os.getpid()))

    def test_hand_written_issued_line_without_a_recorder_refuses(self) -> None:
        r = self.rec(write=False)
        hg._append_event({"event": "issued", "grant_id": r["grant_id"], "carrier": "typed"})
        self.assertIn("no derived coordinator recorder",
                      ta.verify(r, "stage the goal doc", _iso(self.now)).reason)
        r2 = self.rec(write=False)
        hg._append_event({"event": "issued", "grant_id": r2["grant_id"], "carrier": "typed",
                          "recorded_by": "cos", "recorder_role": "cos"})  # no recorder_pid
        self.assertIn("no derived coordinator recorder",
                      ta.verify(r2, "stage the goal doc", _iso(self.now)).reason)

    def test_record_body_cannot_rename_its_recorder(self) -> None:
        r = self.rec()
        r["typed"] = dict(r["typed"], recorded_by="dp")
        self.assertIn("the ledger says 'cos'", ta.verify(r, "stage the goal doc", _iso(self.now)).reason)

    def test_recorder_cannot_consume_and_only_the_recorder_may_relay(self) -> None:
        r = self.rec()
        self.assertIn("cannot also act", ta.verify(r, "stage the goal doc", _iso(self.now),
                                                   consumer="cos").reason)
        self.assertIn("not by its recorder", ta.verify(r, "stage the goal doc", _iso(self.now),
                                                       relayed_by="advisor").reason)
        self.assertTrue(ta.verify(r, "stage the goal doc", _iso(self.now),
                                  consumer="executor", relayed_by="cos").ok)

    def test_a_signed_grants_issued_entry_does_not_admit_a_typed_record(self) -> None:
        r = self.rec(write=False)
        hg.record_issued(r)  # an 'issued' line WITHOUT carrier=typed
        self.assertIn("issued ledger", ta.verify(r, "stage the goal doc", _iso(self.now)).reason)

    def test_signature_carrier_on_a_typed_record_refuses(self) -> None:
        r = dict(self.rec(), sig="AAAA")
        self.assertIn("carries no signature", ta.verify(r, "stage the goal doc", _iso(self.now)).reason)


class PayloadBoundToTheIssuedLine(Base):
    """L0 #2: the issued ledger line binds the WHOLE approved payload, so an
    unconsumed receipt cannot be re-presented with a different meaning."""

    def test_a_goal_stage_receipt_cannot_be_repurposed_as_a_scope_amend(self) -> None:
        # Codex repro: keep the ledger, change the payload, recompute its hash.
        r = self.rec("goal.stage")
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": "s",
             "from_scope": SCOPE, "scope": NEW_SCOPE}
        forged_instruction = hg.session_instruction("scope.amend", s)
        forged = dict(r, scope_class="scope.amend", session=s,
                      instruction_sha256=hashlib.sha256(
                          forged_instruction.encode("utf-8")).hexdigest())
        res = ta.verify(forged, forged_instruction, _iso(self.now), ceremony=True)
        self.assertFalse(res.ok, "a re-presented receipt verified")
        self.assertIn("payload", res.reason)

    def test_every_payload_field_is_bound(self) -> None:
        r = self.rec("goal.stage")
        for field, value in (("target", "another-seat"), ("ttl_seconds", 5),
                             ("issued_at", _iso(self.now + 1)), ("nonce", "n2"),
                             ("typed", dict(r["typed"], dp_text="something else"))):
            with self.subTest(field):
                res = ta.verify(dict(r, **{field: value}), "stage the goal doc",
                                _iso(self.now + 2))
                self.assertFalse(res.ok, field)

    def test_the_issued_line_carries_the_payload_digest(self) -> None:
        r = self.rec("goal.stage")
        [line] = [e for e in self.ledger()
                  if e.get("event") == "issued" and e.get("grant_id") == r["grant_id"]]
        self.assertEqual(line["payload_sha256"],
                         hashlib.sha256(hg.canonical_bytes(r)).hexdigest())

    def test_an_issued_line_without_a_digest_refuses(self) -> None:
        r = self.rec("goal.stage", write=False)
        hg._append_event({"event": "issued", "grant_id": r["grant_id"], "carrier": "typed",
                          "scope_class": "goal.stage", "channel": "remote-control",
                          "recorded_by": "cos", "recorder_role": "cos",
                          "recorder_pid": os.getpid(), "ts": _iso(self.now)})
        res = ta.verify(r, "stage the goal doc", _iso(self.now))
        self.assertFalse(res.ok)
        self.assertIn("payload", res.reason)


class MalformedTypedInputRejects(Base):
    def test_a_list_valued_scope_class_rejects_instead_of_raising(self) -> None:
        r = dict(self.rec(), scope_class=["goal.stage"])
        res = ta.verify(r, "stage the goal doc", _iso(self.now))
        self.assertFalse(res.ok)

    def test_a_non_json_field_rejects_instead_of_raising(self) -> None:
        r = dict(self.rec(), target={"a", "b"})
        res = ta.verify(r, "stage the goal doc", _iso(self.now))
        self.assertFalse(res.ok)


class SeatLineageRefused(Base):
    lineage_role = ""

    def test_advisor_and_executor_lineage_cannot_record(self) -> None:
        for role in ("advisor", "executor"):
            with self.subTest(role):
                registry.register(name=role, role=role)
                with self.assertRaisesRegex(ValueError, f"a {role} seat cannot record"):
                    self.rec()
                registry.unregister(role)
        refused = [e for e in self.ledger() if e.get("event") == "record-refused"]
        self.assertEqual(len(refused), 2)
        self.assertFalse([e for e in self.ledger() if e.get("event") == "issued"])

    def test_a_seat_also_registered_as_cos_is_still_refused(self) -> None:
        registry.register(name="cos", role="cos")
        registry.register(name="advisor", role="advisor")
        with self.assertRaisesRegex(ValueError, "advisor seat cannot record"):
            self.rec()

    def test_unregistered_process_cannot_record(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a registered live 'cos' session"):
            self.rec()


class SessionAuthTyped(Base):
    sid = "advisor-s1"

    def arm_touch_id(self, pid: "int | None" = None) -> None:
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=pid or os.getpid(), now_s=self.now,
                         gate_fn=lambda _p: True, seat="advisor",
                         env={"MAW_BRIDGE_ROLE": "advisor"}, runner=_git())
        self.assertTrue(ok, msg)

    def audit(self) -> list[dict]:
        p = sa._runtime_path(self.root, sa.AUDIT_REL)
        return [json.loads(x) for x in p.read_text().splitlines()]

    def test_typed_session_arm_can_never_arm(self) -> None:
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": self.sid, "scope": SCOPE}
        r = dict(self.rec(), scope_class="session.arm", session=s,
                 instruction_sha256=hashlib.sha256(
                     hg.session_instruction("session.arm", s).encode()).hexdigest())
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=os.getpid(), now_s=self.now,
                         gate_fn=lambda _p: self.fail("Touch ID ran"), seat="advisor",
                         env={"MAW_BRIDGE_ROLE": "advisor"}, runner=_git(), grant=r)
        self.assertFalse(ok)
        self.assertIn("passkey only", msg)
        self.assertEqual(self.audit()[-1]["event"], "arm-refused")
        self.assertFalse(sa._runtime_path(self.root, sa.TOKEN_REL).exists())

    def amend_rec(self, **over) -> dict:
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": self.sid,
             "from_scope": SCOPE, "scope": NEW_SCOPE}
        s.update(over)
        return self.rec("scope.amend", instruction=hg.session_instruction("scope.amend", s), session=s)

    def test_typed_amend_within_the_same_repo_amends_and_says_so(self) -> None:
        # The armed advisor is a DIFFERENT live process than the recording CoS.
        advisor_pid = os.getppid()
        self.arm_touch_id(pid=advisor_pid)
        before = json.loads(sa._runtime_path(self.root, sa.TOKEN_REL).read_text())
        ok, msg = sa.amend(NEW_SCOPE, self.root, self.amend_rec(), session_id=self.sid,
                           pid=advisor_pid, now_s=self.now + 30, runner=_git())
        self.assertTrue(ok, msg)
        self.assertIn("OPERATOR-TYPED", msg)
        tok = json.loads(sa._runtime_path(self.root, sa.TOKEN_REL).read_text())
        self.assertEqual(tok["scope"], NEW_SCOPE)
        self.assertEqual(tok["expires_at"], before["expires_at"])
        self.assertEqual(tok["amended"][-1]["carrier"], "typed")
        self.assertEqual(self.audit()[-1]["carrier"], "typed")

    def _arm_with_prs(self, prs):
        advisor_pid = os.getppid()
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=advisor_pid,
                         now_s=self.now, gate_fn=lambda _p: True, seat="advisor",
                         env={"MAW_BRIDGE_ROLE": "advisor"}, runner=_git(), prs=prs)
        self.assertTrue(ok, msg)
        return advisor_pid

    def _typed_amend(self, pid, prs):
        rec = self.amend_rec(prs=sa._prs_text(prs))
        return sa.amend(NEW_SCOPE, self.root, rec, session_id=self.sid, pid=pid,
                        now_s=self.now + 30, runner=_git(), prs=prs)

    def test_a_typed_amend_may_shrink_the_pr_set(self) -> None:
        pid = self._arm_with_prs([1, 2, 3])
        ok, msg = self._typed_amend(pid, [2])
        self.assertTrue(ok, msg)
        tok = json.loads(sa._runtime_path(self.root, sa.TOKEN_REL).read_text())
        self.assertEqual(tok["prs"], [2])

    def test_a_typed_amend_may_not_widen_the_pr_set(self) -> None:
        pid = self._arm_with_prs([1, 2])
        ok, msg = self._typed_amend(pid, [1, 2, 9])
        self.assertFalse(ok, msg)
        self.assertIn("passkey", msg)

    def test_a_typed_amend_may_not_add_a_set_to_an_unbounded_session(self) -> None:
        pid = self._arm_with_prs(None)
        ok, msg = self._typed_amend(pid, [4])
        self.assertFalse(ok, msg)
        self.assertIn("passkey", msg)

    def test_a_repurposed_goal_stage_receipt_cannot_amend_the_scope(self) -> None:
        # L0 #2 end-to-end: the Codex repro, driven through session_auth.amend.
        advisor_pid = os.getppid()
        self.arm_touch_id(pid=advisor_pid)
        genuine = self.rec("goal.stage")
        legit = self.amend_rec()
        forged = dict(genuine, scope_class="scope.amend", session=legit["session"],
                      instruction_sha256=legit["instruction_sha256"])
        ok, msg = sa.amend(NEW_SCOPE, self.root, forged, session_id=self.sid,
                           pid=advisor_pid, now_s=self.now + 30, runner=_git())
        self.assertFalse(ok, msg)
        self.assertIn("payload", msg)
        tok = json.loads(sa._runtime_path(self.root, sa.TOKEN_REL).read_text())
        self.assertEqual(tok["scope"], SCOPE)

    def test_the_armed_session_cannot_launder_itself_into_the_recorder(self) -> None:
        # The token is armed under THIS pid; the `cos` entry from setUp shares it,
        # exactly the unregister -> register --role cos -> record -> amend path.
        self.arm_touch_id()
        ok, msg = sa.amend(NEW_SCOPE, self.root, self.amend_rec(), session_id=self.sid,
                           pid=os.getpid(), now_s=self.now + 30, runner=_git())
        self.assertFalse(ok)
        self.assertIn("recorded by the armed session itself", msg)
        self.assertEqual(self.audit()[-1]["event"], "amend-refused")

    def test_typed_amend_to_another_repo_refuses(self) -> None:
        self.arm_touch_id(pid=os.getppid())
        ok, msg = sa.amend(NEW_SCOPE, self.root, self.amend_rec(repo="example-org/widget"),
                           session_id=self.sid, pid=os.getppid(), now_s=self.now + 30, runner=_git())
        self.assertFalse(ok)
        self.assertIn("example-org/widget", msg)
        self.assertEqual(self.audit()[-1]["event"], "amend-refused")


class WireRecvLeavesTypedAmend(Base):
    """R16 (run 1002b), wire leg: a typed scope.amend from the CoS is consumed by
    `session_auth amend`, never by the recv that carried it."""

    def test_wire_recv_does_not_consume_a_typed_scope_amend(self) -> None:
        import base64
        import types
        from net import cli
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": "s",
             "from_scope": SCOPE, "scope": NEW_SCOPE}
        instruction = hg.session_instruction("scope.amend", s)
        r = self.rec("scope.amend", instruction=instruction, session=s)
        turn = types.SimpleNamespace(grant=base64.b64encode(json.dumps(r).encode()).decode(),
                                     body=instruction, role="cos")
        me = registry.Entry(name="maw-advisor", role="advisor", repo_root="", cwd="", pid=1,
                            tty=None, tmux_pane=None, session_id="s", started_at="", last_seen="")
        status, note = cli._wire_grant_trust(turn, me=me)
        self.assertEqual(status, "plain")
        self.assertIn("session ceremony input", note)
        self.assertFalse(hg.is_consumed(r["grant_id"]))
        self.assertTrue(ta.verify(r, instruction, _iso(self.now), ceremony=True).ok)


class RecvRendersDpTyped(unittest.TestCase):
    """Through the REAL send --grant-file + recv, isolated HOME/switchboard/ledger.
    Every step runs as a subprocess so record() and register() share one lineage."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (root / "home").mkdir()
        (root / "keys").mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("HERDR", "TMUX", "MAW_"))}
        self.env.update(HOME=str(root / "home"), MAW_NET_DIR=str(root / "net"),
                        MAW_GRANTS_DIR=str(root / "grants"), MAW_GRANT_KEYS_DIR=str(root / "keys"),
                        PYTHONPATH=str(MAW))
        for name in ("advisor", "executor"):
            cp = self.run_mod("net.cli", "register", "--name", name, "--role", name)
            self.assertEqual(cp.returncode, 0, cp.stderr)
        # The CoS is a DIFFERENT session: run its steps under a shell named
        # `claude`, so registry.find_claude_ancestor() resolves them to that
        # shell's pid, not to the seats' shared lineage.
        self.fake_claude = root / "claude"
        self.fake_claude.symlink_to("/bin/sh")

    def run_mod(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", *args], cwd=str(self.repo), env=self.env,
                              capture_output=True, text=True, timeout=60)

    def as_cos(self, script: str) -> subprocess.CompletedProcess:
        """Run shell `script` inside ONE fake-claude session (the CoS)."""
        return subprocess.run([str(self.fake_claude), "-c", script], cwd=str(self.repo),
                              env=self.env, capture_output=True, text=True, timeout=120)

    def cos_record_and_send(self, body: str, klass: str = "goal.stage",
                            mutate: str = "", send: bool = True,
                            target: str = "executor") -> subprocess.CompletedProcess:
        root = Path(self.tmp.name)
        (root / "instr.txt").write_text(body, encoding="utf-8")
        (root / "dp.txt").write_text("go ahead", encoding="utf-8")
        (root / "body.txt").write_text(body, encoding="utf-8")
        py = sys.executable
        g = root / "typed.json"
        script = (
            f"{py} -m net.cli register --name cos --role cos >/dev/null && "
            f"{py} -m guards.typed_approval record --scope-class {klass} --target {target} "
            f"--instruction-file {root / 'instr.txt'} --dp-text-file {root / 'dp.txt'} "
            f"--channel cos-terminal --out {g} && {mutate} "
            + (f"{py} -m net.cli send executor --as cos --from-file {root / 'body.txt'} "
               f"--grant-file {g}; " if send else "true; ")
        )
        cp = self.as_cos(script)
        self.assertTrue(g.exists(), cp.stderr)
        return cp

    def relay_from_seat(self, sender: str, body: str) -> subprocess.CompletedProcess:
        root = Path(self.tmp.name)
        (root / "body.txt").write_text(body, encoding="utf-8")
        self.run_mod("net.cli", "send", "executor", "--as", sender, "--from-file",
                     str(root / "body.txt"), "--grant-file", str(root / "typed.json"))
        return self.run_mod("net.cli", "recv", "executor")

    def test_typed_from_the_cos_on_the_wire_renders_dp_typed(self) -> None:
        body = "stage the next goal doc now"
        self.cos_record_and_send(body)
        out = self.run_mod("net.cli", "recv", "executor")
        self.assertIn("[via wire pull]", out.stdout)
        self.assertIn("trust: OPERATOR-TYPED", out.stdout)
        self.assertIn("recorded by cos", out.stdout)
        self.assertNotIn("OPERATOR-AUTHORIZED", out.stdout)
        # replayed by the CoS: consumed already, renders plain
        root = Path(self.tmp.name)
        cp = self.as_cos(f"{sys.executable} -m net.cli register --name cos --role cos >/dev/null && "
                         f"{sys.executable} -m net.cli send executor --as cos --from-file "
                         f"{root / 'body.txt'} --grant-file {root / 'typed.json'}")
        self.assertIn("parked on wire", cp.stderr)
        again = self.run_mod("net.cli", "recv", "executor")
        self.assertNotIn("OPERATOR-TYPED", again.stdout)
        self.assertIn("replay", again.stderr)

    def test_a_typed_approval_for_another_seat_renders_plain(self) -> None:
        # L0 #10 (advisor follow-up): the typed target is enforced like a grant's.
        body = "stage the next goal doc now"
        self.cos_record_and_send(body, target="advisor")
        out = self.run_mod("net.cli", "recv", "executor")
        self.assertNotIn("OPERATOR-TYPED", out.stdout)
        self.assertIn("target", out.stderr + out.stdout)

    def test_typed_relayed_over_the_bridge_renders_plain(self) -> None:
        body = "stage the next goal doc now"
        self.cos_record_and_send(body, send=False)
        out = self.relay_from_seat("advisor", body)
        self.assertIn("[via bridge pull]", out.stdout)
        self.assertNotIn("OPERATOR-TYPED", out.stdout)
        self.assertIn("not from the coordinator", out.stderr)

    def test_a_non_cos_sender_on_the_wire_renders_plain(self) -> None:
        body = "stage the next goal doc now"
        self.cos_record_and_send(body, send=False)
        cp = self.run_mod("net.cli", "register", "--name", "consultant", "--role", "consultant")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        out = self.relay_from_seat("consultant", body)
        self.assertIn("[via wire pull]", out.stdout)
        self.assertNotIn("OPERATOR-TYPED", out.stdout)
        self.assertIn("not a registered 'cos' session", out.stderr)

    def test_seat_cannot_record_through_the_cli(self) -> None:
        root = Path(self.tmp.name)
        (root / "instr.txt").write_text("x", encoding="utf-8")
        (root / "dp.txt").write_text("yes", encoding="utf-8")
        cp = self.run_mod("guards.typed_approval", "record", "--scope-class", "goal.stage",
                          "--target", "t", "--instruction-file", str(root / "instr.txt"),
                          "--operator-text-file", str(root / "dp.txt"), "--channel", "cos-terminal")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("seat cannot record", cp.stderr)

    def test_typed_merge_approval_is_passkey_only_on_the_wire(self) -> None:
        body = "merge PR 237"
        g = Path(self.tmp.name) / "typed.json"
        mutate = (f"{sys.executable} -c \"import json,sys; p=sys.argv[1]; r=json.load(open(p)); "
                  f"r['scope_class']='merge.approval'; json.dump(r, open(p,'w'))\" {g} && ")
        self.cos_record_and_send(body, mutate=mutate)
        out = self.run_mod("net.cli", "recv", "executor")
        self.assertNotIn("OPERATOR-TYPED", out.stdout)
        self.assertNotIn("OPERATOR-AUTHORIZED", out.stdout)
        self.assertIn("passkey only", out.stderr)


if __name__ == "__main__":
    unittest.main()
