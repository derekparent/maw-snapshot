"""Tests for init.substitution — {{var}} resolver + profile-template smoke."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from init.substitution import (  # noqa: E402
    PLACEHOLDER_RE,
    UnresolvedPlaceholderError,
    find_placeholders,
    substitute,
    substitute_file,
)

PROFILES_DIR = Path(__file__).resolve().parents[2] / "maw" / "init" / "templates" / "profiles"


class SubstituteTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(substitute("hello {{name}}", {"name": "world"}), "hello world")

    def test_multiple_placeholders(self):
        self.assertEqual(
            substitute("{{a}} + {{b}} = {{a}}{{b}}", {"a": "1", "b": "2"}),
            "1 + 2 = 12",
        )

    def test_no_placeholders_unchanged(self):
        self.assertEqual(substitute("hello world", {}), "hello world")

    def test_empty_template(self):
        self.assertEqual(substitute("", {"x": "y"}), "")

    def test_unknown_placeholder_raises(self):
        with self.assertRaises(UnresolvedPlaceholderError) as ctx:
            substitute("hello {{name}}", {})
        self.assertIn("name", str(ctx.exception))
        self.assertIn("{{name}}", str(ctx.exception))

    def test_unknown_placeholder_lists_all(self):
        with self.assertRaises(UnresolvedPlaceholderError) as ctx:
            substitute("{{a}} and {{b}} and {{c}}", {"b": "2"})
        msg = str(ctx.exception)
        self.assertIn("{{a}}", msg)
        self.assertIn("{{c}}", msg)
        # Resolved placeholder not listed
        self.assertNotIn(", {{b}},", msg)

    def test_extra_vars_ignored(self):
        self.assertEqual(
            substitute("hello {{name}}", {"name": "x", "unused": "y"}),
            "hello x",
        )

    def test_placeholder_at_start(self):
        self.assertEqual(substitute("{{x}}!", {"x": "go"}), "go!")

    def test_placeholder_at_end(self):
        self.assertEqual(substitute("hi {{x}}", {"x": "you"}), "hi you")

    def test_adjacent_placeholders(self):
        self.assertEqual(
            substitute("{{a}}{{b}}{{c}}", {"a": "1", "b": "2", "c": "3"}),
            "123",
        )

    def test_value_with_special_chars(self):
        # Path-like value with /, dots, dashes
        self.assertEqual(
            substitute("path: {{p}}", {"p": "/home/user/Projects/maw"}),
            "path: /home/user/Projects/maw",
        )

    def test_value_with_curly_braces_does_not_re_substitute(self):
        # If a value happens to contain {{x}}, it MUST NOT be re-substituted —
        # that would let attacker-controlled values inject placeholders.
        self.assertEqual(
            substitute("{{a}} done", {"a": "{{never_resolved}}"}),
            "{{never_resolved}} done",
        )

    def test_value_with_newlines(self):
        self.assertEqual(
            substitute("body:\n{{b}}\nend", {"b": "line1\nline2"}),
            "body:\nline1\nline2\nend",
        )

    def test_invalid_placeholder_syntax_left_alone(self):
        # `{{ name }}` (with spaces) does NOT match the regex; it's literal text.
        self.assertEqual(substitute("{{ name }}", {"name": "x"}), "{{ name }}")
        # `{{1bad}}` (digit-leading identifier) does not match.
        self.assertEqual(substitute("{{1bad}}", {"1bad": "x"}), "{{1bad}}")


class SubstituteFileTests(unittest.TestCase):
    def test_substitute_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("hello {{name}}")
            tmp = Path(f.name)
        try:
            self.assertEqual(
                substitute_file(tmp, {"name": "world"}),
                "hello world",
            )
        finally:
            tmp.unlink()


class FindPlaceholdersTests(unittest.TestCase):
    def test_finds_all(self):
        self.assertEqual(
            find_placeholders("{{a}} and {{b}} and {{a}}"),
            {"a", "b"},
        )

    def test_empty_when_none(self):
        self.assertEqual(find_placeholders("plain text"), set())


class ProfileTemplateSmokeTests(unittest.TestCase):
    """Smoke tests that the 4 profile templates render with realistic vars."""

    PROFILES = (
        "python-flask-railway",
        "python-fastapi-railway",
        "node-next-vercel",
        "generic",
    )

    SAMPLE_VARS = {
        "plugin_version": "0.6.0",
        "repo_id": "00000000-0000-4000-a000-000000000001",
        "repo_slug": "tracker-app-a3f8c102",
        "deploy_url": "https://example.up.railway.app",
    }

    def test_all_profiles_present(self):
        for name in self.PROFILES:
            path = PROFILES_DIR / f"{name}.yaml"
            self.assertTrue(path.is_file(), f"missing profile template: {name}")

    def test_all_profiles_render(self):
        for name in self.PROFILES:
            path = PROFILES_DIR / f"{name}.yaml"
            rendered = substitute_file(path, self.SAMPLE_VARS)
            self.assertNotIn("{{", rendered, f"unresolved placeholder in {name}")
            self.assertIn("schema_version: 1", rendered)
            self.assertIn(f'profile: "{name}"', rendered)

    def test_profile_placeholders_are_subset_of_sample_vars(self):
        # SAMPLE_VARS must cover every placeholder used by every profile,
        # so /maw:init can pre-collect the variables before rendering.
        sample_keys = set(self.SAMPLE_VARS.keys())
        for name in self.PROFILES:
            path = PROFILES_DIR / f"{name}.yaml"
            template = path.read_text(encoding="utf-8")
            placeholders = find_placeholders(template)
            missing = placeholders - sample_keys
            self.assertEqual(
                missing, set(),
                f"profile {name} uses placeholders not in SAMPLE_VARS: {missing}",
            )

    def test_repo_slug_resolves_in_bridge_dir(self):
        # Specifically verify the bridge.dir composition works end-to-end —
        # the slug appears exactly where the file format module expects it.
        path = PROFILES_DIR / "python-flask-railway.yaml"
        rendered = substitute_file(path, self.SAMPLE_VARS)
        self.assertIn(
            'dir: "~/.claude/bridge/tracker-app-a3f8c102"',
            rendered,
        )
        self.assertIn('repo_slug: "tracker-app-a3f8c102"', rendered)


if __name__ == "__main__":
    unittest.main()
