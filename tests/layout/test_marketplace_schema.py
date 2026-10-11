"""Layout smoke: marketplace.json parses and declares the expected plugin set.

Per PRD §11.13 — Layout/manifest smoke tests (v0.6 must-land after the 2026-05-10
test-signal-gap leak).

This test pair sits at the marketplace layer (covering the marketplace.json file
itself), complementing test_plugin_manifest.py which sits at the per-plugin layer.

Stdlib-only by design.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

EXPECTED_PLUGINS = ("maw",)
REQUIRED_MARKETPLACE_FIELDS = ("$schema", "name", "description", "owner", "plugins")


class MarketplaceSchemaTests(unittest.TestCase):

    def setUp(self) -> None:
        if not MARKETPLACE_JSON.is_file():
            self.skipTest(f"{MARKETPLACE_JSON} not found — repo layout precondition not met")
        with MARKETPLACE_JSON.open() as f:
            try:
                self.marketplace = json.load(f)
            except json.JSONDecodeError as e:
                self.fail(f"marketplace.json is not valid JSON: {e}")

    def test_marketplace_has_required_fields(self) -> None:
        for field in REQUIRED_MARKETPLACE_FIELDS:
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    self.marketplace,
                    f"marketplace.json missing required field '{field}'",
                )

    def test_marketplace_name(self) -> None:
        self.assertEqual(
            self.marketplace.get("name"),
            "maw",
            "marketplace.json name must be 'maw' (referenced as `@maw` in install commands)",
        )

    def test_owner_has_name(self) -> None:
        owner = self.marketplace.get("owner", {})
        self.assertIsInstance(owner, dict, "owner must be an object")
        self.assertIn("name", owner, "owner must have a name field")
        self.assertTrue(owner["name"], "owner.name must be non-empty")

    def test_plugins_is_a_list(self) -> None:
        self.assertIsInstance(
            self.marketplace.get("plugins"),
            list,
            "marketplace.plugins must be a list",
        )

    def test_every_declared_plugin_source_exists(self) -> None:
        for p in self.marketplace.get("plugins", []):
            if isinstance(p, dict) and isinstance(p.get("source"), str):
                with self.subTest(plugin=p.get("name")):
                    self.assertTrue((REPO_ROOT / p["source"]).is_dir(), p["source"])

    def test_declared_plugins_include_expected_set(self) -> None:
        """The umbrella maw plugin must be declared. Other entries are optional
        (the release export drops the unshipped maw-bridge one); any entry that
        IS declared must point at a source dir that exists (test below)."""
        plugins = self.marketplace.get("plugins", [])
        declared_names = {p.get("name") for p in plugins if isinstance(p, dict)}
        for expected in EXPECTED_PLUGINS:
            with self.subTest(plugin=expected):
                self.assertIn(
                    expected,
                    declared_names,
                    f"marketplace.json missing expected plugin '{expected}' "
                    f"(declared: {sorted(declared_names)})",
                )

    def test_no_duplicate_plugin_names(self) -> None:
        plugins = self.marketplace.get("plugins", [])
        names = [p.get("name") for p in plugins if isinstance(p, dict)]
        self.assertEqual(
            len(names),
            len(set(names)),
            f"marketplace.json has duplicate plugin names: {names}",
        )

    def test_schema_url_matches_expected(self) -> None:
        """Defensive: catch accidental edits to the $schema URL.

        If the schema URL drifts, the marketplace may pass validation against
        an unintended schema. Lock in the v0.6 expected URL.
        """
        expected = "https://anthropic.com/claude-code/marketplace.schema.json"
        self.assertEqual(
            self.marketplace.get("$schema"),
            expected,
            f"marketplace $schema URL drifted from '{expected}'",
        )


if __name__ == "__main__":
    unittest.main()
