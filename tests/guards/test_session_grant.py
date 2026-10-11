"""Phone arm + amend: `session.arm` / `scope.amend` passkey grants as the
session_auth ceremony (refit R3a; DECISIONS.md 2026-09-23, ruling 3).

Hermetic: keys, the grant ledger and repo_root are tmpdirs; time, identity and
the git runner are injected. The signer is TEST SCAFFOLDING (a fixed test key),
never an operator key. Every refusal case also asserts its audit record (gate 8).
"""
from __future__ import annotations

import base64
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_grant as hg  # noqa: E402
from guards import p256  # noqa: E402
from guards import session_auth as sa  # noqa: E402

# Neutral key ids: the suite never reads the operator's ~/.maw/operator.toml
# (tests/run_all.py isolates it), so the minting tools get theirs from here.
TEST_KEY_IDS = {"MAW_GRANT_PUBKEY_ID": "test-grant-key",
                "MAW_PHONE_PUBKEY_ID": "test-phone-key",
                "MAW_RELAY_PUBKEY_ID": "test-relay-key"}
_KEY_ID_ENV = mock.patch.dict(os.environ, TEST_KEY_IDS)


def setUpModule() -> None:
    _KEY_ID_ENV.start()


def tearDownModule() -> None:
    _KEY_ID_ENV.stop()

PRIV_D = 0x00C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
PUB = p256._mul(PRIV_D, (p256.GX, p256.GY))
ROGUE_D = 0x0A1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8F9
SCOPE = "maw refit run: R1, R2 and R3 lanes, then the advisor-goal tail"
NEW_SCOPE = "maw refit run: R1-R3 lanes plus T1 duplicate-pair guard only"


def _sign(digest: int, d: int = PRIV_D) -> str:
    k = 0x1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD
    R = p256._mul(k, (p256.GX, p256.GY))
    r = R[0] % p256.N
    s = (pow(k, -1, p256.N) * (digest + r * d)) % p256.N
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _git(slug: str = "maw", owner: str = "example-org", url: "str | None" = None):
    def run(cmd, *a, **k):
        if cmd[:1] == ["git"] and "get-url" in cmd:
            if url == "":
                return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="no origin")
            out = url if url is not None else f"git@github.com:{owner}/{slug}.git"
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")
    return run


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root / "repo"
        self.root.mkdir()
        self.keys = root / "keys"
        self.keys.mkdir()
        env = mock.patch.dict(os.environ, {"MAW_GRANT_KEYS_DIR": str(self.keys),
                                           "MAW_GRANTS_DIR": str(root / "grants")})
        env.start()
        self.addCleanup(env.stop)
        (self.keys / "dp-test.pub").write_text(json.dumps({
            "pubkey_id": "dp-test", "x": f"{PUB[0]:x}", "y": f"{PUB[1]:x}",
            "not_before": "2020-01-01T00:00:00Z", "not_after": "2099-01-01T00:00:00Z",
        }), encoding="utf-8")
        hg.ack_keyset()
        self.now = time.time()
        self.sid, self.pid = "advisor-s1", os.getpid()
        self.n = 0

    def grant(self, scope_class: str, session: dict, *, d: int = PRIV_D,
              issued: float | None = None, ttl: int = 600, record: bool = True,
              tamper: "dict | None" = None) -> dict:
        self.n += 1
        g = {
            "grant_id": f"g-{self.n}", "scope_class": scope_class, "target": "advisor",
            "instruction_sha256": hashlib.sha256(
                hg.session_instruction(scope_class, session).encode()).hexdigest(),
            "issued_at": _iso(issued if issued is not None else self.now),
            "ttl_seconds": ttl, "session_token_ref": None, "nonce": f"n-{self.n}",
            "pubkey_id": "dp-test", "merge": None, "session": session,
        }
        g["sig"] = _sign(int.from_bytes(hashlib.sha256(hg.canonical_bytes(g)).digest(), "big"), d)
        if record:
            hg.record_issued(g)
        if tamper:  # bytes changed AFTER the signature (a relay swap)
            g = {**g, **tamper}
        return g

    def arm_session(self, scope: str = SCOPE) -> dict:
        return {"repo": "example-org/maw", "seat": "advisor", "session_id": self.sid, "scope": scope}

    def arm(self, g: object, scope: str = SCOPE, slug: str = "maw", now: float | None = None):
        return sa.arm(scope, self.root, session_id=self.sid, pid=self.pid,
                      now_s=self.now if now is None else now,
                      gate_fn=lambda _p: self.fail("Touch ID must not run on the grant path"),
                      seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                      runner=_git(slug), grant=g)

    def amend(self, g: object, scope: str = NEW_SCOPE, slug: str = "maw", now: float | None = None):
        return sa.amend(scope, self.root, g, session_id=self.sid, pid=self.pid,
                        now_s=self.now + 60 if now is None else now, runner=_git(slug))

    def token(self) -> dict:
        return json.loads(sa._runtime_path(self.root, sa.TOKEN_REL).read_text())

    def audit(self) -> list[dict]:
        p = sa._runtime_path(self.root, sa.AUDIT_REL)
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def assert_refused_and_audited(self, result, event: str, needle: str) -> None:
        ok, msg = result
        self.assertFalse(ok, msg)
        self.assertIn(needle, msg)
        last = self.audit()[-1]
        self.assertEqual(last["event"], event)
        self.assertIn(needle, last["reason"])


