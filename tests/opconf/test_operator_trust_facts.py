"""The operator's trust-path facts live in ~/.maw/operator.toml, not in maw.

Key ids and the no-echo classification record used to be hardcoded with one
operator's values. These tests pin the move:

- ABSENT config fails closed: every minting surface refuses BEFORE it shows,
  signs, opens a window or posts, and an empty id never selects a key.
- PRESENT config gives the same behaviour the hardcoded values gave: the same
  id lands in the grant, env beats the file, and the echo decision is unchanged.
- Verification never reads these settings: it trusts only the committed keys.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MAW = Path(__file__).resolve().parents[2] / "maw"
sys.path.insert(0, str(MAW))

import opconf  # noqa: E402
from guards import human_grant as hg  # noqa: E402
from guards import mint as gmint  # noqa: E402
from guards import session_auth as sa  # noqa: E402
from phone_mint import mint_session as ms  # noqa: E402
from phone_mint import relay_client as rc  # noqa: E402
from phone_mint import serve  # noqa: E402

TRUST_ENV = ("MAW_GRANT_PUBKEY_ID", "MAW_PHONE_PUBKEY_ID", "MAW_RELAY_PUBKEY_ID",
             "MAW_NO_ECHO_REPOS", "MAW_OPERATOR_CONFIG")
OPERATOR_FILE = (
    '# an operator file in the shape maw writes\n'
    'grant_pubkey_id = "op-grant-1"        # env: MAW_GRANT_PUBKEY_ID\n'
    'phone_pubkey_id = "op-phone-1"\n'
    'relay_pubkey_id = "op-relay-1"\n'
    'no_echo_repos = "maw, widget ,,gadget"\n'
)


def _assemble(**kw) -> dict:
    return ms.assemble_unsigned_grant(instruction="x", scope_class="seat.standdown",
                                      target="t", ttl_seconds=60,
                                      now_iso="2026-09-28T00:00:00Z", **kw)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "operator.toml"
        env = {k: v for k, v in os.environ.items() if k not in TRUST_ENV}
        env["MAW_OPERATOR_CONFIG"] = str(self.cfg)
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def present(self) -> None:
        self.cfg.write_text(OPERATOR_FILE, encoding="utf-8")


class AbsentConfigFailsClosed(Base):
    def test_shipped_defaults_carry_no_trust_facts(self) -> None:
        for key in ("grant_pubkey_id", "phone_pubkey_id", "relay_pubkey_id", "no_echo_repos"):
            self.assertEqual(opconf.KEYS[key][1], "", key)

    def test_require_names_the_env_var_and_the_file(self) -> None:
        with self.assertRaises(opconf.NotConfigured) as cm:
            opconf.require("phone_pubkey_id")
        self.assertIn("MAW_PHONE_PUBKEY_ID", str(cm.exception))
        self.assertIn(str(self.cfg), str(cm.exception))

    def test_a_blank_value_is_not_configured(self) -> None:
        self.cfg.write_text('phone_pubkey_id = ""\n', encoding="utf-8")
        with self.assertRaises(opconf.NotConfigured):
            opconf.require("phone_pubkey_id")

    def test_phone_assembly_refuses(self) -> None:
        with self.assertRaises(ValueError):  # NotConfigured is a ValueError
            _assemble()

    def test_mint_cli_refuses_before_the_signer_is_touched(self) -> None:
        instr = Path(self.tmp.name) / "i.txt"
        instr.write_text("x", encoding="utf-8")
        signed: list = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc_ = gmint.main(["--instruction-file", str(instr), "--scope", "prompt.relay",
                              "--target", "t", "--ttl", "60"],
                             signer=lambda d, r="": signed.append(d))
        self.assertEqual(rc_, 1)
        self.assertEqual(signed, [])
        self.assertIn("grant_pubkey_id is not configured", err.getvalue())

    def test_register_window_refuses_before_it_opens(self) -> None:
        with self.assertRaises(ValueError):
            serve.MintServer(kind="register", window_seconds=1, port=0)

    def test_relay_register_refuses_before_it_posts(self) -> None:
        posted: list = []
        err = io.StringIO()
        with mock.patch.object(rc, "read_secret", return_value="s" * 32), \
                mock.patch.object(rc, "post", side_effect=lambda *a, **k: posted.append(a)), \
                contextlib.redirect_stderr(err):
            rc_ = rc.main(["--relay", "https://relay.example", "register"])
        self.assertEqual(rc_, 1)
        self.assertEqual(posted, [])
        self.assertIn("relay_pubkey_id is not configured", err.getvalue())

    def test_an_empty_key_id_never_selects_a_key(self) -> None:
        keys = Path(self.tmp.name) / "keys"
        keys.mkdir()
        # A file literally named ".pub" is what an empty id would open.
        (keys / ".pub").write_text('{"pubkey_id": "", "x": "1", "y": "2", '
                                   '"not_before": "a", "not_after": "b"}', encoding="utf-8")
        with mock.patch.dict(os.environ, {"MAW_GRANT_KEYS_DIR": str(keys)}):
            self.assertIsNone(hg.load_key(""))

    def test_a_key_file_must_declare_its_own_id(self) -> None:
        # The verifier trusts a file only under the id it declares, so a renamed
        # or copied .pub cannot stand in for a configured id.
        keys = Path(self.tmp.name) / "keys"
        keys.mkdir()
        body = '"x": "1", "y": "2", "not_before": "a", "not_after": "b"}'
        (keys / "op-phone-1.pub").write_text('{"pubkey_id": "op-phone-1", ' + body, encoding="utf-8")
        (keys / "copied.pub").write_text('{"pubkey_id": "op-phone-1", ' + body, encoding="utf-8")
        with mock.patch.dict(os.environ, {"MAW_GRANT_KEYS_DIR": str(keys)}):
            self.assertIsNotNone(hg.load_key("op-phone-1"))
            self.assertIsNone(hg.load_key("copied"))

    def test_echo_decision_is_unchanged_without_the_list(self) -> None:
        with mock.patch.object(sa, "NO_ECHO_REPOS", opconf.get_list("no_echo_repos")):
            self.assertEqual(sa.NO_ECHO_REPOS, ())
            for slug in ("maw", "widget", "never-classified", ""):
                self.assertFalse(sa.requires_echo(slug), slug)

    def test_the_stricter_opt_in_stays_in_code(self) -> None:
        # ECHO_REQUIRED_REPOS is the checkpoint direction: a missing file must
        # never be able to drop it, so it is not an opconf key.
        self.assertNotIn("echo_required_repos", opconf.KEYS)
        with mock.patch.object(sa, "ECHO_REQUIRED_REPOS", ("gadget",)):
            self.assertTrue(sa.requires_echo("gadget"))


class PresentConfigSameBehaviour(Base):
    def test_the_file_id_lands_in_the_grant(self) -> None:
        self.present()
        self.assertEqual(_assemble()["pubkey_id"], "op-phone-1")
        self.assertEqual(rc.relay_pubkey_id(), "op-relay-1")
        self.assertEqual(opconf.require("grant_pubkey_id"), "op-grant-1")

    def test_an_explicit_id_beats_the_config(self) -> None:
        self.present()
        self.assertEqual(_assemble(pubkey_id="other-key")["pubkey_id"], "other-key")

    def test_env_beats_the_file(self) -> None:
        self.present()
        with mock.patch.dict(os.environ, {"MAW_PHONE_PUBKEY_ID": "env-phone"}):
            self.assertEqual(_assemble()["pubkey_id"], "env-phone")

    def test_register_window_claims_the_configured_id(self) -> None:
        self.present()
        self.assertEqual(serve.MintServer(kind="register", window_seconds=1,
                                          port=0).register_pubkey_id, "op-phone-1")

    def test_mint_cli_uses_the_configured_id(self) -> None:
        self.present()
        seen: list = []

        def fake_mint(**kw):
            seen.append(kw["pubkey_id"])
            raise ValueError("stop here")

        instr = Path(self.tmp.name) / "i.txt"
        instr.write_text("x", encoding="utf-8")
        with mock.patch.object(gmint, "mint", side_effect=fake_mint), \
                contextlib.redirect_stderr(io.StringIO()):
            gmint.main(["--instruction-file", str(instr), "--scope", "prompt.relay",
                        "--target", "t", "--ttl", "60"], signer=lambda d, r="": None)
        self.assertEqual(seen, ["op-grant-1"])

    def test_no_echo_list_is_read_at_import_and_trimmed(self) -> None:
        # A fresh interpreter: reloading session_auth in-process would leave
        # other test modules holding the old module's classes.
        self.present()
        code = ("from guards import session_auth as sa; print(sa.NO_ECHO_REPOS); "
                "print([sa.requires_echo(s) for s in ('maw', 'widget', 'never-classified')])")
        out = subprocess.run([sys.executable, "-c", code], cwd=MAW, env=dict(os.environ),
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "('maw', 'widget', 'gadget')\n[False, False, False]\n")

    def test_the_python39_parser_reads_the_same_values(self) -> None:
        self.present()
        with mock.patch.object(opconf, "tomllib", None):
            self.assertEqual(opconf.require("phone_pubkey_id"), "op-phone-1")
            self.assertEqual(opconf.get_list("no_echo_repos"), ("maw", "widget", "gadget"))

    def test_cli_prints_an_id_unexpanded(self) -> None:
        self.present()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(opconf.main(["relay_pubkey_id"]), 0)
        self.assertEqual(out.getvalue(), "op-relay-1\n")


if __name__ == "__main__":
    unittest.main()
