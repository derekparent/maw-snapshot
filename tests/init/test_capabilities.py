"""Tests for init.capabilities — schema + registry + serialization."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from init.capabilities import (  # noqa: E402
    ALL_CAPABILITIES,
    V06_CAPABILITIES,
    V07_STUB_CAPABILITIES,
    CapabilityError,
    CapabilityRegistry,
    CapabilityResult,
    UnknownCapabilityError,
    read_capability_state,
    write_failures_file,
)


class SchemaTests(unittest.TestCase):
    def test_v06_capabilities_match_prd(self):
        # PRD §4.1.1: v0.6 narrows to 2 capabilities (cron_create, push_notification)
        self.assertEqual(V06_CAPABILITIES, ("cron_create", "push_notification"))

    def test_v07_stubs_match_prd(self):
        # PRD §4.1.1 + KD9: agent teams, managed agents, RemoteTrigger
        self.assertEqual(
            V07_STUB_CAPABILITIES,
            ("agent_teams", "managed_agents", "remote_trigger"),
        )

    def test_all_is_union(self):
        self.assertEqual(set(ALL_CAPABILITIES), set(V06_CAPABILITIES) | set(V07_STUB_CAPABILITIES))

    def test_no_overlap_between_v06_and_v07(self):
        self.assertEqual(set(V06_CAPABILITIES) & set(V07_STUB_CAPABILITIES), set())


class RegistryRecordTests(unittest.TestCase):
    def test_record_known_capability(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        self.assertIn("cron_create", reg.results)
        self.assertEqual(reg.results["cron_create"].probe_passed, True)
        self.assertIsNone(reg.results["cron_create"].smoke_passed)
        self.assertIsNotNone(reg.results["cron_create"].timestamp)

    def test_record_with_smoke(self):
        reg = CapabilityRegistry()
        reg.record("push_notification", probe_passed=True, smoke_passed=True)
        self.assertEqual(reg.results["push_notification"].smoke_passed, True)

    def test_record_failure_with_error(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False, error="tool not in deferred set")
        self.assertEqual(reg.results["cron_create"].probe_passed, False)
        self.assertEqual(reg.results["cron_create"].error, "tool not in deferred set")

    def test_record_unknown_raises(self):
        reg = CapabilityRegistry()
        with self.assertRaises(UnknownCapabilityError) as ctx:
            reg.record("totally_made_up", probe_passed=True)
        self.assertIn("totally_made_up", str(ctx.exception))

    def test_record_overwrites_previous(self):
        # Repeated init runs should record latest state, not append history
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False)
        reg.record("cron_create", probe_passed=True)
        self.assertEqual(reg.results["cron_create"].probe_passed, True)
        self.assertEqual(len(reg.results), 1)


class ConfigBlockTests(unittest.TestCase):
    def test_empty_registry_emits_all_false(self):
        # Default-false fail-closed posture
        reg = CapabilityRegistry()
        block = reg.to_config_block()
        self.assertEqual(
            block["capabilities"],
            {"cron_create": False, "push_notification": False},
        )
        self.assertEqual(
            block["capabilities_v07_stubs"],
            {"agent_teams": False, "managed_agents": False, "remote_trigger": False},
        )

    def test_partial_population(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        block = reg.to_config_block()
        self.assertEqual(block["capabilities"]["cron_create"], True)
        self.assertEqual(block["capabilities"]["push_notification"], False)

    def test_full_population_all_pass(self):
        reg = CapabilityRegistry()
        for name in ALL_CAPABILITIES:
            reg.record(name, probe_passed=True)
        block = reg.to_config_block()
        for value in block["capabilities"].values():
            self.assertTrue(value)
        for value in block["capabilities_v07_stubs"].values():
            self.assertTrue(value)

    def test_v06_keys_appear_first(self):
        reg = CapabilityRegistry()
        block = reg.to_config_block()
        # dict order matters for YAML field order
        self.assertEqual(
            list(block["capabilities"].keys()),
            list(V06_CAPABILITIES),
        )
        self.assertEqual(
            list(block["capabilities_v07_stubs"].keys()),
            list(V07_STUB_CAPABILITIES),
        )


class YamlTextTests(unittest.TestCase):
    def test_empty_registry_yaml(self):
        reg = CapabilityRegistry()
        text = reg.to_yaml_text()
        self.assertIn("capabilities:", text)
        self.assertIn("  cron_create: false", text)
        self.assertIn("  push_notification: false", text)
        self.assertIn("capabilities_v07_stubs:", text)
        self.assertIn("  agent_teams: false", text)
        self.assertIn("  managed_agents: false", text)
        self.assertIn("  remote_trigger: false", text)

    def test_yaml_uses_lowercase_booleans(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        text = reg.to_yaml_text()
        self.assertIn("cron_create: true", text)
        self.assertNotIn("True", text)
        self.assertNotIn("False", text)

    def test_yaml_renders_v07_comment(self):
        reg = CapabilityRegistry()
        text = reg.to_yaml_text()
        self.assertIn("# v0.7-ready stubs", text)
        self.assertIn("KD9", text)

    def test_yaml_field_order_stable(self):
        reg = CapabilityRegistry()
        reg.record("push_notification", probe_passed=True)  # record in non-PRD order
        reg.record("cron_create", probe_passed=False)
        text = reg.to_yaml_text()
        # cron_create appears before push_notification regardless of record order
        cron_idx = text.index("cron_create")
        push_idx = text.index("push_notification")
        self.assertLess(cron_idx, push_idx)


class FailuresTests(unittest.TestCase):
    def test_no_failures_when_clean(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        reg.record("push_notification", probe_passed=True, smoke_passed=True)
        self.assertEqual(reg.failures(), [])

    def test_probe_failure_listed(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False)
        failures = reg.failures()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].name, "cron_create")

    def test_smoke_failure_listed(self):
        reg = CapabilityRegistry()
        reg.record("push_notification", probe_passed=True, smoke_passed=False)
        failures = reg.failures()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].name, "push_notification")

    def test_smoke_skipped_is_not_a_failure(self):
        # smoke_passed=None means smoke wasn't requested (e.g., --validate not passed)
        # That's not a failure — distinguishes "didn't try" from "tried and failed"
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True, smoke_passed=None)
        self.assertEqual(reg.failures(), [])

    def test_unprobed_capability_not_in_failures(self):
        # An unprobed capability defaults to false in the block but doesn't
        # appear as a "failure" entry — there's no error to surface, just absence
        reg = CapabilityRegistry()
        self.assertEqual(reg.failures(), [])


class WriteFailuresFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".maw" / ".runtime" / "capability-failures.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_file(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False, error="not in deferred set")
        write_failures_file(self.path, reg)
        self.assertTrue(self.path.is_file())

    def test_creates_runtime_dir_with_0700(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False)
        write_failures_file(self.path, reg)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_reasserts_0700_on_preexisting_looser_dir(self):
        # mkdir(mode=..., exist_ok=True) ignores the mode on an existing
        # dir; a runtime dir left at 0o755 by an earlier run must be
        # tightened, not silently accepted (WS4 init I2).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o755)
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False)
        write_failures_file(self.path, reg)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_atomic_write_leaves_no_partial_and_roundtrips(self):
        # The write goes through write_atomic_text (temp+fsync+rename), so
        # no temp file is left behind and the result is valid JSON.
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        write_failures_file(self.path, reg)
        siblings = list(self.path.parent.iterdir())
        self.assertEqual([p.name for p in siblings], [self.path.name])
        json.loads(self.path.read_text(encoding="utf-8"))  # parses clean

    def test_file_perms_0600(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False)
        write_failures_file(self.path, reg)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_payload_shape(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=False, error="not in deferred set")
        write_failures_file(self.path, reg)
        payload = json.loads(self.path.read_text())
        self.assertIn("written_at", payload)
        self.assertIn("failures", payload)
        self.assertEqual(len(payload["failures"]), 1)
        self.assertEqual(payload["failures"][0]["name"], "cron_create")
        self.assertEqual(payload["failures"][0]["error"], "not in deferred set")

    def test_no_failures_writes_empty_list(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        write_failures_file(self.path, reg)
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["failures"], [])

    def test_overwrites_existing_file(self):
        reg1 = CapabilityRegistry()
        reg1.record("cron_create", probe_passed=False)
        write_failures_file(self.path, reg1)

        reg2 = CapabilityRegistry()  # all-clean
        reg2.record("cron_create", probe_passed=True)
        write_failures_file(self.path, reg2)

        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["failures"], [])


class CapabilityResultTests(unittest.TestCase):
    """Direct tests on the dataclass shape so future refactors don't drift."""

    def test_construct_minimal(self):
        r = CapabilityResult(name="cron_create", probe_passed=True)
        self.assertEqual(r.name, "cron_create")
        self.assertTrue(r.probe_passed)
        self.assertIsNone(r.smoke_passed)
        self.assertIsNone(r.error)
        self.assertIsNone(r.timestamp)

    def test_construct_full(self):
        r = CapabilityResult(
            name="push_notification",
            probe_passed=True,
            smoke_passed=False,
            error="timeout after 1s",
            timestamp="2026-05-09T05:00:00Z",
        )
        self.assertFalse(r.smoke_passed)
        self.assertEqual(r.error, "timeout after 1s")

    def test_frozen(self):
        # Probe results are immutable; updates go through new records
        r = CapabilityResult(name="cron_create", probe_passed=True)
        with self.assertRaises(Exception):
            r.probe_passed = False  # type: ignore[misc]

    def test_cleanup_error_defaults_none(self):
        r = CapabilityResult(name="cron_create", probe_passed=True)
        self.assertIsNone(r.cleanup_error)