class AmendIsBoundToTheArmedRepo(Base):
    def test_an_amend_from_a_different_origin_refuses(self) -> None:
        ok, msg = self.arm(self.grant("session.arm", self.arm_session()))
        self.assertTrue(ok, msg)
        s = {"repo": "example-org/other", "seat": "advisor", "session_id": self.sid,
             "from_scope": SCOPE, "scope": NEW_SCOPE}
        self.assert_refused_and_audited(
            self.amend(self.grant("scope.amend", s), slug="other"),
            "amend-refused", "armed")


class ArmByGrant(Base):
    def test_a_grant_carrying_a_pr_set_arms_with_it_enforced(self) -> None:
        g = self.grant("session.arm", dict(self.arm_session(), prs="#3,#8"))
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=self.pid,
                         now_s=self.now, gate_fn=lambda _p: self.fail("Touch ID ran"),
                         seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                         runner=_git("maw"), grant=g, prs=[8, 3])
        self.assertTrue(ok, msg)
        self.assertEqual(self.token()["prs"], [3, 8])

    def test_a_pr_set_the_grant_did_not_sign_refuses(self) -> None:
        g = self.grant("session.arm", self.arm_session())  # signed with no set
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=self.pid,
                         now_s=self.now, gate_fn=lambda _p: self.fail("Touch ID ran"),
                         seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                         runner=_git("maw"), grant=g, prs=[3])
        self.assertFalse(ok, msg)
        self.assertIn("prs", msg)

    def test_a_fresh_install_with_no_keys_refuses_grant_arms_only(self) -> None:
        # L2 rule 6: the export ships no operator keys. On a fresh install (no
        # key files, no recorded baseline) a phone-grant arm refuses (no key
        # installed); the Touch ID arm is unaffected.
        g = self.grant("session.arm", self.arm_session())
        for f in self.keys.glob("*.pub"):
            f.unlink()
        (Path(os.environ["MAW_GRANTS_DIR"]) / "keyset.fp").unlink()
        ok, msg = self.arm(g)
        self.assertFalse(ok, msg)
        self.assertIn("no grant public key is installed", msg)
        self.assertFalse(sa._runtime_path(self.root, sa.TOKEN_REL).exists())
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=self.pid,
                         now_s=self.now, gate_fn=lambda _p: True, seat="advisor",
                         env={"MAW_BRIDGE_ROLE": "advisor"}, runner=_git("maw"))
        self.assertTrue(ok, msg)

    def test_valid_grant_arms_with_the_same_6h_cap(self) -> None:
        ok, msg = self.arm(self.grant("session.arm", self.arm_session()))
        self.assertTrue(ok, msg)
        tok = self.token()
        self.assertEqual(tok["scope"], SCOPE)
        self.assertEqual(tok["seat"], "advisor")
        self.assertEqual(tok["armed_via"], "grant:g-1")
        self.assertEqual(sa._parse_iso(tok["expires_at"]) - sa._parse_iso(tok["armed_at"]), 6 * 3600)
        self.assertEqual(self.audit()[-1]["via"], "grant:g-1")

    def test_relay_swaps_bytes_after_display(self) -> None:
        swapped = dict(self.arm_session(), scope="merge anything on any repo forever and always")
        g = self.grant("session.arm", self.arm_session(), tamper={"session": swapped})
        self.assert_refused_and_audited(self.arm(g, scope=swapped["scope"]), "arm-refused",
                                        "instruction hash mismatch")
        # A smarter relay also rewrites the hash: now the signature fails.
        rehash = hashlib.sha256(hg.session_instruction("session.arm", swapped).encode()).hexdigest()
        g = self.grant("session.arm", self.arm_session(),
                       tamper={"session": swapped, "instruction_sha256": rehash})
        self.assert_refused_and_audited(self.arm(g, scope=swapped["scope"]), "arm-refused",
                                        "signature does not verify")
        self.assertFalse(sa._runtime_path(self.root, sa.TOKEN_REL).exists())

    def test_replayed_assertion_is_refused(self) -> None:
        g = self.grant("session.arm", self.arm_session())
        self.assertTrue(self.arm(g)[0])
        self.assert_refused_and_audited(self.arm(g), "arm-refused", "consumed")

    def test_expired_grant_is_refused(self) -> None:
        g = self.grant("session.arm", self.arm_session(), issued=self.now - 700, ttl=600)
        self.assert_refused_and_audited(self.arm(g), "arm-refused", "expired")

    def test_grant_for_another_repo_is_refused(self) -> None:
        g = self.grant("session.arm", dict(self.arm_session(), repo="example-org/shop-app"))
        self.assert_refused_and_audited(self.arm(g), "arm-refused",
                                        "repo grant='example-org/shop-app'")

    def test_same_name_other_owner_is_refused(self) -> None:
        g = self.grant("session.arm", self.arm_session())
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=self.pid, now_s=self.now,
                         seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                         runner=_git(owner="someone-else"), grant=g)
        self.assertFalse(ok)
        self.assertIn("derived='someone-else/maw'", msg)

    def test_no_origin_remote_refuses_a_grant_arm(self) -> None:
        g = self.grant("session.arm", self.arm_session())
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=self.pid, now_s=self.now,
                         seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                         runner=_git(url=""), grant=g)
        self.assertFalse(ok)
        self.assertIn("no readable origin remote", msg)
        self.assertEqual(self.audit()[-1]["event"], "arm-refused")

    def test_grant_for_another_session_or_scope_is_refused(self) -> None:
        g = self.grant("session.arm", dict(self.arm_session(), session_id="advisor-other"))
        self.assert_refused_and_audited(self.arm(g), "arm-refused", "session_id")
        g = self.grant("session.arm", self.arm_session())
        self.assert_refused_and_audited(self.arm(g, scope=NEW_SCOPE), "arm-refused", "scope grant=")

    def test_non_committed_key_is_refused(self) -> None:
        g = self.grant("session.arm", self.arm_session(), d=ROGUE_D)
        self.assert_refused_and_audited(self.arm(g), "arm-refused", "signature does not verify")

    def test_unissued_grant_is_refused(self) -> None:
        g = self.grant("session.arm", self.arm_session(), record=False)
        self.assert_refused_and_audited(self.arm(g), "arm-refused", "issued ledger")

    def test_other_classes_cannot_arm(self) -> None:
        for klass in ("goal.stage", "prompt.relay", "unblock.instruct", "scope.amend"):
            with self.subTest(klass):
                g = {"scope_class": klass, "grant_id": "x", "session": self.arm_session()}
                self.assert_refused_and_audited(self.arm(g), "arm-refused", "needs 'session.arm'")

    def test_executor_seat_cannot_arm_by_grant(self) -> None:
        g = self.grant("session.arm", dict(self.arm_session(), seat="executor"))
        ok, msg = sa.arm(SCOPE, self.root, session_id=self.sid, pid=self.pid, now_s=self.now,
                         seat="executor", env={"MAW_BRIDGE_ROLE": "executor"},
                         runner=_git(), grant=g)
        self.assertFalse(ok)
        self.assertIn("not authorized to merge", msg)
        self.assertEqual(self.audit()[-1]["event"], "arm-refused")

    def test_empty_or_short_scope_refused(self) -> None:
        g = self.grant("session.arm", dict(self.arm_session(), scope="all"))
        self.assert_refused_and_audited(self.arm(g, scope="all"), "arm-refused", "too short")

    def test_changed_keyset_is_refused(self) -> None:
        g = self.grant("session.arm", self.arm_session())
        (self.keys / "extra.pub").write_text("{}", encoding="utf-8")
        ok, msg = self.arm(g)
        self.assertFalse(ok)
        self.assertEqual(self.audit()[-1]["event"], "arm-refused")


