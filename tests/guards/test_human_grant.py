"""guards.human_grant verify + ledger tests.

The product module is VERIFY-ONLY, so the accept path needs a signer to produce a
valid grant. That signer is TEST SCAFFOLDING (a minimal ECDSA sign over p256's
curve math) — it never ships. Every reject class from the design's threat model
gets a case; the accept path proves a correctly-signed grant verifies.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_grant as hg  # noqa: E402
from guards import p256  # noqa: E402

# A fixed test keypair (NOT a real the operator key — test scaffolding).
PRIV_D = 0x00C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
PUB = p256._mul(PRIV_D, (p256.GX, p256.GY))


def _sign(digest: int, k: int = 0x1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD) -> str:
    """Minimal deterministic ECDSA sign (test-only) → base64(r||s)."""
    R = p256._mul(k, (p256.GX, p256.GY))
    r = R[0] % p256.N
    s = (pow(k, -1, p256.N) * (digest + r * PRIV_D)) % p256.N
    assert r != 0 and s != 0
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class GrantTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = Path(self.tmp.name) / "keys"
        self.keys.mkdir()
        self.grants = Path(self.tmp.name) / "grants"
        env = mock.patch.dict(os.environ, {
            "MAW_GRANT_KEYS_DIR": str(self.keys),
            "MAW_GRANTS_DIR": str(self.grants),
        })
        env.start()
        self.addCleanup(env.stop)
        # commit the fixture public key with a wide validity window
        (self.keys / "dp-test.pub").write_text(json.dumps({
            "pubkey_id": "dp-test",
            "x": f"{PUB[0]:x}", "y": f"{PUB[1]:x}",
            "not_before": "2020-01-01T00:00:00Z",
            "not_after": "2099-01-01T00:00:00Z",
        }), encoding="utf-8")

    def make_grant(self, instruction: str = "stand down, seat widget-executor",
                   *, record: bool = True, **overrides) -> tuple[dict, str]:
        g = {
            "grant_id": overrides.pop("grant_id", "g-0001"),
            "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
            "scope_class": "seat.standdown",
            "target": "widget-executor",
            "issued_at": _now(),
            "ttl_seconds": 600,
            "session_token_ref": None,
            "nonce": "n-0001",
            "pubkey_id": "dp-test",
            "merge": None,
        }
        g.update(overrides)
        # (re)hash the instruction unless the caller pinned instruction_sha256
        if "instruction_sha256" not in overrides:
            g["instruction_sha256"] = hashlib.sha256(instruction.encode()).hexdigest()
        digest = int.from_bytes(hashlib.sha256(hg.canonical_bytes(g)).digest(), "big")
        g["sig"] = _sign(digest)
        if record:
            hg.record_issued(g)
        return g, instruction


class KeysetCoversTrustPolicy(GrantTestBase):
    """L0 #8: the keyset fingerprint covers every field verify() trusts, and one
    entry point runs the alarm and the verification together."""

    def _rewrite_key(self, **over) -> None:
        f = self.keys / "dp-test.pub"
        d = json.loads(f.read_text(encoding="utf-8"))
        d.update(over)
        f.write_text(json.dumps(d), encoding="utf-8")

    def test_extending_a_keys_expiry_changes_the_fingerprint(self) -> None:
        before = hg.keyset_fingerprint()
        self._rewrite_key(not_after="2199-01-01T00:00:00Z")
        self.assertNotEqual(hg.keyset_fingerprint(), before)

    def test_moving_not_before_changes_the_fingerprint(self) -> None:
        before = hg.keyset_fingerprint()
        self._rewrite_key(not_before="2000-01-01T00:00:00Z")
        self.assertNotEqual(hg.keyset_fingerprint(), before)

    def test_a_format_upgrade_is_named_as_such_and_still_refuses(self) -> None:
        g, instr = self.make_grant()
        fp = hg.grants_dir() / "keyset.fp"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(hg._legacy_keyset_fingerprint() + "\n", encoding="utf-8")
        res = hg.verify_presented(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("FORMAT upgraded", res.reason)
        self.assertIn("/bin/maw-run guards.human_grant ack-keyset", res.reason)
        self.assertNotIn("CHANGED", res.reason)
        self.assertIn(f"records the keys in {hg.keys_dir()};", res.reason)
        self.assertNotIn("installed plugin", res.reason)

    def test_a_real_change_after_an_old_baseline_still_says_changed(self) -> None:
        fp = hg.grants_dir() / "keyset.fp"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(hg._legacy_keyset_fingerprint() + "\n", encoding="utf-8")
        self._rewrite_key(x="1234")
        alarm = hg.keyset_alarm()
        self.assertIn("CHANGED", alarm)
        # The ack command names the copy that raised the alarm, so the alarm
        # says which keys directory that command would bless.
        self.assertIn(f"records the keys in {hg.keys_dir()};", alarm)
        self.assertIn("the copy whose keys were reviewed", alarm)

    def test_the_ack_says_which_keys_directory_it_recorded(self) -> None:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(hg.main(["ack-keyset"]), 0)
        self.assertIn(f"from {hg.keys_dir()} ", buf.getvalue())
        self.assertIn(hg.keyset_fingerprint()[:12], buf.getvalue())

    def test_verify_presented_runs_the_alarm(self) -> None:
        g, instr = self.make_grant()
        self.assertIsNone(hg.keyset_alarm())  # bootstrap the reviewed baseline
        self.assertTrue(hg.verify_presented(g, instr, _now()).ok)
        self._rewrite_key(not_after="2199-01-01T00:00:00Z")
        res = hg.verify_presented(g, instr, _now())
        self.assertFalse(res.ok)
        self.assertIn("CHANGED", res.reason)


class AcceptPath(GrantTestBase):
    def test_valid_grant_verifies(self) -> None:
        g, instr = self.make_grant()
        self.assertTrue(hg.verify(g, instr, _now()).ok)

    def test_target_must_name_the_receiver(self) -> None:
        # L0 #4: a valid signature for ANOTHER seat is not an order for this one.
        g, instr = self.make_grant()  # target widget-executor
        res = hg.verify(g, instr, _now(), receiver={"maw-advisor", "advisor"})
        self.assertFalse(res.ok)
        self.assertIn("target", res.reason)
        self.assertTrue(hg.verify(g, instr, _now(),
                                  receiver={"widget-executor", "executor"}).ok)

    def test_a_non_string_target_never_matches(self) -> None:
        g, instr = self.make_grant(target=["advisor"])
        self.assertFalse(hg.verify(g, instr, _now(), receiver={"advisor"}).ok)

    def test_a_list_valued_scope_class_rejects_instead_of_raising(self) -> None:
        g, instr = self.make_grant(scope_class=["seat.standdown"])
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_consume_then_replay_rejected(self) -> None:
        g, instr = self.make_grant()
        self.assertTrue(hg.verify(g, instr, _now()).ok)
        self.assertTrue(hg.consume(g["grant_id"], g["nonce"]))
        self.assertFalse(hg.verify(g, instr, _now()).ok)  # single-use

    def test_consume_is_an_atomic_claim(self) -> None:
        # N2: consume() is the real single-use gate — the FIRST caller claims it
        # (True), every later caller is refused (False). Callers act only on True,
        # so two concurrent presentations cannot both act.
        g, _ = self.make_grant()
        self.assertTrue(hg.consume(g["grant_id"], g["nonce"]))
        self.assertFalse(hg.consume(g["grant_id"], g["nonce"]))
        self.assertFalse(hg.consume(g["grant_id"], g["nonce"]))

    def test_verify_rejects_non_json_native_grant_without_raising(self) -> None:
        # N4: a grant carrying a non-JSON-native value must REJECT, never raise.
        g, instr = self.make_grant()
        g["target"] = {1, 2, 3}  # a set is not JSON-serializable
        r = hg.verify(g, instr, _now())
        self.assertFalse(r.ok)  # returned, not raised


class RejectBattery(GrantTestBase):
    def test_tampered_instruction(self) -> None:
        g, _ = self.make_grant()
        self.assertFalse(hg.verify(g, "a DIFFERENT instruction", _now()).ok)

    def test_tampered_field_breaks_signature(self) -> None:
        g, instr = self.make_grant()
        g["target"] = "prod-executor"  # change after signing
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_not_in_ledger_rejected(self) -> None:
        g, instr = self.make_grant(record=False)
        r = hg.verify(g, instr, _now())
        self.assertFalse(r.ok)
        self.assertIn("ledger", r.reason)

    def test_expired_rejected(self) -> None:
        g, instr = self.make_grant(issued_at="2020-01-01T00:00:00Z", ttl_seconds=600)
        # re-sign already happened in make_grant with that issued_at
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_future_issued_at_rejected(self) -> None:
        g, instr = self.make_grant(issued_at="2099-01-01T00:00:00Z")
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_unknown_pubkey_rejected(self) -> None:
        g, instr = self.make_grant(pubkey_id="no-such-key")
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_ttl_over_cap_rejected(self) -> None:
        g, instr = self.make_grant(ttl_seconds=hg.MAX_TTL_SECONDS + 1)
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_unknown_scope_class_rejected(self) -> None:
        g, instr = self.make_grant(scope_class="merge.blanket")
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_key_validity_window_enforced(self) -> None:
        (self.keys / "dp-test.pub").write_text(json.dumps({
            "pubkey_id": "dp-test", "x": f"{PUB[0]:x}", "y": f"{PUB[1]:x}",
            "not_before": "2099-01-01T00:00:00Z", "not_after": "2099-12-31T00:00:00Z",
        }), encoding="utf-8")
        g, instr = self.make_grant()
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_session_token_arm_rejected(self) -> None:
        g, instr = self.make_grant(arm_session=True)
        self.assertFalse(hg.verify(g, instr, _now()).ok)


class MergeApproval(GrantTestBase):
    SHA = "a" * 40

    def make_merge_grant(self, **overrides):
        return self.make_grant(
            instruction="merge maw PR #124",
            scope_class="merge.approval",
            target="maw",
            merge={"repo": "maw", "pr": 124, "head_sha": self.SHA},
            **overrides,
        )

    def test_valid_merge_approval(self) -> None:
        g, instr = self.make_merge_grant()
        r = hg.verify(g, instr, _now(), live_head_sha=self.SHA, verdict_present=True)
        self.assertTrue(r.ok, r.reason)

    def test_sha_drift_rejected(self) -> None:
        g, instr = self.make_merge_grant()
        r = hg.verify(g, instr, _now(), live_head_sha="b" * 40, verdict_present=True)
        self.assertFalse(r.ok)
        self.assertIn("SHA-drift", r.reason)

    def test_missing_verdict_rejected(self) -> None:
        g, instr = self.make_merge_grant()
        r = hg.verify(g, instr, _now(), live_head_sha=self.SHA, verdict_present=False)
        self.assertFalse(r.ok)
        self.assertIn("verdict", r.reason)

    def test_missing_live_data_rejected(self) -> None:
        # A merge.approval grant presented WITHOUT the live checks fails closed.
        g, instr = self.make_merge_grant()
        self.assertFalse(hg.verify(g, instr, _now()).ok)

    def test_merge_object_required(self) -> None:
        g, instr = self.make_grant(scope_class="merge.approval", merge=None)
        self.assertFalse(hg.verify(g, instr, _now(),
                                   live_head_sha=self.SHA, verdict_present=True).ok)

    def test_merge_on_non_merge_grant_rejected(self) -> None:
        g, instr = self.make_grant(merge={"repo": "maw", "pr": 1, "head_sha": self.SHA})
        self.assertFalse(hg.verify(g, instr, _now()).ok)


class Keyset(GrantTestBase):
    def test_fingerprint_changes_when_a_key_is_added(self) -> None:
        fp1 = hg.keyset_fingerprint()
        (self.keys / "dp-2.pub").write_text(json.dumps({
            "pubkey_id": "dp-2", "x": f"{p256.GX:x}", "y": f"{p256.GY:x}",
            "not_before": "2020-01-01T00:00:00Z", "not_after": "2099-01-01T00:00:00Z",
        }), encoding="utf-8")
        self.assertNotEqual(fp1, hg.keyset_fingerprint())

    def test_fingerprint_stable_across_reads(self) -> None:
        self.assertEqual(hg.keyset_fingerprint(), hg.keyset_fingerprint())


class EmptyTrustStoreTest(GrantTestBase):
    """L2 rule 6: the public export ships NO operator keys. An empty or missing
    store must refuse every grant (reason: no grant public key is installed) and never fall back to the keys
    committed beside the module."""

    def _empty_store(self, missing: bool = False) -> None:
        for f in self.keys.glob("*.pub"):
            f.unlink()
        if missing:
            self.keys.rmdir()

    def test_an_empty_store_refuses_a_well_signed_grant(self) -> None:
        g, instr = self.make_grant()
        self.assertTrue(hg.verify(g, instr, _now()).ok)  # control: same grant verifies
        for missing in (False, True):
            with self.subTest(missing=missing):
                if self.keys.exists():
                    self._empty_store(missing=missing)
                g, instr = self.make_grant(grant_id=f"g-empty-{missing}", nonce=f"n-{missing}")
                r = hg.verify(g, instr, _now())
                self.assertFalse(r.ok)
                self.assertIn("no grant public key is installed", r.reason)

    def test_an_empty_store_never_falls_back_to_the_committed_keys(self) -> None:
        self._empty_store()
        self.assertEqual(hg._keyset_rows(), [])
        committed = hg._KEYS_DIR
        for f in committed.glob("*.pub"):
            with self.subTest(committed_key=f.stem):
                self.assertIsNone(hg.load_key(f.stem))


if __name__ == "__main__":
    unittest.main()
