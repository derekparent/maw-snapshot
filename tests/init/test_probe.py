"""Tests for the capability probe runner plumbing (Phase 1c-part-3).

Covers the contract §10 surface: gate cadence (missing/fresh/stale/
malformed/version-mismatch/forced + TTL knob), the stdin→schema-v2 writer
(including the §7 rule that a bad payload never touches the runtime file),
the banner renderer, and the hook's plugin-surface registration.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from init.capabilities import (  # noqa: E402
    ALL_CAPABILITIES,
    RUNTIME_FILE,
    CapabilityRegistry,
    read_capability_state,
    write_failures_file,
)
from init.probe import (  # noqa: E402
    DEFAULT_TTL_HOURS,
    PROBE_ERRORS,
    PROBE_LOG,
    capabilities_line,
    cmd_write,
    probe_due,
    probe_prompt,
    probe_state,
    read_ttl_hours,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_PASS = {name: {"probe_passed": True} for name in ALL_CAPABILITIES}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProbeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".maw").mkdir()
        (self.repo / ".maw" / "config.yaml").write_text(
            "repo_slug: probe-test\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_snapshot(self) -> None:
        registry = CapabilityRegistry()
        for name in ALL_CAPABILITIES:
            registry.record(name, probe_passed=True)
        write_failures_file(self.repo / RUNTIME_FILE, registry)

    def runtime_path(self) -> Path:
        return self.repo / RUNTIME_FILE

    def write_results(self, stdin_text: str) -> int:
        """cmd_write with its stdout/stderr captured (keeps the runner clean)."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            return cmd_write(self.repo, stdin_text)


class TestGate(ProbeTestCase):
    def test_no_maw_config_never_due(self) -> None:
        bare = Path(self._tmp.name) / "bare"
        bare.mkdir()
        due, reason = probe_due(bare, env={})
        self.assertFalse(due)
        self.assertEqual(reason, "no-maw-config")

    def test_missing_snapshot_due(self) -> None:
        due, reason = probe_due(self.repo, env={})
        self.assertTrue(due)
        self.assertEqual(reason, "missing")

    def test_fresh_snapshot_not_due(self) -> None:
        self.write_snapshot()
        due, reason = probe_due(self.repo, env={})
        self.assertFalse(due)
        self.assertEqual(reason, "fresh")

    def test_stale_snapshot_due(self) -> None:
        self.write_snapshot()
        later = _utcnow() + timedelta(hours=DEFAULT_TTL_HOURS + 1)
        due, reason = probe_due(self.repo, now=later, env={})
        self.assertTrue(due)
        self.assertEqual(reason, "stale")

    def test_malformed_snapshot_due(self) -> None:
        self.runtime_path().parent.mkdir(parents=True, exist_ok=True)
        self.runtime_path().write_text("{not json", encoding="utf-8")
        due, reason = probe_due(self.repo, env={})
        self.assertTrue(due)
        self.assertEqual(reason, "malformed")

    def test_valid_envelope_with_garbage_results_due(self) -> None:
        # schema_version + written_at valid but results unusable: the gate
        # must not report fresh while every reader raises — that wedges the
        # probe for a full TTL (Codex P2 #2).
        self.runtime_path().parent.mkdir(parents=True, exist_ok=True)
        from init.capabilities import SCHEMA_VERSION

        self.runtime_path().write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "written_at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "results": {"cron_create": {"name": "cron_create"}},
                }
            ),
            encoding="utf-8",
        )
        due, reason = probe_due(self.repo, env={})
        self.assertTrue(due)
        self.assertEqual(reason, "malformed")

    def test_version_mismatch_due(self) -> None:
        self.runtime_path().parent.mkdir(parents=True, exist_ok=True)
        self.runtime_path().write_text(
            json.dumps({"schema_version": 99, "written_at": "2026-06-10T00:00:00Z"}),
            encoding="utf-8",
        )
        due, reason = probe_due(self.repo, env={})
        self.assertTrue(due)
        self.assertEqual(reason, "version-mismatch")

    def test_reprobe_env_forces_even_when_fresh(self) -> None:
        self.write_snapshot()
        due, reason = probe_due(self.repo, env={"MAW_REPROBE": "1"})
        self.assertTrue(due)
        self.assertIn("forced", reason)

    def test_ttl_knob_from_config(self) -> None:
        (self.repo / ".maw" / "config.yaml").write_text(
            "repo_slug: probe-test\ncapability_probe_ttl_hours: 1\n",
            encoding="utf-8",
        )
        self.assertEqual(read_ttl_hours(self.repo), 1)
        self.write_snapshot()
        within = _utcnow() + timedelta(minutes=30)
        past = _utcnow() + timedelta(hours=2)
        self.assertEqual(probe_state(self.repo, now=within), "fresh")
        self.assertEqual(probe_state(self.repo, now=past), "stale")

    def test_ttl_default_when_knob_absent(self) -> None:
        self.assertEqual(read_ttl_hours(self.repo), DEFAULT_TTL_HOURS)

    def test_future_written_at_treated_as_stale(self) -> None:
        # A clock jump (or planted file) must not suppress probing forever.
        self.write_snapshot()
        earlier = _utcnow() - timedelta(hours=2)
        self.assertEqual(probe_state(self.repo, now=earlier), "stale")
        due, reason = probe_due(self.repo, now=earlier, env={})
        self.assertTrue(due)
        self.assertEqual(reason, "stale")

    def test_prompt_names_all_capabilities_and_writer(self) -> None:
        text = probe_prompt(self.repo, "/plugin/root")
        for name in ALL_CAPABILITIES:
            self.assertIn(name, text)
        self.assertIn("/bin/maw-run init.probe write", text)
        self.assertIn("/plugin/root", text)

    def test_prompt_has_no_dead_cron_param_or_malformed_call(self) -> None:
        # #90: the daily red "Invalid tool parameters" line came from a
        # deliberately-malformed PushNotification call; and CronCreate no longer
        # has delete_immediately (a one-shot is recurring: false).
        text = probe_prompt(self.repo, "/plugin/root")
        self.assertNotIn("delete_immediately", text)
        self.assertNotIn("malformed", text)
        self.assertIn("recurring: false", text)
        # push_notification is now a surface-presence probe, not a live call.
        self.assertIn("surface presence", text)

    def test_prompt_shell_quotes_paths_with_metacharacters(self) -> None:
        spaced = Path(self._tmp.name) / "my project (copy)"
        spaced.mkdir()
        text = probe_prompt(spaced, "/plug in/root; rm -rf x")
        self.assertIn(shlex.quote(str(spaced)), text)
        self.assertIn(shlex.quote("/plug in/root; rm -rf x/bin/maw-run"), text)
        # The display copy is repr-quoted so the path reads as data, not
        # as instruction text.
        self.assertIn(repr(str(spaced)), text)


