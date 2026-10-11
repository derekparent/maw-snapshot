"""`/maw:init` on a Python below the floor says which Python it needs, in one
line, with no traceback (final pass 2026-10-09, finding 6)."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAW = ROOT / "maw"
sys.path.insert(0, str(MAW))

from init import pyfloor  # noqa: E402


def _old_python() -> "str | None":
    """A real interpreter below the floor, if this machine has one (stock
    macOS: /usr/bin/python3 is 3.9)."""
    for exe in ("/usr/bin/python3", "/usr/bin/python3.9", "/usr/bin/python3.10"):
        if not os.access(exe, os.X_OK):
            continue
        r = subprocess.run([exe, "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip() == "True":
            return exe
    return None


class FloorMessageTests(unittest.TestCase):
    def test_below_the_floor_is_one_line_naming_both_versions(self) -> None:
        msg = pyfloor.floor_message((3, 9, 6, "final", 0), "/usr/bin/python3")
        self.assertIsNotNone(msg)
        self.assertEqual(len(msg.splitlines()), 1)
        self.assertIn("Python 3.11 or later", msg)
        self.assertIn("3.9.6", msg)
        self.assertIn("/usr/bin/python3", msg)

    def test_the_floor_and_above_pass(self) -> None:
        for info in ((3, 11, 0), (3, 12, 4), (3, 14, 2), (4, 0, 0)):
            self.assertIsNone(pyfloor.floor_message(info, "x"), info)
        self.assertIsNone(pyfloor.floor_message())

    def test_just_below_the_floor_refuses(self) -> None:
        self.assertIsNotNone(pyfloor.floor_message((3, 10, 99), "x"))
        self.assertIsNotNone(pyfloor.floor_message((2, 7, 18), "x"))

    def test_require_exits_1_with_the_line_on_stderr(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            pyfloor.require((3, 9, 6), "/usr/bin/python3")
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_the_module_is_plain_enough_for_an_old_interpreter(self) -> None:
        """No annotations import, no f-string, no walrus, no match: the guard
        has to load on the interpreter it is refusing."""
        text = (MAW / "init" / "pyfloor.py").read_text(encoding="utf-8")
        for needle in ("from __future__", ' f"', " f\'", ":=", "match ", "tomllib"):
            if needle == "tomllib":
                self.assertNotIn("import tomllib", text)
            else:
                self.assertNotIn(needle, text)

    def test_init_checks_the_floor_before_the_import_that_needs_it(self) -> None:
        text = (MAW / "init" / "cli.py").read_text(encoding="utf-8")
        self.assertLess(text.index("pyfloor.require()"), text.index("from init.stack_detection"))


@unittest.skipIf(_old_python() is None, "no interpreter below 3.11 on this machine")
class RealOldInterpreterTests(unittest.TestCase):
    def test_init_on_an_old_python_is_one_line_and_exit_1(self) -> None:
        old = _old_python()
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "bin"
            shim.mkdir()
            (shim / "python3").symlink_to(old)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            env = {"PATH": f"{shim}:/usr/bin:/bin", "HOME": tmp}
            r = subprocess.run([str(MAW / "bin" / "maw-run"), "init.cli"], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stderr[-2000:])
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertIn("Python 3.11 or later", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertFalse((repo / ".maw").exists())


if __name__ == "__main__":
    unittest.main()