class AmendByGrant(Base):
    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self.arm(self.grant("session.arm", self.arm_session()))[0])
        self.before = self.token()

    def amend_session(self, **over) -> dict:
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": self.sid,
             "from_scope": SCOPE, "scope": NEW_SCOPE}
        s.update(over)
        return s

    def test_valid_amend_changes_scope_only(self) -> None:
        ok, msg = self.amend(self.grant("scope.amend", self.amend_session()))
        self.assertTrue(ok, msg)
        tok = self.token()
        self.assertEqual(tok["scope"], NEW_SCOPE)
        for k in ("armed_at", "expires_at", "pid", "session_id", "seat", "nonce"):
            self.assertEqual(tok[k], self.before[k], k)
        self.assertEqual(self.audit()[-1]["event"], "amend")

    def test_amend_to_another_repo_is_refused(self) -> None:
        g = self.grant("scope.amend", self.amend_session(repo="example-org/widget"))
        self.assert_refused_and_audited(self.amend(g), "amend-refused",
                                        "repo grant='example-org/widget'")
        self.assertEqual(self.token(), self.before)

    def test_amend_cannot_change_seat(self) -> None:
        g = self.grant("scope.amend", self.amend_session(seat="executor"))
        self.assert_refused_and_audited(self.amend(g), "amend-refused", "seat grant='executor'")

    def test_amend_from_a_stale_scope_is_refused(self) -> None:
        g = self.grant("scope.amend", self.amend_session(from_scope="an older scope text here"))
        self.assert_refused_and_audited(self.amend(g), "amend-refused", "from_scope")

    def test_amend_never_extends_past_the_cap(self) -> None:
        late = sa._parse_iso(self.before["expires_at"]) + 1
        g = self.grant("scope.amend", self.amend_session(), issued=late - 10)
        self.assert_refused_and_audited(self.amend(g, now=late), "amend-refused", "cap-reached")

    def test_amend_replay_and_arm_grant_as_amend(self) -> None:
        g = self.grant("scope.amend", self.amend_session())
        self.assertTrue(self.amend(g)[0])
        again = self.grant("scope.amend", self.amend_session(from_scope=NEW_SCOPE, scope=SCOPE))
        self.assertTrue(self.amend(again, scope=SCOPE)[0])
        # The token is back at SCOPE, so g's from_scope matches again: only the
        # single-use ledger stops the replay.
        self.assert_refused_and_audited(self.amend(g), "amend-refused", "consumed")
        armg = self.grant("session.arm", self.arm_session(NEW_SCOPE))
        self.assert_refused_and_audited(self.amend(armg), "amend-refused", "needs 'scope.amend'")

    def test_amend_without_a_live_token_is_refused(self) -> None:
        sa._runtime_path(self.root, sa.TOKEN_REL).unlink()
        g = self.grant("scope.amend", self.amend_session())
        self.assert_refused_and_audited(self.amend(g), "amend-refused", "no live armed session")