class TestWriter(ProbeTestCase):
    def test_valid_payload_writes_v2_snapshot_and_log(self) -> None:
        payload = dict(ALL_PASS)
        payload["push_notification"] = {
            "probe_passed": False,
            "error": "tool not in surface",
        }
        rc = self.write_results(json.dumps(payload))
        self.assertEqual(rc, 0)

        registry = read_capability_state(self.repo)
        self.assertIsNotNone(registry)
        self.assertTrue(registry.results["cron_create"].probe_passed)
        self.assertFalse(registry.results["push_notification"].probe_passed)
        self.assertEqual(
            registry.results["push_notification"].error, "tool not in surface"
        )

        log_lines = (
            (self.repo / PROBE_LOG).read_text(encoding="utf-8").strip().splitlines()
        )
        self.assertEqual(len(log_lines), len(ALL_CAPABILITIES))

    def test_cleanup_error_kept_but_not_a_failure(self) -> None:
        payload = dict(ALL_PASS)
        payload["agent_teams"] = {
            "probe_passed": True,
            "cleanup_error": "TeamDelete timed out",
        }
        self.assertEqual(self.write_results(json.dumps(payload)), 0)
        registry = read_capability_state(self.repo)
        result = registry.results["agent_teams"]
        self.assertTrue(result.probe_passed)
        self.assertEqual(result.cleanup_error, "TeamDelete timed out")
        self.assertEqual(registry.failures(), [])

    def _assert_rejected(self, stdin_text: str) -> None:
        rc = self.write_results(stdin_text)
        self.assertEqual(rc, 1)
        self.assertFalse(self.runtime_path().exists(), "runtime file must be untouched")
        errors = (self.repo / PROBE_ERRORS).read_text(encoding="utf-8")
        self.assertTrue(errors.strip())

    def test_malformed_json_rejected(self) -> None:
        self._assert_rejected("not json at all")

    def test_missing_capability_rejected(self) -> None:
        payload = dict(ALL_PASS)
        del payload["remote_trigger"]
        self._assert_rejected(json.dumps(payload))

    def test_unknown_capability_rejected(self) -> None:
        payload = dict(ALL_PASS)
        payload["warp_drive"] = {"probe_passed": True}
        self._assert_rejected(json.dumps(payload))

    def test_non_object_result_rejected(self) -> None:
        payload = dict(ALL_PASS)
        payload["cron_create"] = "yes"
        self._assert_rejected(json.dumps(payload))

    def test_string_probe_passed_rejected(self) -> None:
        # bool("false") is True — a coercing write would record an
        # unavailable capability as available (Codex P2 #1).
        payload = dict(ALL_PASS)
        payload["cron_create"] = {"probe_passed": "false"}
        self._assert_rejected(json.dumps(payload))

    def test_string_smoke_passed_rejected(self) -> None:
        payload = dict(ALL_PASS)
        payload["cron_create"] = {"probe_passed": True, "smoke_passed": "ok"}
        self._assert_rejected(json.dumps(payload))

    def test_non_string_error_rejected(self) -> None:
        payload = dict(ALL_PASS)
        payload["cron_create"] = {"probe_passed": False, "error": {"msg": "x"}}
        self._assert_rejected(json.dumps(payload))

    def test_bad_payload_never_clobbers_good_snapshot(self) -> None:
        self.assertEqual(self.write_results(json.dumps(ALL_PASS)), 0)
        before = self.runtime_path().read_text(encoding="utf-8")
        rc = self.write_results("broken")
        self.assertEqual(rc, 1)
        self.assertEqual(self.runtime_path().read_text(encoding="utf-8"), before)


