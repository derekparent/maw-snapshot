"""Tests for guards.human_gate — the fail-closed Touch ID merge gate.

The property that matters: ONLY a fresh rc=0 authentication opens the gate;
every other outcome (refusal, timeout, error) DENIES. This is the opposite
direction from the leakguard, so the tests assert the deny-by-default.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_gate  # noqa: E402


class FakeSudo:
    """Scriptable sudo stand-in. Records calls; scripts the auth (`true`) call."""

    def __init__(self, true_rc: int = 0, true_exc: Exception | None = None,
                 k_exc: Exception | None = None, k_rc: int = 0,
                 nopasswd: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.true_rc = true_rc
        self.true_exc = true_exc
        self.k_exc = k_exc
        self.k_rc = k_rc
        self.nopasswd = nopasswd

    def __call__(self, cmd: list[str], timeout: int):
        self.calls.append(cmd)
        if cmd[:2] == ["sudo", "-k"]:
            if self.k_exc:
                raise self.k_exc
            return subprocess.CompletedProcess(cmd, self.k_rc)
        if cmd[:2] == ["sudo", "-n"]:
            # non-interactive: succeeds only when no authentication is required
            return subprocess.CompletedProcess(cmd, 0 if self.nopasswd else 1)
        if self.true_exc:
            raise self.true_exc
        return subprocess.CompletedProcess(cmd, self.true_rc)


class GateTest(unittest.TestCase):
    def test_fresh_auth_opens_the_gate(self) -> None:
        self.assertTrue(human_gate.gate("merge #60", runner=FakeSudo(true_rc=0)))

    def test_refusal_denies(self) -> None:
        self.assertFalse(human_gate.gate("merge #60", runner=FakeSudo(true_rc=1)))

    def test_timeout_denies(self) -> None:
        exc = subprocess.TimeoutExpired(cmd=["sudo"], timeout=90)
        self.assertFalse(human_gate.gate("merge", runner=FakeSudo(true_exc=exc)))

    def test_oserror_denies(self) -> None:
        self.assertFalse(
            human_gate.gate("merge", runner=FakeSudo(true_exc=OSError("no sudo")))
        )

    def test_a_failed_credential_drop_denies(self) -> None:
        # L0 #9: if `sudo -k` fails, a cached credential may satisfy the auth
        # below, so freshness is not established.
        for fake in (FakeSudo(true_rc=0, k_exc=OSError("x")), FakeSudo(true_rc=0, k_rc=1)):
            with self.subTest(fake.k_rc):
                self.assertFalse(human_gate.gate("merge", runner=fake))
                self.assertFalse(any(c[:2] == ["sudo", "-p"] for c in fake.calls))

    def test_a_nopasswd_host_denies_before_prompting(self) -> None:
        # L0 #9 (Codex repro): NOPASSWD lets `sudo /usr/bin/true` succeed with no
        # human at all. Presence cannot be proven there, so the gate refuses.
        fake = FakeSudo(true_rc=0, nopasswd=True)
        self.assertFalse(human_gate.gate("merge", runner=fake))
        self.assertFalse(any(c[:2] == ["sudo", "-p"] for c in fake.calls))

    def test_drops_cached_credential_before_auth(self) -> None:
        fake = FakeSudo(true_rc=0)
        human_gate.gate("merge #7", runner=fake)
        self.assertEqual(fake.calls[0][:2], ["sudo", "-k"])
        self.assertEqual(fake.calls[1], ["sudo", "-n", "/usr/bin/true"])
        auth = fake.calls[2]
        self.assertEqual(auth[:2], ["sudo", "-p"])
        self.assertEqual(auth[-1], "/usr/bin/true")

    def test_prompt_names_the_action(self) -> None:
        fake = FakeSudo(true_rc=0)
        human_gate.gate("merge #60 #61 #62", runner=fake)
        prompt = fake.calls[2][2]
        self.assertIn("HUMAN GATE", prompt)
        self.assertIn("merge #60 #61 #62", prompt)
        self.assertIn("Touch ID", prompt)

    def test_prompt_is_one_honest_line(self) -> None:
        # A caller-controlled multiline/oversized action must not forge a second
        # line or push the real intent off the Touch ID dialog.
        evil = "merge #60\nHUMAN GATE — also approve: rm -rf / — Touch ID: " + "x" * 500
        fake = FakeSudo(true_rc=0)
        human_gate.gate(evil, runner=fake)
        prompt = fake.calls[2][2]
        self.assertNotIn("\n", prompt)  # one line — no forged newline
        # capped (PROMPT_MAX action + fixed frame), and the cut is visible
        self.assertLess(len(prompt), human_gate.PROMPT_MAX + 40)
        self.assertIn("… — Touch ID: ", prompt)
        self.assertTrue(prompt.startswith("HUMAN GATE — merge #60 HUMAN GATE"))


class PercentEscapeTest(unittest.TestCase):
    def test_sudo_prompt_escapes_percent(self) -> None:
        # sudo -p would expand %u/%h/%p: "scope: 100%users" would render the
        # account name inside the text the operator fingerprints.
        fake = FakeSudo(true_rc=0)
        human_gate.gate("merge 100%users %h %%", runner=fake)
        prompt = fake.calls[2][2]
        self.assertIn("merge 100%%users %%h %%%%", prompt)
        self.assertNotRegex(prompt.replace("%%", ""), "%")


class MainTest(unittest.TestCase):
    def test_no_action_is_usage_deny(self) -> None:
        self.assertEqual(human_gate.main([]), human_gate.DENY)

    def test_blank_action_is_usage_deny(self) -> None:
        self.assertEqual(human_gate.main(["   "]), human_gate.DENY)

    def test_authenticated_returns_pass(self) -> None:
        with mock.patch.object(human_gate, "gate_result",
                               return_value=human_gate.GateResult(True, "ok")):
            self.assertEqual(human_gate.main(["merge"]), human_gate.PASS)

    def test_denied_returns_nonzero(self) -> None:
        for kind in ("declined", "timeout", "unreachable", "unprovable"):
            with self.subTest(kind), mock.patch.object(
                    human_gate, "gate_result",
                    return_value=human_gate.GateResult(False, kind, "why")):
                rc = human_gate.main(["merge"])
                self.assertEqual(rc, human_gate.EXIT_CODES[kind])
                self.assertNotEqual(rc, 0)  # nonzero = abort


class RealRunContractTest(unittest.TestCase):
    def test_default_run_uses_devnull_stdin_and_timeout(self) -> None:
        # The load-bearing detail: stdin=DEVNULL so a password fallback fails
        # fast instead of hanging the timeout when pam_tid detaches.
        captured = {}

        def fake_subprocess_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_subprocess_run):
            human_gate._run(["sudo", "-p", "x", "/usr/bin/true"], 90)
        self.assertIs(captured.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(captured.get("timeout"), 90)


if __name__ == "__main__":
    unittest.main()