class TouchIdAmendViaMintCli(Base):
    """`guards.mint --session-file`: the Touch ID (Secure Enclave) path to a
    scope.amend, so adding to a scope is not a full re-arm. The SE signer is
    injected as an ARGUMENT (main(..., signer=...)); nothing here can reach the
    real helper or a Touch ID prompt."""

    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self.arm(self.grant("session.arm", self.arm_session()))[0])
        self.before = self.token()
        self.signed: list[str] = []

    def signer(self, digest_hex: str, reason: str) -> str:
        self.signed.append(reason)
        return _sign(int(digest_hex, 16))

    def amend_session(self, **over) -> dict:
        s = {"repo": "example-org/maw", "seat": "advisor", "session_id": self.sid,
             "from_scope": SCOPE, "scope": NEW_SCOPE}
        s.update(over)
        return s

    def write(self, name: str, obj: object) -> str:
        p = Path(self.tmp.name) / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        return str(p)

    def request_file(self, session: dict) -> str:
        """What `session_auth grant-request scope.amend` prints."""
        return self.write("req.json", {"session": session, "instruction":
                                       hg.session_instruction("scope.amend", session)})

    def mint(self, *args: str) -> "tuple[int, dict | None, str]":
        import contextlib
        import io
        from guards import mint as gmint
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gmint.main(["--target", "advisor", "--ttl", "600",
                             "--pubkey-id", "dp-test", *args], signer=self.signer)
        return rc, (json.loads(out.getvalue()) if rc == 0 else None), err.getvalue()

    def test_grant_request_output_mints_an_amend_that_applies(self) -> None:
        rc, g, err = self.mint("--scope", "scope.amend",
                               "--session-file", self.request_file(self.amend_session()))
        self.assertEqual(rc, 0, err)
        self.assertIn("session.from_scope", err)   # the operator sees what he signs
        self.assertIn(NEW_SCOPE, err)
        self.assertIn("sig", g)
        self.assertNotIn("webauthn", g)
        ok, msg = self.amend(g)
        self.assertTrue(ok, msg)
        tok = self.token()
        self.assertEqual(tok["scope"], NEW_SCOPE)
        for k in ("armed_at", "expires_at", "pid", "session_id", "seat", "nonce"):
            self.assertEqual(tok[k], self.before[k], k)
        self.assertEqual(tok["amended"][-1]["carrier"], "touch-id")
        self.assertEqual(self.audit()[-1]["carrier"], "touch-id")

    def test_bare_session_object_is_accepted(self) -> None:
        rc, g, err = self.mint("--scope", "scope.amend",
                               "--session-file", self.write("s.json", self.amend_session()))
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.amend(g)[0])

    def test_minted_for_another_session_repo_or_seat_refuses(self) -> None:
        for over, needle in (({"session_id": "advisor-other"}, "session_id grant="),
                             ({"repo": "example-org/widget"}, "repo grant="),
                             ({"seat": "executor"}, "seat grant=")):
            with self.subTest(over=over):
                rc, g, err = self.mint("--scope", "scope.amend", "--session-file",
                                       self.request_file(self.amend_session(**over)))
                self.assertEqual(rc, 0, err)
                self.assert_refused_and_audited(self.amend(g), "amend-refused", needle)
                self.assertEqual(self.token(), self.before)

    def test_widening_the_pr_set_needs_the_grant_to_carry_it(self) -> None:
        # Armed with no set; the grant names none, so `--prs` on amend is a
        # widening the operator never signed.
        rc, g, err = self.mint("--scope", "scope.amend",
                               "--session-file", self.request_file(self.amend_session()))
        self.assertEqual(rc, 0, err)
        ok, msg = sa.amend(NEW_SCOPE, self.root, g, session_id=self.sid, pid=self.pid,
                           now_s=self.now + 60, runner=_git(), prs=[7, 9])
        self.assertFalse(ok)
        self.assertIn("prs grant=None", msg)
        self.assertEqual(self.token(), self.before)

    def test_amend_never_extends_the_cap(self) -> None:
        late = sa._parse_iso(self.before["expires_at"]) + 1
        rc, g, err = self.mint("--scope", "scope.amend",
                               "--session-file", self.request_file(self.amend_session()))
        self.assertEqual(rc, 0, err)
        self.assert_refused_and_audited(self.amend(g, now=late), "amend-refused", "cap-reached")

    def test_refusals_happen_before_the_signer_is_asked(self) -> None:
        req = self.request_file(self.amend_session())
        edited = self.write("edited.json", {
            "session": self.amend_session(scope="maw refit run: everything, all repos"),
            "instruction": hg.session_instruction("scope.amend", self.amend_session())})
        instr = self.write("i.txt", "x")
        arm_req = self.write("arm.json", {"session": self.arm_session(), "instruction":
                                          hg.session_instruction("session.arm", self.arm_session())})
        cases = (
            (["--scope", "scope.amend"], "needs --session-file"),
            (["--scope", "scope.amend", "--session-file", req, "--instruction-file", instr],
             "drop --instruction-file"),
            (["--scope", "prompt.relay", "--session-file", req], "only for"),
            (["--scope", "prompt.relay"], "--instruction-file is required"),
            (["--scope", "scope.amend", "--session-file", edited], "does not match"),
            (["--scope", "scope.amend", "--session-file", arm_req], "session object must have"),
            (["--scope", "scope.amend", "--session-file", self.write("bad.json", "[")],
             "session object"),
            (["--scope", "scope.amend", "--session-file", str(Path(self.tmp.name) / "nope")],
             "cannot read --session-file"),
        )
        for args, needle in cases:
            with self.subTest(args=args):
                rc, _, err = self.mint(*args)
                self.assertEqual(rc, 1)
                self.assertIn(needle, err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.signed, [])