class TestBannerLine(ProbeTestCase):
    def test_missing_renders_deferred_probe_with_hint(self) -> None:
        line = capabilities_line(self.repo)
        self.assertIn("deferred-probe", line)
        self.assertIn("next session start", line)

    def test_fresh_renders_probed_state(self) -> None:
        payload = dict(ALL_PASS)
        payload["push_notification"] = {
            "probe_passed": False,
            "error": "tool not in surface",
        }
        self.write_results(json.dumps(payload))
        line = capabilities_line(self.repo)
        self.assertIn("cron_create=available", line)
        self.assertIn("push_notification=unavailable(tool not in surface)", line)
        self.assertNotIn("stale", line)

    def test_stale_renders_with_stale_suffix(self) -> None:
        self.write_results(json.dumps(ALL_PASS))
        later = _utcnow() + timedelta(hours=DEFAULT_TTL_HOURS + 1)
        line = capabilities_line(self.repo, now=later)
        self.assertIn("cron_create=available", line)
        self.assertIn("[stale; re-probes at next session start]", line)

    def test_malformed_renders_reprobe_notice(self) -> None:
        self.runtime_path().parent.mkdir(parents=True, exist_ok=True)
        self.runtime_path().write_text("{broken", encoding="utf-8")
        line = capabilities_line(self.repo)
        self.assertIn("malformed", line)
        self.assertIn("re-probe", line)

    def test_long_error_truncated(self) -> None:
        payload = dict(ALL_PASS)
        payload["cron_create"] = {"probe_passed": False, "error": "x" * 100}
        self.write_results(json.dumps(payload))
        line = capabilities_line(self.repo)
        self.assertIn("cron_create=unavailable(" + "x" * 37 + "...)", line)


class TestCliEntrypoint(ProbeTestCase):
    """One subprocess pass over the real argv surface the hook script uses."""

    def _run(self, args: list[str], stdin: str = "", env_extra: dict | None = None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MAW_")}
        env["PYTHONPATH"] = str(REPO_ROOT / "maw")
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "init.probe", *args],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_gate_then_write_then_silent(self) -> None:
        gate1 = self._run(
            ["gate", "--repo-root", str(self.repo), "--plugin-root", "/plug"]
        )
        self.assertEqual(gate1.returncode, 0)
        self.assertIn("Capability probe due", gate1.stdout)

        write = self._run(
            ["write", "--repo-root", str(self.repo)], stdin=json.dumps(ALL_PASS)
        )
        self.assertEqual(write.returncode, 0)

        gate2 = self._run(
            ["gate", "--repo-root", str(self.repo), "--plugin-root", "/plug"]
        )
        self.assertEqual(gate2.returncode, 0)
        self.assertEqual(gate2.stdout, "")


class TestHookSurface(unittest.TestCase):
    """The probe hook is declared in hooks.json and the script is executable."""

    def test_hook_registered_in_hooks_json(self) -> None:
        payload = json.loads(
            (REPO_ROOT / "maw" / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        commands = [
            hook["command"]
            for group in payload["hooks"]["SessionStart"]
            for hook in group["hooks"]
        ]
        self.assertTrue(
            any("probe-capabilities.sh" in c for c in commands),
            f"probe hook not registered; commands={commands}",
        )

    def test_hook_script_exists_and_executable(self) -> None:
        script = REPO_ROOT / "maw" / "hooks" / "probe-capabilities.sh"
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)

    def test_hook_exits_zero_when_plugin_root_unset(self) -> None:
        # The never-block guarantee must survive the abnormal case: a maw
        # repo (config present) but no CLAUDE_PLUGIN_ROOT in the env.
        script = REPO_ROOT / "maw" / "hooks" / "probe-capabilities.sh"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".maw").mkdir()
            (Path(tmp) / ".maw" / "config.yaml").write_text(
                "repo_slug: hook-test\n", encoding="utf-8"
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("CLAUDE_PLUGIN_ROOT",) and not k.startswith("MAW_")
            }
            result = subprocess.run(
                ["bash", str(script)],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
