"""#206 item 7: a refused Touch ID arm says WHICH failure it was.

Advisors burned three arms on one generic line ("no fresh human authentication
(timeout, refusal, or error)") when the sheet had never appeared. The gate now
classifies a denial (declined / timeout / unreachable / unprovable), with its
own reason text and exit code per class. The classification may only change
the WORDS and the CODE: the falsifier below drives every refusal path through
`gate`, `gate_result`, `session_auth.arm` and the `arm` CLI and asserts each one
still refuses (nonzero, no token written).

No test here may reach a real sudo: every gate call takes an explicit runner,
and `subprocess.run` is replaced for the whole module so a regression fails
instead of putting a Touch ID sheet on the operator's screen.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_gate, session_auth  # noqa: E402

_NO_REAL = mock.patch.object(
    subprocess, "run",
    side_effect=AssertionError("a test reached the real subprocess.run"))


def setUpModule() -> None:  # noqa: N802
    _NO_REAL.start()


def tearDownModule() -> None:  # noqa: N802
    _NO_REAL.stop()


FALLBACK = ("sudo: a terminal is required to read the password; either use the "
            "-S option to read from standard input or configure an askpass helper\n"
            "sudo: a password is required\n")


class Sudo:
    """Scripted sudo: `k` and `n` script the drop and the probe, `auth` the
    prompted call (an exception instance, or (rc, stderr))."""

    def __init__(self, auth: "object" = (1, FALLBACK), k: "object" = (0, ""),
                 n: "object" = (1, "sudo: a password is required\n")) -> None:
        self.script = {"-k": k, "-n": n, "-p": auth}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: int):
        self.calls.append(cmd)
        step = self.script[cmd[1]]
        if isinstance(step, BaseException):
            raise step
        rc, err = step
        return subprocess.CompletedProcess(cmd, rc, "", err)

    @property
    def prompted(self) -> bool:
        return any(c[1] == "-p" for c in self.calls)


def clock(elapsed: float):
    ticks = iter([100.0, 100.0 + elapsed])
    return lambda: next(ticks)


TIMEOUT = subprocess.TimeoutExpired(cmd=["sudo"], timeout=90)

# (name, sudo, elapsed seconds, expected kind)
CASES = [
    ("sandboxed drop", Sudo(k=PermissionError(1, "Operation not permitted")), 0.0,
     "unreachable"),
    ("drop exits nonzero", Sudo(k=(1, "sudo: unable to drop\n")), 0.0, "unreachable"),
    ("probe will not run", Sudo(n=OSError("no sudo")), 0.0, "unreachable"),
    ("NOPASSWD host", Sudo(n=(0, "")), 0.0, "unprovable"),
    ("auth times out", Sudo(auth=TIMEOUT), 0.0, "timeout"),
    ("auth will not run", Sudo(auth=OSError("exec failed")), 0.0, "unreachable"),
    ("no new privileges", Sudo(auth=(1, 'sudo: The "no new privileges" flag is set\n')),
     30.0, "unreachable"),
    ("instant fallback (no GUI)", Sudo(auth=(1, FALLBACK)), 0.1, "unreachable"),
    ("sheet cancelled", Sudo(auth=(1, FALLBACK)), 7.5, "declined"),
    ("wrong password", Sudo(auth=(1, "Sorry, try again.\nsudo: 1 incorrect password attempt\n")),
     0.1, "declined"),
    ("silent failure, human-scale", Sudo(auth=(1, "")), 12.0, "declined"),
]


class ClassifyTest(unittest.TestCase):
    def test_each_failure_gets_its_class(self) -> None:
        for name, sudo, elapsed, kind in CASES:
            with self.subTest(name):
                res = human_gate.gate_result("arm", runner=sudo, clock=clock(elapsed))
                self.assertFalse(res.ok)
                self.assertEqual(res.kind, kind)
                self.assertTrue(res.detail)

    def test_unreachable_says_the_sheet_was_not_shown(self) -> None:
        res = human_gate.gate_result("arm", runner=Sudo(auth=(1, FALLBACK)),
                                     clock=clock(0.2))
        self.assertIn("never appeared", res.detail)
        self.assertIn("pam_tid", res.detail)
        self.assertIn("a password is required", res.detail)  # sudo's own words kept

    def test_declined_says_the_prompt_was_up(self) -> None:
        res = human_gate.gate_result("arm", runner=Sudo(auth=(1, FALLBACK)),
                                     clock=clock(7.5))
        self.assertIn("was up 7.5s", res.detail)

    def test_no_prompt_before_the_drop_and_probe_pass(self) -> None:
        for sudo in (Sudo(k=(1, "")), Sudo(n=(0, "")), Sudo(n=OSError("x"))):
            with self.subTest(sudo.script):
                human_gate.gate_result("arm", runner=sudo, clock=clock(0))
                self.assertFalse(sudo.prompted)

    def test_success_is_unchanged(self) -> None:
        res = human_gate.gate_result("arm", runner=Sudo(auth=(0, "")), clock=clock(3))
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, "ok")
        self.assertEqual(res.exit_code, 0)
        self.assertTrue(human_gate.gate("arm", runner=Sudo(auth=(0, ""))))


class ExitCodeTest(unittest.TestCase):
    def test_codes_are_distinct_and_clear_of_usage_codes(self) -> None:
        codes = human_gate.EXIT_CODES
        self.assertEqual(codes["ok"], 0)
        denials = [c for k, c in codes.items() if k != "ok"]
        self.assertEqual(len(denials), len(set(denials)))
        # 1 = refusals before the gate (and usage); 2 = argparse errors.
        self.assertFalse({0, 1, 2} & set(denials))

    def test_cli_help_documents_every_code(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            session_auth.main(["arm", "--help"])
        text = out.getvalue()
        for kind, code in human_gate.EXIT_CODES.items():
            if kind != "ok":
                self.assertRegex(text, rf"\b{code} {kind}\b")


class RunnerResolvesAtCallTime(unittest.TestCase):
    def test_patching_run_reaches_gate_without_a_runner(self) -> None:
        # Before #206 item 7, gate's default runner was bound at import: patching
        # human_gate._run did nothing and the REAL sudo ran.
        fake = Sudo(auth=(0, ""))
        with mock.patch.object(human_gate, "_run", fake):
            self.assertTrue(human_gate.gate("arm"))
        self.assertTrue(fake.prompted)


class FalsifierEveryRefusalRefuses(unittest.TestCase):
    """The class is wording only: every denial path still refuses everywhere."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _arm(self, gate_fn, scope: str = "merge the exit-reasons PRs"):
        return session_auth.arm(
            scope, self.root, session_id="sess-1", pid=os.getpid(),
            gate_fn=gate_fn, seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
            runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))

    def _token(self) -> Path:
        return self.root / session_auth.TOKEN_REL

    def test_gate_and_arm_refuse_on_every_class(self) -> None:
        for name, sudo, elapsed, kind in CASES:
            with self.subTest(name):
                self.assertFalse(human_gate.gate("arm", runner=sudo))
                ok, msg = self._arm(
                    lambda a, s=sudo, e=elapsed: human_gate.gate_result(
                        a, runner=s, clock=clock(e)))
                self.assertFalse(ok)
                self.assertFalse(self._token().exists())
                self.assertEqual(msg.exit_code, human_gate.EXIT_CODES[kind])
                self.assertNotEqual(msg.exit_code, 0)
                self.assertIn(f"refusing to arm ({kind})", msg)
                self.assertIn("no fresh human authentication", msg)
                self.assertIn("no token", msg)

    def test_a_plain_false_gate_keeps_the_old_line_and_code(self) -> None:
        ok, msg = self._arm(lambda _a: False)
        self.assertFalse(ok)
        self.assertFalse(self._token().exists())
        self.assertEqual(getattr(msg, "exit_code", session_auth.DENY), session_auth.DENY)
        self.assertIn("(timeout, refusal, or error)", msg)

    def test_the_cli_exits_with_the_class_and_writes_no_token(self) -> None:
        real_arm = session_auth.arm
        for name, sudo, elapsed, kind in CASES:
            with self.subTest(name):
                def arm(scope, repo_root, grant=None, prs=None, s=sudo, e=elapsed):
                    return real_arm(
                        scope, repo_root, session_id="sess-1", pid=os.getpid(),
                        gate_fn=lambda a: human_gate.gate_result(
                            a, runner=s, clock=clock(e)),
                        seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                        runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""),
                        grant=grant, prs=prs)
                err = io.StringIO()
                with mock.patch.object(session_auth, "arm", arm), \
                        contextlib.redirect_stderr(err):
                    rc = session_auth.main(["--repo-root", str(self.root), "arm",
                                            "--scope", "merge the exit-reasons PRs"])
                self.assertEqual(rc, human_gate.EXIT_CODES[kind])
                self.assertNotEqual(rc, 0)
                self.assertFalse(self._token().exists())
                self.assertIn(f"({kind})", err.getvalue())

    def test_the_cli_still_arms_on_success(self) -> None:
        real_arm = session_auth.arm

        def arm(scope, repo_root, grant=None, prs=None):
            return real_arm(
                scope, repo_root, session_id="sess-1", pid=os.getpid(),
                gate_fn=lambda a: human_gate.gate_result(
                    a, runner=Sudo(auth=(0, "")), clock=clock(4)),
                seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""),
                grant=grant, prs=prs)
        with mock.patch.object(session_auth, "arm", arm), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = session_auth.main(["--repo-root", str(self.root), "arm",
                                    "--scope", "merge the exit-reasons PRs"])
        self.assertEqual(rc, 0)
        self.assertTrue(self._token().exists())


if __name__ == "__main__":
    unittest.main()
