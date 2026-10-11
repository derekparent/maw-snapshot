"""Tests for substrate template transplant (Task D).

Verifies that each profile YAML lists template file paths that actually
exist on disk under maw/init/templates/, and that every listed template
file renders cleanly through substitution.py with a merged sample
variables dict (no UnresolvedPlaceholderError).

Stdlib-only. The profile YAMLs are read as text and parsed by a tiny
line-based helper rather than depending on PyYAML; the YAML shape used
here (top-level keys with `- string` list items and simple `key: "value"`
dict items) is well within the substrate doctrine of zero-dep hook
tooling.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from init.substitution import (  # noqa: E402
    UnresolvedPlaceholderError,
    find_placeholders,
    substitute,
    substitute_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "maw" / "init" / "templates"
PROFILES_DIR = TEMPLATES_DIR / "profiles"

PROFILES = (
    "generic",
    "python-flask-railway",
    "python-fastapi-railway",
    "node-next-vercel",
)

# Base vars supplied by /maw:init that are NOT profile-local. The profile's
# template_vars is merged on top to form the full substitution dict.
BASE_VARS = {
    "plugin_version": "0.6.0",
    "repo_id": "00000000-0000-4000-a000-000000000001",
    "repo_slug": "example-repo-a3f8c102",
    "deploy_url": "https://example.up.railway.app",
}


def _read_yaml_list(text: str, key: str) -> list[str]:
    """Extract a top-level `<key>:` block whose body is `- item` lines.

    Returns the list of item strings (whitespace-stripped, surrounding
    quotes removed). Returns [] if the key is absent. Tolerant of comment
    lines (`#`) within the block. Stops at the next top-level key.
    """
    pattern = re.compile(
        rf"(?m)^{re.escape(key)}:\s*\n((?:[ \t]+.*\n|[ \t]*#.*\n|\s*\n)+)"
    )
    match = pattern.search(text)
    if not match:
        return []
    items: list[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            value = stripped[2:].strip()
            # Strip surrounding quotes if present.
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            items.append(value)
    return items


def _read_yaml_string_dict(text: str, key: str) -> dict[str, str]:
    """Extract a top-level `<key>:` block whose body is `name: "value"` lines.

    Returns the dict. Returns {} if the key is absent. Only string-valued
    entries are supported; anything more complex is out of scope.
    """
    pattern = re.compile(
        rf"(?m)^{re.escape(key)}:\s*\n((?:[ \t]+.*\n|[ \t]*#.*\n|\s*\n)+)"
    )
    match = pattern.search(text)
    if not match:
        return {}
    result: dict[str, str] = {}
    entry_re = re.compile(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*"([^"]*)"\s*$')
    for line in match.group(1).splitlines():
        m = entry_re.match(line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


class ProfileBlocksPresentTests(unittest.TestCase):
    """Each profile YAML must declare hooks/git_hooks/schemas/template_vars."""

    def _read_profile(self, name: str) -> str:
        path = PROFILES_DIR / f"{name}.yaml"
        self.assertTrue(path.is_file(), f"profile YAML missing: {name}.yaml")
        return path.read_text(encoding="utf-8")

    def test_all_profiles_declare_hooks_block(self):
        # `hooks:` may now be declared empty (`hooks: []`, opt-in model) or
        # as a block with items — either way the key itself must be present.
        for name in PROFILES:
            text = self._read_profile(name)
            self.assertRegex(
                text, r"(?m)^hooks:",
                f"profile {name} missing top-level `hooks:` key",
            )

    def test_all_profiles_declare_git_hooks_block(self):
        for name in PROFILES:
            text = self._read_profile(name)
            self.assertIn(
                "\ngit_hooks:\n", text,
                f"profile {name} missing top-level `git_hooks:` block",
            )

    def test_all_profiles_declare_schemas_block(self):
        for name in PROFILES:
            text = self._read_profile(name)
            self.assertIn(
                "\nschemas:\n", text,
                f"profile {name} missing top-level `schemas:` block",
            )

    def test_all_profiles_declare_template_vars(self):
        for name in PROFILES:
            text = self._read_profile(name)
            tv = _read_yaml_string_dict(text, "template_vars")
            self.assertGreater(
                len(tv), 0,
                f"profile {name} declares no template_vars entries",
            )
            # Sanity: the substrate's known per-repo vars must be present.
            for key in ("budget_soft", "budget_hard", "deploy_branch",
                        "alias_suggestion", "live_sweep_cred_const"):
                self.assertIn(
                    key, tv,
                    f"profile {name} template_vars missing {key!r}",
                )


class ListedTemplatesExistTests(unittest.TestCase):
    """Every path a profile lists must exist on disk under templates/."""

    def test_all_listed_files_exist(self):
        missing: list[str] = []
        for name in PROFILES:
            text = (PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8")
            for block in ("hooks", "git_hooks", "schemas"):
                for rel in _read_yaml_list(text, block):
                    full = TEMPLATES_DIR / rel
                    if not full.is_file():
                        missing.append(f"{name}/{block}/{rel}")
        self.assertEqual(
            missing, [],
            f"profile YAMLs reference template files that don't exist: {missing}",
        )

    def test_hooks_may_be_empty_but_listed_items_exist(self):
        # Repo-local hook scripts are opt-in now (profiles default to
        # `hooks: []`); an empty list is valid. Whatever a profile DOES
        # list must still resolve to a real template file on disk — that
        # part of the old rule survives.
        for name in PROFILES:
            text = (PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8")
            hooks = _read_yaml_list(text, "hooks")
            for rel in hooks:
                full = TEMPLATES_DIR / rel
                self.assertTrue(
                    full.is_file(),
                    f"profile {name} hooks list references missing file {rel}",
                )

    def test_pre_push_listed_per_profile(self):
        for name in PROFILES:
            text = (PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8")
            git_hooks = _read_yaml_list(text, "git_hooks")
            self.assertIn(
                "git-hooks/pre-push", git_hooks,
                f"profile {name} must include git-hooks/pre-push (PRD §11.4 default-deny gate)",
            )

    def test_handoff_schema_listed_per_profile(self):
        for name in PROFILES:
            text = (PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8")
            schemas = _read_yaml_list(text, "schemas")
            self.assertIn(
                "schemas/handoff.json", schemas,
                f"profile {name} must include schemas/handoff.json (PRD §3.3)",
            )


class TemplateSubstitutionTests(unittest.TestCase):
    """Every listed template renders cleanly with merged vars (no UnresolvedPlaceholderError)."""

    def _merged_vars(self, profile_name: str) -> dict[str, str]:
        text = (PROFILES_DIR / f"{profile_name}.yaml").read_text(encoding="utf-8")
        tv = _read_yaml_string_dict(text, "template_vars")
        merged = dict(BASE_VARS)
        merged.update(tv)
        return merged

    def test_all_listed_templates_substitute_cleanly(self):
        for name in PROFILES:
            text = (PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8")
            vars_for_profile = self._merged_vars(name)
            for block in ("hooks", "git_hooks", "schemas"):
                for rel in _read_yaml_list(text, block):
                    full = TEMPLATES_DIR / rel
                    try:
                        rendered = substitute_file(full, vars_for_profile)
                    except UnresolvedPlaceholderError as exc:
                        self.fail(
                            f"profile {name} template {rel} failed substitution: {exc}\n"
                            f"  vars supplied: {sorted(vars_for_profile)}"
                        )
                    # Sanity: no `{{ident}}` remaining (whitespace-strict, so
                    # `{{ x }}` is legitimate non-placeholder text and ignored).
                    leftover = re.findall(r"\{\{[a-zA-Z_][a-zA-Z0-9_]*\}\}", rendered)
                    self.assertEqual(
                        leftover, [],
                        f"profile {name} template {rel} has leftover placeholders: {leftover}",
                    )

    def test_template_vars_cover_all_placeholders(self):
        """Each profile's BASE_VARS+template_vars must cover every placeholder
        in every template it lists. Catches authoring errors where a new
        placeholder is added to a template without a corresponding default."""
        for name in PROFILES:
            text = (PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8")
            vars_for_profile = self._merged_vars(name)
            supplied = set(vars_for_profile.keys())
            for block in ("hooks", "git_hooks", "schemas"):
                for rel in _read_yaml_list(text, block):
                    full = TEMPLATES_DIR / rel
                    needed = find_placeholders(full.read_text(encoding="utf-8"))
                    missing = needed - supplied
                    self.assertEqual(
                        missing, set(),
                        f"profile {name} template {rel} needs placeholders {missing} "
                        f"that neither BASE_VARS nor template_vars supply",
                    )


class TemplateContentSanityTests(unittest.TestCase):
    """Spot-check substituted content for invariants the substrate depends on."""

    def test_pre_push_renders_protected_branch_match(self):
        path = TEMPLATES_DIR / "git-hooks" / "pre-push"
        rendered = substitute_file(path, {"deploy_branch": "main", "alias_suggestion": "mw"})
        self.assertIn("refs/heads/main", rendered)
        self.assertIn("refs/heads/master", rendered)  # legacy co-target literal
        self.assertIn("'mw'", rendered)

    def test_budget_guard_renders_as_valid_python(self):
        path = TEMPLATES_DIR / "hooks" / "budget-guard.py"
        rendered = substitute_file(path, {"budget_soft": "600", "budget_hard": "1500"})
        self.assertIn("SOFT_LIMIT = 600", rendered)
        self.assertIn("HARD_LIMIT = 1500", rendered)
        # Compile-check: rendered file must be syntactically valid Python.
        compile(rendered, str(path), "exec")

    def test_handoff_schema_renders_as_valid_json(self):
        import json
        path = TEMPLATES_DIR / "schemas" / "handoff.json"
        rendered = substitute_file(path, {
            "repo_slug": "example-repo-a3f8c102",
            "live_sweep_cred_const": "env:MY_USER,MY_PASS",
        })
        # Parses as JSON and the substituted bits land where expected.
        parsed = json.loads(rendered)
        self.assertEqual(
            parsed["$id"],
            "https://example-repo-a3f8c102.local/schemas/handoff.json",
        )
        cred = parsed["$defs"]["live_sweep_launch"]["properties"]["credentials_source"]["const"]
        self.assertEqual(cred, "env:MY_USER,MY_PASS")

    def test_validate_launch_renders_as_valid_python(self):
        path = TEMPLATES_DIR / "hooks" / "validate-launch.py"
        rendered = substitute_file(path, {"live_sweep_cred_const": "env:X,Y"})
        self.assertIn('LIVE_SWEEP_CRED_CONST = "env:X,Y"', rendered)
        compile(rendered, str(path), "exec")


class PrePushHookExecutionTests(unittest.TestCase):
    """Run-2 gap-fill: execute the rendered pre-push bash hook and assert
    its default-deny polarity actually denies.

    Static tests already verify the template's substitutability and that
    its branch literal lands. They do NOT verify the hook's runtime
    behavior — i.e., when invoked as a bash subprocess against a fake
    push to refs/heads/main, does it exit non-zero when MAW_HUMAN is
    unset (default-deny)? And exit zero when MAW_HUMAN=1 is supplied?

    KISS substrate behavior test: render the template, write to a tempfile,
    invoke bash on it with stdin and env stubs, and inspect rc/stderr.
    """

    def _run_pre_push(
        self,
        *,
        env: dict[str, str] | None = None,
        remote_ref: str = "refs/heads/main",
    ) -> tuple[int, str]:
        """Render the template, invoke as bash subprocess; return (rc, stderr)."""
        import os as _os
        import subprocess as _subprocess
        import tempfile as _tempfile

        path = TEMPLATES_DIR / "git-hooks" / "pre-push"
        rendered = substitute_file(
            path, {"deploy_branch": "main", "alias_suggestion": "mw"}
        )
        with _tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(rendered)
            tmp_path = tmp.name
        _os.chmod(tmp_path, 0o755)
        try:
            # stdin shape: <local_ref> <local_sha> <remote_ref> <remote_sha>
            stdin = f"refs/heads/main aaaaaaaa {remote_ref} bbbbbbbb\n"
            # Scrub MAW_* from inherited env so the test's own host context
            # can't leak in. The caller supplies exactly what should be set.
            base_env = {
                k: v for k, v in _os.environ.items()
                if not k.startswith("MAW_")
            }
            if env:
                base_env.update(env)
            result = _subprocess.run(
                ["bash", tmp_path, "origin", "git@example:repo.git"],
                input=stdin,
                env=base_env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode, result.stderr
        finally:
            _os.unlink(tmp_path)

    def test_blocks_when_maw_human_unset(self):
        rc, err = self._run_pre_push(env=None)
        self.assertNotEqual(rc, 0, "default-deny: missing MAW_HUMAN must block")
        self.assertIn("MAW_HUMAN", err)
        self.assertIn("BLOCKED", err)

    def test_allows_when_maw_human_is_1(self):
        rc, err = self._run_pre_push(env={"MAW_HUMAN": "1"})
        self.assertEqual(rc, 0, f"MAW_HUMAN=1 must permit; stderr={err!r}")

    def test_blocks_when_maw_agent_is_1_even_with_maw_human(self):
        # Agent precedence: MAW_AGENT=1 always blocks, even if MAW_HUMAN=1.
        # Defense against accidental env-bleed where both got set.
        rc, err = self._run_pre_push(env={"MAW_HUMAN": "1", "MAW_AGENT": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("MAW_AGENT", err)
        self.assertIn("BLOCKED", err)

    def test_allows_push_to_non_protected_branch(self):
        # Pushes to dev / feature/* / fix/* are NOT gated regardless of
        # MAW_HUMAN/MAW_AGENT, per the substrate doctrine.
        rc, err = self._run_pre_push(
            env=None,  # no MAW_HUMAN, no MAW_AGENT
            remote_ref="refs/heads/dev",
        )
        self.assertEqual(rc, 0, f"non-protected push must pass; stderr={err!r}")


if __name__ == "__main__":
    unittest.main()
