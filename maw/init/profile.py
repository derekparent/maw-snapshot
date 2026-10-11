"""Profile YAML loader for /maw:init.

Profile YAMLs at maw/init/templates/profiles/*.yaml carry four blocks that
/maw:init consumes directly: hooks:, git_hooks:, schemas:, template_vars:.
The rest of the YAML body is the `.maw/config.yaml` template content that
ships rendered into the target repo.

We deliberately do NOT take a YAML dependency (stdlib-only doctrine, per
CLAUDE.md). The line-based readers below cover only the shapes we use:
top-level `key:` followed by `- item` lists, and top-level `key:` followed
by `name: "value"` dicts of strings. tests/init/test_templates.py uses
the same shape and reader.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from init.substitution import substitute_file


class ProfileError(Exception):
    """Profile YAML missing / malformed / unknown profile name."""


@dataclass(frozen=True)
class Profile:
    name: str
    path: Path
    hooks: tuple[str, ...]
    git_hooks: tuple[str, ...]
    schemas: tuple[str, ...]
    template_vars: dict[str, str]

    @property
    def all_template_paths(self) -> tuple[str, ...]:
        """Every template-relative path this profile installs.

        Order is significant for observability: hooks, then git-hooks, then
        schemas. Used by the install walker for predictable banner output.
        """
        return tuple(self.hooks) + tuple(self.git_hooks) + tuple(self.schemas)


def _read_yaml_list(text: str, key: str) -> list[str]:
    """Extract a top-level `<key>:` block whose body is `- item` lines."""
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
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            items.append(value)
    return items


def _read_yaml_string_dict(text: str, key: str) -> dict[str, str]:
    """Extract a top-level `<key>:` block whose body is `name: "value"` lines."""
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


def _read_nested_string(text: str, parent: str, child: str) -> str | None:
    """Extract `<parent>:\\n  <child>: "value"` (one level deep, quoted string)."""
    pattern = re.compile(
        rf"(?m)^{re.escape(parent)}:\s*\n((?:[ \t]+.*\n|[ \t]*#.*\n|\s*\n)+)"
    )
    match = pattern.search(text)
    if not match:
        return None
    entry_re = re.compile(
        rf'^\s+{re.escape(child)}\s*:\s*"([^"]*)"\s*$'
    )
    for line in match.group(1).splitlines():
        m = entry_re.match(line)
        if m:
            return m.group(1)
    return None


def profiles_dir() -> Path:
    """Return the canonical profiles/ directory inside this plugin install."""
    return Path(__file__).resolve().parent / "templates" / "profiles"


def list_available_profiles() -> list[str]:
    """Names of profiles bundled with the substrate (without .yaml suffix)."""
    return sorted(p.stem for p in profiles_dir().glob("*.yaml"))


def load_profile(name: str) -> Profile:
    """Load a profile by name. Raises ProfileError if not found / malformed."""
    path = profiles_dir() / f"{name}.yaml"
    if not path.is_file():
        avail = ", ".join(list_available_profiles())
        raise ProfileError(
            f"unknown profile {name!r}. Available: {avail}"
        )
    text = path.read_text(encoding="utf-8")
    hooks = tuple(_read_yaml_list(text, "hooks"))
    git_hooks = tuple(_read_yaml_list(text, "git_hooks"))
    schemas = tuple(_read_yaml_list(text, "schemas"))
    template_vars = _read_yaml_string_dict(text, "template_vars")
    if not (hooks or git_hooks or schemas):
        raise ProfileError(
            f"profile {name!r} declares no hooks/git_hooks/schemas — malformed"
        )
    return Profile(
        name=name,
        path=path,
        hooks=hooks,
        git_hooks=git_hooks,
        schemas=schemas,
        template_vars=template_vars,
    )


def render_config_yaml(profile: Profile, variables: dict[str, str]) -> str:
    """Render the profile YAML as the target repo's .maw/config.yaml content.

    The profile file IS the config template — the hooks/git_hooks/schemas/
    template_vars blocks travel into the installed config.yaml. That's
    intentional per KD3: a re-run of /maw:init can read the config to
    rediscover what the profile installed without consulting the substrate.
    """
    return substitute_file(profile.path, variables)


def read_profile_name_from_config(config_text: str) -> str | None:
    """Extract maw.profile from a rendered config.yaml. None if absent."""
    return _read_nested_string(config_text, "maw", "profile")


def read_repo_id_from_config(config_text: str) -> str | None:
    """Extract maw.repo_id from a rendered config.yaml. None if absent."""
    return _read_nested_string(config_text, "maw", "repo_id")


def read_repo_slug_from_config(config_text: str) -> str | None:
    """Extract bridge.repo_slug from a rendered config.yaml. None if absent."""
    return _read_nested_string(config_text, "bridge", "repo_slug")


__all__ = [
    "Profile",
    "ProfileError",
    "load_profile",
    "list_available_profiles",
    "profiles_dir",
    "render_config_yaml",
    "read_profile_name_from_config",
    "read_repo_id_from_config",
    "read_repo_slug_from_config",
]