class VerifierShape(Base):
    def test_instruction_is_unambiguous_for_separator_laden_scopes(self) -> None:
        a = {"repo": "o/r", "seat": "advisor", "session_id": "s",
             "from_scope": "alpha lane — to: beta lane", "scope": "gamma lane only"}
        b = dict(a, from_scope="alpha lane", scope="beta lane — to: gamma lane only")
        self.assertNotEqual(hg.session_instruction("scope.amend", a),
                            hg.session_instruction("scope.amend", b))

    def test_session_object_rules(self) -> None:
        good = self.arm_session()
        self.assertIsNone(hg.session_shape_error("session.arm", good))
        self.assertIsNotNone(hg.session_shape_error("session.arm", None))
        self.assertIsNotNone(hg.session_shape_error("session.arm", dict(good, extra="x")))
        self.assertIsNotNone(hg.session_shape_error("session.arm", dict(good, scope="a  b")))
        self.assertIsNotNone(hg.session_shape_error("goal.stage", good))
        self.assertIsNone(hg.session_shape_error("goal.stage", None))

    def test_verify_rejects_session_on_a_non_session_class(self) -> None:
        instr = "stage the goal"
        g = {"grant_id": "g-x", "scope_class": "goal.stage", "target": "t",
             "instruction_sha256": hashlib.sha256(instr.encode()).hexdigest(),
             "issued_at": _iso(self.now), "ttl_seconds": 600, "session_token_ref": None,
             "nonce": "n", "pubkey_id": "dp-test", "merge": None, "session": self.arm_session()}
        g["sig"] = _sign(int.from_bytes(hashlib.sha256(hg.canonical_bytes(g)).digest(), "big"))
        hg.record_issued(g)
        res = hg.verify(g, instr, _iso(self.now))
        self.assertFalse(res.ok)
        self.assertIn("session present on a non-session grant", res.reason)

    def test_mint_surfaces_refuse_a_bad_session_before_signing(self) -> None:
        from guards import mint as gmint
        from phone_mint import mint_session as ms
        with self.assertRaises(ValueError):
            ms.assemble_unsigned_grant(instruction="x", scope_class="session.arm", target="t",
                                       ttl_seconds=600, now_iso=_iso(self.now), session=None)
        with self.assertRaises(ValueError):
            gmint.mint("x", "goal.stage", "t", 600, "dp-test", _iso(self.now),
                       signer=lambda *_: self.fail("signed"), show=lambda *_: None,
                       record=False, session=self.arm_session())
        g = ms.assemble_unsigned_grant(instruction="x", scope_class="session.arm", target="t",
                                       ttl_seconds=600, now_iso=_iso(self.now),
                                       session=self.arm_session())
        self.assertEqual(g["session"], self.arm_session())