class SchemaV2WriteTests(unittest.TestCase):
    """v2 write shape (PRD §3.2.4): schema_version + results + failures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".maw" / ".runtime" / "capability-failures.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _payload(self, reg):
        write_failures_file(self.path, reg)
        return json.loads(self.path.read_text())

    def test_emits_schema_version_2(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        self.assertEqual(self._payload(reg)["schema_version"], 2)

    def test_emits_full_results_map(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        reg.record("push_notification", probe_passed=False, error="not found")
        payload = self._payload(reg)
        self.assertIn("cron_create", payload["results"])
        self.assertIn("push_notification", payload["results"])
        # Passing capability is in results but NOT in failures.
        self.assertTrue(payload["results"]["cron_create"]["probe_passed"])
        names_in_failures = {f["name"] for f in payload["failures"]}
        self.assertNotIn("cron_create", names_in_failures)
        self.assertIn("push_notification", names_in_failures)

    def test_cleanup_error_serialized(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True, cleanup_error="CronDelete failed")
        payload = self._payload(reg)
        self.assertEqual(payload["results"]["cron_create"]["cleanup_error"], "CronDelete failed")

    def test_cleanup_error_not_a_failure(self):
        # Parent passed; cleanup failed → still NOT counted as a failure.
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True, cleanup_error="CronDelete failed")
        self.assertEqual(reg.failures(), [])
        self.assertEqual(self._payload(reg)["failures"], [])


class ReadCapabilityStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.path = self.repo / ".maw" / ".runtime" / "capability-failures.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_capability_state(self.repo))

    def test_v2_roundtrip(self):
        reg = CapabilityRegistry()
        reg.record("cron_create", probe_passed=True)
        reg.record("push_notification", probe_passed=False, error="tool not found")
        write_failures_file(self.path, reg)

        loaded = read_capability_state(self.repo)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.results["cron_create"].probe_passed, True)
        self.assertEqual(loaded.results["push_notification"].probe_passed, False)
        self.assertEqual(loaded.results["push_notification"].error, "tool not found")

    def test_v1_inference_from_absent_schema_version(self):
        # v1 shape: NO schema_version, failures-only.
        self._write(
            {
                "written_at": "2026-05-13T00:00:00Z",
                "failures": [
                    {
                        "name": "push_notification",
                        "probe_passed": False,
                        "smoke_passed": None,
                        "error": "tool not found",
                        "timestamp": "2026-05-13T00:00:00Z",
                    }
                ],
            }
        )
        loaded = read_capability_state(self.repo)
        self.assertIsNotNone(loaded)
        self.assertIn("push_notification", loaded.results)
        self.assertFalse(loaded.results["push_notification"].probe_passed)
        # v1 had no cleanup_error → reconstructed as None.
        self.assertIsNone(loaded.results["push_notification"].cleanup_error)

    def test_v1_empty_failures(self):
        self._write({"written_at": "2026-05-13T00:00:00Z", "failures": []})
        loaded = read_capability_state(self.repo)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.results, {})

    def test_malformed_json_raises(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid", encoding="utf-8")
        with self.assertRaises(CapabilityError):
            read_capability_state(self.repo)

    def test_non_object_root_raises(self):
        self._write([1, 2, 3])
        with self.assertRaises(CapabilityError):
            read_capability_state(self.repo)

    def test_unknown_schema_version_raises(self):
        self._write({"schema_version": 99, "written_at": "x", "results": {}})
        with self.assertRaises(CapabilityError):
            read_capability_state(self.repo)

    def test_explicit_schema_version_1_is_unsupported(self):
        # v1 is identified by ABSENT schema_version; an explicit 1 is anomalous.
        self._write({"schema_version": 1, "failures": []})
        with self.assertRaises(CapabilityError):
            read_capability_state(self.repo)

    def test_v2_missing_probe_passed_raises(self):
        self._write(
            {"schema_version": 2, "results": {"cron_create": {"error": "x"}}}
        )
        with self.assertRaises(CapabilityError):
            read_capability_state(self.repo)


if __name__ == "__main__":
    unittest.main()
