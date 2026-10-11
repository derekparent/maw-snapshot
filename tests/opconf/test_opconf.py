"""opconf: operator-local settings resolve env > ~/.maw/operator.toml > neutral
default, never fail open into a private path, and never wedge a caller."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

import opconf  # noqa: E402
from net import board  # noqa: E402

ENV_VARS = ("MAW_HANDOFF_DIR", "MAW_BOARD_SH", "MAW_BOARD_DIR", "MAW_OPERATOR_CONFIG")


class OpconfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "operator.toml"
        env = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
        env["MAW_OPERATOR_CONFIG"] = str(self.cfg)
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_absent_file_gives_the_neutral_default(self) -> None:
        self.assertEqual(opconf.get("handoff_dir"), "~/.maw/handoff/pending")
        self.assertEqual(opconf.get("board_sh"), "")
        self.assertIsNone(opconf.get_path("board_sh"))

    def test_env_beats_file_beats_default(self) -> None:
        self.cfg.write_text('handoff_dir = "/from/file"\nboard_sh = "/file/board.sh"\n')
        self.assertEqual(opconf.get("handoff_dir"), "/from/file")
        with mock.patch.dict(os.environ, {"MAW_HANDOFF_DIR": "/from/env"}):
            self.assertEqual(opconf.get("handoff_dir"), "/from/env")
        self.assertEqual(opconf.get_path("board_sh"), Path("/file/board.sh"))

    def test_malformed_file_warns_and_falls_back(self) -> None:
        self.cfg.write_text("handoff_dir = [unterminated\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(opconf.get("handoff_dir"), "~/.maw/handoff/pending")
        self.assertIn("opconf: ignoring", err.getvalue())

    def test_a_non_string_value_warns_and_falls_back(self) -> None:
        self.cfg.write_text("board_sh = 3\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(opconf.get("board_sh"), "")
        self.assertIn("not a string", err.getvalue())

    def test_without_tomllib_a_present_file_still_resolves(self) -> None:
        # Advisor on #278: /usr/bin/python3 (3.9) has no tomllib, and a present
        # file must not be silently skipped there.
        self.cfg.write_text('# maw operator config\n\nhandoff_dir = "/from/file"  # note\n'
                            'board_sh = "/file/board.sh"\n')
        with mock.patch.object(opconf, "tomllib", None):
            self.assertEqual(opconf.get("handoff_dir"), "/from/file")
            self.assertEqual(opconf.get_path("board_sh"), Path("/file/board.sh"))

    def test_without_tomllib_an_unparseable_line_warns_and_falls_back(self) -> None:
        self.cfg.write_text('handoff_dir = "/from/file"\n[table]\n')
        err = io.StringIO()
        with mock.patch.object(opconf, "tomllib", None), contextlib.redirect_stderr(err):
            self.assertEqual(opconf.get("handoff_dir"), "~/.maw/handoff/pending")
        self.assertIn("opconf: ignoring", err.getvalue())
        self.assertIn("no tomllib", err.getvalue())

    def test_shipped_defaults_are_neutral(self) -> None:
        # Every default is empty (not configured) or under maw's own ~/.maw dir,
        # so no machine's private layout ships as a default.
        for key, (_env, default) in opconf.KEYS.items():
            with self.subTest(key=key):
                self.assertTrue(default == "" or default.startswith("~/.maw/"), default)

    def test_cli_prints_the_expanded_value(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(opconf.main(["handoff_dir"]), 0)
        self.assertEqual(out.getvalue().strip(), str(Path("~/.maw/handoff/pending").expanduser()))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(opconf.main(["nope"]), 2)

    def test_unconfigured_board_is_off(self) -> None:
        sent = []
        ok = board.write(board.build_fragment(name="maw", status="working", headline="h"),
                         runner=lambda cmd, s: sent.append(cmd))
        self.assertFalse(ok)
        self.assertEqual(sent, [])
        self.assertEqual(board._existing("maw"), {})

    def test_configured_board_is_used(self) -> None:
        sh = Path(self.tmp.name) / "board.sh"
        sh.write_text("#!/bin/sh\n")
        self.cfg.write_text(f'board_sh = "{sh}"\n')
        sent = []
        import subprocess
        ok = board.write(board.build_fragment(name="maw", status="working", headline="h"),
                         runner=lambda cmd, s: sent.append(cmd) or subprocess.CompletedProcess(cmd, 0))
        self.assertTrue(ok)
        self.assertEqual(sent[0][0], str(sh))


if __name__ == "__main__":
    unittest.main()