class AsciiPremise(Base):
    """Advisor F1 on #237: canonical_bytes (Python, ensure_ascii) and the phone
    page's canon() (JS JSON.stringify) agree only on ASCII. A non-ASCII scope must
    be refused BEFORE the operator is asked (gate 12), never after his Face ID."""

    DASH = "maw refit run \u2014 R1 to R3 lanes only"

    def test_verify_refuses_non_ascii_session(self) -> None:
        g = self.grant("session.arm", self.arm_session(self.DASH))
        res = hg.verify(g, hg.session_instruction("session.arm", g["session"]), _iso(self.now),
                        ceremony=True)
        self.assertFalse(res.ok)
        self.assertIn("U+2014", res.reason)

    def test_a_session_grant_verifies_only_inside_the_ceremony(self) -> None:
        # R16 (run 1002b): a delivery point that verified a session grant went on
        # to consume it. verify() refuses unless the caller is the ceremony.
        for klass, session in (("session.arm", self.arm_session()),
                               ("scope.amend", dict(self.arm_session(), from_scope=SCOPE))):
            with self.subTest(klass):
                g = self.grant(klass, session)
                instr = hg.session_instruction(klass, session)
                res = hg.verify(g, instr, _iso(self.now))
                self.assertFalse(res.ok)
                self.assertIn("only inside the session_auth ceremony", res.reason)
                self.assertTrue(hg.verify(g, instr, _iso(self.now), ceremony=True).ok)

    def test_both_mint_surfaces_refuse_before_showing(self) -> None:
        from guards import mint as gmint
        from phone_mint import mint_session as ms
        bad = self.arm_session(self.DASH)
        with self.assertRaisesRegex(ValueError, "U\\+2014"):
            ms.assemble_unsigned_grant(instruction="x", scope_class="session.arm", target="t",
                                       ttl_seconds=600, now_iso=_iso(self.now), session=bad)
        with self.assertRaisesRegex(ValueError, "U\\+2014"):
            gmint.mint("x", "session.arm", "t", 600, "dp-test", _iso(self.now),
                       signer=lambda *_: self.fail("signed"),
                       show=lambda *_: self.fail("shown"), record=False, session=bad)

    def test_grant_request_refuses_before_minting(self) -> None:
        with mock.patch.object(sa, "_registry_identity", return_value=((self.sid, self.pid), "")), \
             mock.patch.dict(os.environ, {"MAW_BRIDGE_ROLE": "advisor"}):
            req, why = sa.session_request("session.arm", self.DASH, self.root, runner=_git())
            self.assertIsNone(req)
            self.assertIn("U+2014", why)
            req, why = sa.session_request("session.arm", SCOPE, self.root, runner=_git())
        self.assertEqual(req["session"], self.arm_session())
        self.assertEqual(req["instruction"], hg.session_instruction("session.arm", self.arm_session()))

    def test_js_canon_matches_python_on_an_ascii_session_grant(self) -> None:
        import re
        import shutil
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        from phone_mint import serve
        m = re.search(r"function canon\(v\)\{.*?\n", serve._PAGE_TMPL)
        self.assertIsNotNone(m, "serve.py no longer defines canon() on the page")
        g = self.grant("session.arm", self.arm_session(), record=False)
        g.pop("sig")
        script = m.group(0) + "process.stdout.write(canon(JSON.parse(require('fs').readFileSync(0,'utf8'))));"
        out = subprocess.run([node, "-e", script], input=json.dumps(g), capture_output=True,
                             text=True, timeout=30, check=True).stdout
        self.assertEqual(out.encode("utf-8"), hg.canonical_bytes(g))


if __name__ == "__main__":
    unittest.main()
