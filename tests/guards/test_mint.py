"""guards.mint ceremony tests — everything EXCEPT the Secure-Enclave leg.

The SE Swift helper needs live Touch ID hardware and cannot run in CI, so the
ceremony takes an injectable `signer`. Here the signer is a real test-only ECDSA
sign over p256's curve math against a fixture key, so a MINTED grant round-trips
through the real verifier — proving mint and verify agree end to end without ever
touching the enclave.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_grant as hg  # noqa: E402
from guards import mint as mintmod  # noqa: E402
from guards import p256  # noqa: E402

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


def _fake_signer(digest_hex: str, reason: str = "") -> str:
    """A real ECDSA signature (test scaffolding) so minted grants actually verify."""
    digest = int(digest_hex, 16)
    k = 0x0F1E2D3C4B5A69788796A5B4C3D2E1F00F1E2D3C4B5A69788796A5B4C3D2E1F0
    R = p256._mul(k, (p256.GX, p256.GY))
    r = R[0] % p256.N
    s = (pow(k, -1, p256.N) * (digest + r * PRIV_D)) % p256.N
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MintTestBase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
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
        (self.keys / "dp-test.pub").write_text(json.dumps({
            "pubkey_id": "dp-test", "x": f"{PUB[0]:x}", "y": f"{PUB[1]:x}",
            "not_before": "2020-01-01T00:00:00Z", "not_after": "2099-01-01T00:00:00Z",
        }), encoding="utf-8")
        self.shown: list[str] = []


class Ceremony(MintTestBase):
    def test_minted_grant_round_trips_through_verify(self) -> None:
        instr = "stand down, seat widget-executor, and checkpoint"
        g = mintmod.mint(instr, "seat.standdown", "widget-executor", 600,
                         "dp-test", _now(), _fake_signer, show=self.shown.append)
        self.assertTrue(hg.verify(g, instr, _now()).ok)

    def test_ceremony_shows_full_literal_text_not_a_hash(self) -> None:
        instr = "change the Railway PORT env var to 8080 on the web service"
        mintmod.mint(instr, "unblock.instruct", "widget-executor", 300,
                     "dp-test", _now(), _fake_signer, show=self.shown.append)
        shown = "\n".join(self.shown)
        self.assertIn(instr, shown)  # the verbatim instruction, not a digest
        self.assertNotIn(hashlib.sha256(instr.encode()).hexdigest(), shown)

    def test_shows_before_it_signs(self) -> None:
        # The ceremony's whole point: the operator reads the full text, THEN Touch ID.
        order: list[str] = []

        def show(s: str) -> None:
            order.append("show")

        def signer(d: str, reason: str) -> str:
            order.append("sign")
            return _fake_signer(d)

        mintmod.mint("x", "prompt.relay", "seat", 60, "dp-test", _now(),
                     signer, show=show)
        self.assertEqual(order, ["show", "sign"])

    def test_touchid_reason_names_scope_and_target(self) -> None:
        # S2: the biometric dialog itself says what is being signed.
        reasons: list[str] = []

        def signer(d: str, reason: str) -> str:
            reasons.append(reason)
            return _fake_signer(d)

        mintmod.mint("x", "prompt.relay", "widget-executor", 60, "dp-test",
                     _now(), signer, show=self.shown.append)
        self.assertEqual(reasons, ["sign the operator grant: prompt.relay -> widget-executor"])

    def test_records_in_ledger(self) -> None:
        g = mintmod.mint("y", "goal.stage", "seat", 60, "dp-test", _now(),
                         _fake_signer, show=self.shown.append)
        self.assertTrue(hg.is_issued(g["grant_id"]))

    def test_grant_id_and_nonce_unique(self) -> None:
        a = mintmod.mint("a", "prompt.relay", "s", 60, "dp-test", _now(),
                         _fake_signer, show=self.shown.append)
        b = mintmod.mint("b", "prompt.relay", "s", 60, "dp-test", _now(),
                         _fake_signer, show=self.shown.append)
        self.assertNotEqual(a["grant_id"], b["grant_id"])
        self.assertNotEqual(a["nonce"], b["nonce"])

    def test_merge_approval_round_trips(self) -> None:
        sha = "a" * 40
        instr = "merge maw PR #127"
        g = mintmod.mint(instr, "merge.approval", "maw", 300, "dp-test", _now(),
                         _fake_signer, merge={"repo": "maw", "pr": 127, "head_sha": sha},
                         show=self.shown.append)
        self.assertTrue(hg.verify(g, instr, _now(),
                                  live_head_sha=sha, verdict_present=True).ok)
        self.assertTrue(any("PR #127" in s for s in self.shown))


class MintValidation(MintTestBase):
    def test_unknown_scope_raises(self) -> None:
        with self.assertRaises(ValueError):
            mintmod.mint("x", "merge.blanket", "s", 60, "dp-test", _now(), _fake_signer,
                         show=self.shown.append)

    def test_ttl_over_cap_raises(self) -> None:
        with self.assertRaises(ValueError):
            mintmod.mint("x", "prompt.relay", "s", hg.MAX_TTL_SECONDS + 1, "dp-test",
                         _now(), _fake_signer, show=self.shown.append)

    def test_merge_approval_without_merge_raises(self) -> None:
        with self.assertRaises(ValueError):
            mintmod.mint("x", "merge.approval", "maw", 60, "dp-test", _now(),
                         _fake_signer, show=self.shown.append)

    def test_merge_on_non_merge_scope_raises(self) -> None:
        with self.assertRaises(ValueError):
            mintmod.mint("x", "prompt.relay", "s", 60, "dp-test", _now(), _fake_signer,
                         merge={"repo": "m", "pr": 1, "head_sha": "a" * 40},
                         show=self.shown.append)

    def test_does_not_sign_or_record_on_invalid_request(self) -> None:
        # A malformed request must fail BEFORE showing, signing, or recording.
        signed = []
        with self.assertRaises(ValueError):
            mintmod.mint("x", "bad.scope", "s", 60, "dp-test", _now(),
                         lambda d, r: signed.append(d) or _fake_signer(d),
                         show=self.shown.append)
        self.assertEqual(signed, [])
        self.assertEqual(self.shown, [])


class MintCLI(MintTestBase):
    """`python3 -m guards.mint` — the stable operator-facing surface. The SE signer is
    injected (main(..., signer=...)) so CI never touches Touch ID."""

    def _instr_file(self, text: str) -> str:
        p = Path(self.tmp.name) / "instr.txt"
        p.write_text(text, encoding="utf-8")
        return str(p)

    def _run(self, args: "list[str]", signer=_fake_signer) -> "tuple[int, str, str]":
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mintmod.main(args, signer=signer)
        return rc, out.getvalue(), err.getvalue()

    def test_cli_mints_a_grant_that_verifies(self) -> None:
        # Instruction carries shell-special bytes ON PURPOSE: proves the body is
        # read from the FILE, never an argv the shell could mangle (the backtick
        # bite). If a shell had touched it, the hash — and verify — would break.
        instr = 'relay: run `git status` and echo $HOME "now"'
        f = self._instr_file(instr)
        rc, out, err = self._run(
            ["--instruction-file", f, "--scope", "prompt.relay",
             "--target", "widget-executor", "--ttl", "300", "--pubkey-id", "dp-test"]
        )
        self.assertEqual(rc, 0)
        grant = json.loads(out)  # stdout is PURE grant JSON
        self.assertTrue(hg.verify(grant, instr, _now()).ok)
        self.assertIn("SIGN THE OPERATOR GRANT", err)  # ceremony went to stderr

    def test_stdout_is_only_the_grant_stderr_has_ceremony(self) -> None:
        f = self._instr_file("do the thing")
        rc, out, err = self._run(
            ["--instruction-file", f, "--scope", "goal.stage",
             "--target", "s", "--ttl", "60", "--pubkey-id", "dp-test"]
        )
        self.assertEqual(rc, 0)
        json.loads(out)  # parses clean — no ceremony noise mixed in
        self.assertNotIn("SIGN THE OPERATOR GRANT", out)

    def test_instruction_read_from_file_hash_matches_bytes(self) -> None:
        instr = "line one\nline two with trailing space \n"
        f = self._instr_file(instr)
        rc, out, _ = self._run(
            ["--instruction-file", f, "--scope", "prompt.relay",
             "--target", "s", "--ttl", "60", "--pubkey-id", "dp-test"]
        )
        self.assertEqual(rc, 0)
        grant = json.loads(out)
        self.assertEqual(grant["instruction_sha256"],
                         hashlib.sha256(instr.encode("utf-8")).hexdigest())

    def test_default_pubkey_id(self) -> None:
        f = self._instr_file("x")
        rc, out, _ = self._run(
            ["--instruction-file", f, "--scope", "prompt.relay",
             "--target", "s", "--ttl", "60"]  # no --pubkey-id
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["pubkey_id"], "test-grant-key")

    def test_out_writes_grant_file_and_still_prints(self) -> None:
        f = self._instr_file("attach me")
        outfile = str(Path(self.tmp.name) / "grant.json")
        rc, out, _ = self._run(
            ["--instruction-file", f, "--scope", "prompt.relay", "--target", "s",
             "--ttl", "60", "--pubkey-id", "dp-test", "--out", outfile]
        )
        self.assertEqual(rc, 0)
        on_disk = json.loads(Path(outfile).read_text())
        self.assertEqual(on_disk, json.loads(out))  # file == stdout

    def test_unwritable_out_still_prints_grant(self) -> None:
        # The signature is spent before the write. An unwritable --out must warn
        # and still emit the grant on stdout — never traceback and lose it.
        bad = str(Path(self.tmp.name) / "no-such-dir" / "grant.json")
        f = self._instr_file("recover me")
        rc, out, err = self._run(
            ["--instruction-file", f, "--scope", "prompt.relay", "--target", "s",
             "--ttl", "60", "--pubkey-id", "dp-test", "--out", bad]
        )
        self.assertEqual(rc, 0)          # not a traceback
        json.loads(out)                  # grant still on stdout
        self.assertIn("WARN", err)
        self.assertFalse(Path(bad).exists())

    def test_merge_approval_via_cli_round_trips(self) -> None:
        sha = "b" * 40
        instr = "merge maw PR #140"
        f = self._instr_file(instr)
        rc, out, _ = self._run(
            ["--instruction-file", f, "--scope", "merge.approval", "--target", "maw",
             "--ttl", "300", "--pubkey-id", "dp-test", "--merge-repo", "maw",
             "--merge-pr", "140", "--merge-head-sha", sha]
        )
        self.assertEqual(rc, 0)
        grant = json.loads(out)
        self.assertTrue(hg.verify(grant, instr, _now(),
                                  live_head_sha=sha, verdict_present=True).ok)

    def test_unknown_scope_nonzero_and_clean_message(self) -> None:
        f = self._instr_file("x")
        rc, out, err = self._run(
            ["--instruction-file", f, "--scope", "merge.blanket",
             "--target", "s", "--ttl", "60"]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")            # nothing emitted to stdout
        self.assertIn("refused", err)        # a message, not a traceback
        self.assertNotIn("Traceback", err)

    def test_ttl_over_cap_nonzero(self) -> None:
        f = self._instr_file("x")
        rc, _, err = self._run(
            ["--instruction-file", f, "--scope", "prompt.relay", "--target", "s",
             "--ttl", str(hg.MAX_TTL_SECONDS + 1)]
        )
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)

    def test_merge_approval_missing_params_nonzero(self) -> None:
        f = self._instr_file("x")
        rc, _, err = self._run(
            ["--instruction-file", f, "--scope", "merge.approval",
             "--target", "maw", "--ttl", "300"]  # no merge args
        )
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)

    def test_merge_args_on_non_merge_scope_nonzero(self) -> None:
        f = self._instr_file("x")
        rc, _, err = self._run(
            ["--instruction-file", f, "--scope", "prompt.relay", "--target", "s",
             "--ttl", "60", "--merge-repo", "m", "--merge-pr", "1",
             "--merge-head-sha", "a" * 40]
        )
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)

    def test_missing_instruction_file_nonzero(self) -> None:
        rc, _, err = self._run(
            ["--instruction-file", str(Path(self.tmp.name) / "nope.txt"),
             "--scope", "prompt.relay", "--target", "s", "--ttl", "60"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("cannot read", err)

    def test_does_not_sign_on_invalid_request(self) -> None:
        signed: list[str] = []

        def spy(d: str, r: str) -> str:
            signed.append(d)
            return _fake_signer(d)

        f = self._instr_file("x")
        rc, _, _ = self._run(
            ["--instruction-file", f, "--scope", "bad.scope",
             "--target", "s", "--ttl", "60"], signer=spy
        )
        self.assertEqual(rc, 1)
        self.assertEqual(signed, [])  # refused before the enclave was ever asked


if __name__ == "__main__":
    unittest.main()
