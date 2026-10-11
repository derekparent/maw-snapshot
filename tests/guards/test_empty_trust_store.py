"""An EMPTY grant trust store says no key is installed (RELEASE-NOTES known debt).

The export ships no operator keys, so a fresh install refused every phone grant
with `unknown pubkey_id: '<id>'`, which reads like a typo in the grant. The
reason now says no key is installed and where keys go. Wording only: every one
of these grants still refuses. A store that holds keys, just not the grant's,
keeps `unknown pubkey_id`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from guards import human_grant as hg  # noqa: E402
from guards import p256  # noqa: E402
from phone_mint import request as req  # noqa: E402
from test_human_grant import GrantTestBase, _now  # noqa: E402

NO_KEY = "no grant public key is installed"


class ReasonTest(GrantTestBase):
    def _clear(self) -> None:
        for f in self.keys.glob("*.pub"):
            f.unlink()

    def test_populated_store_with_the_key_verifies(self) -> None:
        g, instr = self.make_grant()
        self.assertTrue(hg.verify(g, instr, _now()).ok)

    def test_populated_store_without_the_id_keeps_unknown(self) -> None:
        g, instr = self.make_grant(pubkey_id="some-other-key")
        r = hg.verify(g, instr, _now())
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "unknown pubkey_id: 'some-other-key'")

    def test_empty_store_says_no_key_installed(self) -> None:
        self._clear()
        g, instr = self.make_grant()
        r = hg.verify(g, instr, _now())
        self.assertFalse(r.ok)
        self.assertIn(NO_KEY, r.reason)
        self.assertNotIn("unknown pubkey_id", r.reason)
        self.assertIn(str(self.keys), r.reason)            # where it looked
        self.assertIn("maw/guards/keys/README.md", r.reason)  # how keys go in
        self.assertIn("'dp-test'", r.reason)               # which id it wanted

    def test_missing_store_dir_says_no_key_installed(self) -> None:
        self._clear()
        self.keys.rmdir()
        g, instr = self.make_grant()
        r = hg.verify(g, instr, _now())
        self.assertFalse(r.ok)
        self.assertIn(NO_KEY, r.reason)

    def test_a_store_of_only_malformed_keys_is_not_empty(self) -> None:
        # A file is there; calling the store empty would hide the broken file.
        self._clear()
        (self.keys / "broken.pub").write_text("not json", encoding="utf-8")
        g, instr = self.make_grant(pubkey_id="broken")
        r = hg.verify(g, instr, _now())
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "unknown pubkey_id: 'broken'")

    def test_non_pub_files_do_not_count_as_keys(self) -> None:
        self._clear()
        (self.keys / "README.md").write_text("# keys\n", encoding="utf-8")
        g, instr = self.make_grant()
        r = hg.verify(g, instr, _now())
        self.assertFalse(r.ok)
        self.assertIn(NO_KEY, r.reason)


class PhoneRequestTest(unittest.TestCase):
    def test_request_refuses_with_the_no_key_reason_before_serving(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "MAW_GRANT_KEYS_DIR": d,
                "MAW_GRANTS_DIR": str(Path(d) / "grants"),
                "MAW_GRANT_PUBKEY_ID": "test-grant-key",
                "MAW_PHONE_PUBKEY_ID": "test-phone-key",
                "MAW_RELAY_PUBKEY_ID": "test-relay-key"}):
            def never(*_a, **_k):
                raise AssertionError("served or ran a command with no key installed")
            with self.assertRaises(ValueError) as cm:
                req.request(instruction="do the thing", scope_class="prompt.relay",
                            target="cos", ttl_seconds=900,
                            now_iso="2026-07-21T05:40:00Z", what="relay a prompt",
                            url="https://h.example/", run=never, server_factory=never)
        msg = str(cm.exception)
        self.assertIn(NO_KEY, msg)
        self.assertIn("commit the phone key first", msg)

    def test_request_with_other_keys_keeps_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "MAW_GRANT_KEYS_DIR": d,
                "MAW_GRANTS_DIR": str(Path(d) / "grants"),
                "MAW_GRANT_PUBKEY_ID": "test-grant-key",
                "MAW_PHONE_PUBKEY_ID": "test-phone-key",
                "MAW_RELAY_PUBKEY_ID": "test-relay-key"}):
            (Path(d) / "another-key.pub").write_text(json.dumps({
                "pubkey_id": "another-key", "x": f"{p256.GX:x}", "y": f"{p256.GY:x}",
                "not_before": "2020-01-01T00:00:00Z",
                "not_after": "2099-01-01T00:00:00Z"}), encoding="utf-8")

            def never(*_a, **_k):
                raise AssertionError("served or ran a command with no matching key")
            with self.assertRaises(ValueError) as cm:
                req.request(instruction="do the thing", scope_class="prompt.relay",
                            target="cos", ttl_seconds=900,
                            now_iso="2026-07-21T05:40:00Z", what="relay a prompt",
                            url="https://h.example/", run=never, server_factory=never)
        self.assertIn("unknown pubkey_id", str(cm.exception))
        self.assertNotIn(NO_KEY, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
