"""Template substitution — `{{var}}` placeholder resolver (PRD §6 Phase 1a).

Used by `/maw:init` to render template files (profile YAML, hook scripts,
git-hooks) into the target repo. Stdlib-only.

Design:
  - Placeholder syntax: `{{name}}` where `name` matches `[a-zA-Z_][a-zA-Z0-9_]*`
  - Unknown placeholders fail closed (UnresolvedPlaceholderError); silent
    fallthrough would let template-authoring typos ship as literal `{{typo}}`
    text in target-repo files.
  - Variables not used by a template are silently ignored — templates declare
    what they need; the resolver passes a superset.
  - No literal-curly-brace escaping in v0.6. If a template needs literal `{{`
    followed by an identifier, that's a v0.7 concern. Today's templates don't.

The "pre-push template uses `{{alias_suggestion}}`" example in PRD §6 Phase 1a
is the canonical use case — `/maw:init`'s alias-suggestion output is one of
the resolved variables.
"""
from __future__ import annotations

import re
from pathlib import Path

# Whitespace-strict by intent (senior 2026-05-08 flag 2): `{{name}}` matches,
# `{{ name }}` does not. A future hand may be tempted to relax this thinking
# it's an oversight — don't. Loose matching would silently consume
# `{{ literal text with spaces }}` in templates where the author meant the
# braces literally. Strict syntax makes typos surface as literal `{{typo}}`
# text, which the unknown-placeholder fail-closed path then catches at
# substitute() time.
PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


class SubstitutionError(Exception):
    """Base class for substitution errors."""


class UnresolvedPlaceholderError(SubstitutionError):
    """Template references a placeholder not present in the variables dict."""


def substitute(template: str, variables: dict[str, str]) -> str:
    """Replace every `{{name}}` in template with variables[name].

    Raises UnresolvedPlaceholderError if any placeholder is not in variables.
    """
    missing: list[str] = []

    def replace(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in variables:
            missing.append(name)
            return ""
        return variables[name]

    rendered = PLACEHOLDER_RE.sub(replace, template)
    if missing:
        unique = sorted(set(missing))
        raise UnresolvedPlaceholderError(
            f"template references unknown placeholder(s): "
            f"{', '.join(f'{{{{{n}}}}}' for n in unique)}"
        )
    return rendered


def substitute_file(template_path: Path, variables: dict[str, str]) -> str:
    """Read a template file, substitute placeholders, return rendered text."""
    text = template_path.read_text(encoding="utf-8")
    return substitute(text, variables)


def find_placeholders(template: str) -> set[str]:
    """Return the set of placeholder names referenced by a template.

    Useful for `/maw:init` to validate it has resolved every variable a
    template needs before rendering.
    """
    return {m.group(1) for m in PLACEHOLDER_RE.finditer(template)}


__all__ = [
    "SubstitutionError",
    "UnresolvedPlaceholderError",
    "PLACEHOLDER_RE",
    "substitute",
    "substitute_file",
    "find_placeholders",
]
