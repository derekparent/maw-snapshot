"""Layout smoke: each plugin declared in marketplace.json resolves to a parseable plugin.json.

Per PRD §11.13 — Layout/manifest smoke tests (v0.6 must-land after the 2026-05-10
test-signal-gap leak). The 198-test Python suite is blind to the plugin loader's
actual surface (manifest location, marketplace entry shape, source-path resolution);
these tests cover that class of failure.

For each plugin declared in .claude-plugin/marketplace.json:
- The marketplace entry has the required fields.
- A bare-string source matches the marketplace schema rule ^\\./.* (the failure
  mode that bit us on 2026-05-10: `source: "."` was rejected by the validator).
- The path resolved by source is a directory that exists.
- That directory contains .claude-plugin/plugin.json.
- The plugin.json parses as JSON and declares a name matching the marketplace entry.

Stdlib-only by design (no jsonschema dep) per the maw doctrine.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# Marketplace schema requires bare-string source to match this pattern.
# (Discovered 2026-05-10: `source: "."` was rejected because it doesn't match.)
SOURCE_PATTERN = re.compile(r"^\./.*")

REQUIRED_MARKETPLACE_ENTRY_FIELDS = ("name", "description", "source", "category")

# The public home of the snapshot. Both manifests name it; a homepage that does
# not parse as a URL stops the plugin from loading at all. The owner is not
# spelled here: the manifests are the only files that carry the author's name.
SNAPSHOT_HOMEPAGE = re.compile(r"^https://github\.com/[A-Za-z0-9-]+/maw-snapshot$")
# Plugins this marketplace declares but the release export leaves out.
UNSHIPPED_PLUGINS = ("maw-bridge",)


class PluginManifestLayoutTests(unittest.TestCase):
    """One test per declared plugin — parametrized via subtests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.assertTrue_marketplace_exists(cls)
        with MARKETPLACE_JSON.open() as f:
            cls.marketplace = json.load(f)
        cls.plugins = cls.marketplace.get("plugins", [])

    def assertTrue_marketplace_exists(cls) -> None:
        if not MARKETPLACE_JSON.is_file():
            raise unittest.SkipTest(
                f"{MARKETPLACE_JSON} not found — repo layout precondition not met"
            )

    def test_marketplace_declares_at_least_one_plugin(self) -> None:
        self.assertGreater(
            len(self.plugins),
            0,
            "marketplace.json must declare at least one plugin",
        )

    def test_each_plugin_has_required_marketplace_fields(self) -> None:
        for entry in self.plugins:
            name = entry.get("name", "<unnamed>")
            with self.subTest(plugin=name):
                for field in REQUIRED_MARKETPLACE_ENTRY_FIELDS:
                    self.assertIn(
                        field,
                        entry,
                        f"plugin '{name}' missing required marketplace field '{field}'",
                    )

    def test_each_plugin_source_matches_schema_pattern(self) -> None:
        """Bare-string sources must match ^\\./.* per marketplace schema.

        This is the rule that bit us on 2026-05-10 when the umbrella entry
        used source: "." (which doesn't match) and CC rejected the install
        with a misleading "source type not supported" error.
        """
        for entry in self.plugins:
            name = entry.get("name", "<unnamed>")
            source = entry.get("source")
            with self.subTest(plugin=name):
                if isinstance(source, str):
                    self.assertRegex(
                        source,
                        SOURCE_PATTERN,
                        f"plugin '{name}' source '{source}' does not match ^\\./.* "
                        f"(marketplace schema requires bare-string sources to start with './')",
                    )
                else:
                    # Object-form sources (git-subdir, github, npm, url) are out
                    # of scope for this layout test — they don't require local
                    # filesystem resolution. Skip with a note.
                    self.skipTest(
                        f"plugin '{name}' uses object-form source; "
                        f"bare-string-only rule does not apply"
                    )

    def test_each_plugin_source_resolves_to_existing_directory(self) -> None:
        """The path named by source must exist as a directory in the repo."""
        for entry in self.plugins:
            name = entry.get("name", "<unnamed>")
            source = entry.get("source")
            with self.subTest(plugin=name):
                if not isinstance(source, str):
                    self.skipTest(f"plugin '{name}' uses object-form source")
                resolved = (REPO_ROOT / source).resolve()
                self.assertTrue(
                    resolved.is_dir(),
                    f"plugin '{name}' source '{source}' does not resolve to a "
                    f"directory (resolved: {resolved})",
                )

    def test_each_plugin_dir_contains_canonical_manifest(self) -> None:
        """source/.claude-plugin/plugin.json must exist for each declared plugin."""
        for entry in self.plugins:
            name = entry.get("name", "<unnamed>")
            source = entry.get("source")
            with self.subTest(plugin=name):
                if not isinstance(source, str):
                    self.skipTest(f"plugin '{name}' uses object-form source")
                plugin_json = REPO_ROOT / source / ".claude-plugin" / "plugin.json"
                self.assertTrue(
                    plugin_json.is_file(),
                    f"plugin '{name}' missing canonical manifest at "
                    f"{plugin_json.relative_to(REPO_ROOT)}",
                )

    def test_each_plugin_manifest_parses_and_name_matches(self) -> None:
        """plugin.json must parse as JSON and declare a name matching the marketplace entry."""
        for entry in self.plugins:
            name = entry.get("name", "<unnamed>")
            source = entry.get("source")
            with self.subTest(plugin=name):
                if not isinstance(source, str):
                    self.skipTest(f"plugin '{name}' uses object-form source")
                plugin_json = REPO_ROOT / source / ".claude-plugin" / "plugin.json"
                if not plugin_json.is_file():
                    self.skipTest(f"plugin.json missing — covered by earlier test")
                try:
                    with plugin_json.open() as f:
                        manifest = json.load(f)
                except json.JSONDecodeError as e:
                    self.fail(
                        f"plugin '{name}' manifest at {plugin_json.relative_to(REPO_ROOT)} "
                        f"is not valid JSON: {e}"
                    )
                self.assertEqual(
                    manifest.get("name"),
                    name,
                    f"plugin '{name}' manifest declares name='{manifest.get('name')}', "
                    f"does not match marketplace entry name='{name}'",
                )


class ShippedManifestFactsTests(unittest.TestCase):
    """What a reader of the snapshot sees in the two manifests."""

    @classmethod
    def setUpClass(cls) -> None:
        if not MARKETPLACE_JSON.is_file():
            raise unittest.SkipTest(f"{MARKETPLACE_JSON} not found")
        cls.marketplace = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
        cls.entry = next(e for e in cls.marketplace["plugins"] if e.get("name") == "maw")
        cls.manifest = json.loads(
            (REPO_ROOT / cls.entry["source"] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    def test_the_version_is_a_plain_release_number(self) -> None:
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")
        # plugin.json wins over a marketplace version; a second copy could only disagree.
        self.assertNotIn("version", self.entry)

    def test_both_manifests_name_the_snapshot_as_homepage(self) -> None:
        self.assertRegex(self.manifest["homepage"], SNAPSHOT_HOMEPAGE)
        self.assertEqual(self.entry["homepage"], self.manifest["homepage"])

    def test_the_marketplace_description_names_no_unshipped_plugin(self) -> None:
        """The export drops the unshipped entries; the description must still be true."""
        for name in UNSHIPPED_PLUGINS:
            self.assertNotIn(name, self.marketplace["description"])

    def test_the_two_descriptions_of_the_plugin_agree(self) -> None:
        self.assertEqual(self.entry["description"], self.manifest["description"])


if __name__ == "__main__":
    unittest.main()
