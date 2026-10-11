"""Tests for maw/bin/maw-run — the PYTHONPATH-setting module wrapper."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "maw" / "bin" / "maw-run"


class MawRunTest(unittest.TestCase):
    def test_no_args_prints_usage_and_exits_2(self) -> None:
        r = subprocess.run([str(BIN)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: maw-run", r.stderr)

    def test_runs_module_with_plugin_root_on_path(self) -> None:
        r = subprocess.run([str(BIN), "opconf", "--help"], capture_output=True,
                           text=True, cwd="/")
        # opconf's own usage exit (2) proves the module imported via the wrapper.
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: python3 -m opconf", r.stderr)

    def test_resolves_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "maw-run"
            os.symlink(BIN, link)
            r = subprocess.run([str(link), "opconf", "--help"],
                               capture_output=True, text=True, cwd="/")
            self.assertEqual(r.returncode, 2)
            self.assertIn("usage: python3 -m opconf", r.stderr)

    def test_a_cwd_module_does_not_shadow_maws(self) -> None:
        # `python3 -m` puts the CWD first on sys.path: run from a repo holding an
        # `opconf.py`, the repo's file would execute in place of maw's.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "opconf.py").write_text('print("SHADOWED")\n', encoding="utf-8")
            r = subprocess.run([str(BIN), "opconf", "--help"],
                               capture_output=True, text=True, cwd=d)
        self.assertNotIn("SHADOWED", r.stdout + r.stderr)
        self.assertIn("usage: python3 -m opconf", r.stderr)

    @unittest.skipUnless(Path("/usr/bin/python3").exists(), "no system python3")
    def test_no_shadow_on_a_pre_311_python(self) -> None:
        # PYTHONSAFEPATH and -P exist only from 3.11; macOS ships 3.9 as
        # /usr/bin/python3. The guard must not depend on the interpreter.
        env = {**os.environ, "PATH": "/usr/bin:" + os.environ.get("PATH", "")}
        with tempfile.TemporaryDirectory() as d:
            # runpy too: the wrapper's own import must not resolve from the cwd.
            for name in ("opconf.py", "runpy.py"):
                (Path(d) / name).write_text(f'print("SHADOWED {name}")\n', encoding="utf-8")
            r = subprocess.run([str(BIN), "opconf", "--help"], capture_output=True,
                               text=True, cwd=d, env=env)
        self.assertNotIn("SHADOWED", r.stdout + r.stderr)
        self.assertIn("usage: python3 -m opconf", r.stderr)

    def test_seat_wrapper_drops_the_cwd_entry_too(self) -> None:
        seat = (BIN.parent / "maw-seat").read_text(encoding="utf-8")
        self.assertIn('sys.path[0] == ""', seat)
        # the cwd entry goes BEFORE runpy is imported
        self.assertLess(seat.index("del sys.path[0]"), seat.index("import runpy"))
        self.assertNotIn("exec python3 -m", seat)


if __name__ == "__main__":
    unittest.main()
