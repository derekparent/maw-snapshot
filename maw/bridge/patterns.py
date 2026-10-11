"""Forbidden-pattern scan for bridge body content (PRD §3.2 + KD9 / Critical 9).

Scan runs at /maw:bridge-in time (consume), per PRD §4.2.0 — defense on consume,
not on write. Hand-edited or tool-written malicious bodies bypass /maw:bridge-out
but cannot bypass /maw:bridge-in. The L1 channel plugin's push site will run the
same scan when it lands (defense in depth per §4.2.5).

Patterns ship as JSON in `patterns.json` next to this module, not YAML per
PRD §3.2 spec text — stdlib-only posture excludes pyyaml; data is identical,
only encoding differs. PRD §3.2 amendment recommended noting the format.

Tradeoff acknowledged (PRD §3.2): a pattern list is reactive — catches known
attacks, inevitably incomplete on novel jailbreaks. The list is one defense
layer; scope-token validator + manual the operator audit-log review are others.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATTERNS_FILE = Path(__file__).parent / "patterns.json"


class PatternsLoadError(Exception):
    """Patterns file missing, malformed, or schema version unsupported."""


@dataclass(frozen=True)
class PatternMatch:
    category: str
    pattern: str
    matched_text: str  # truncated to 80 chars for log-readability


@dataclass(frozen=True)
class PatternRegistry:
    schema_version: int
    compiled: tuple[tuple[str, re.Pattern[str]], ...]


def load_registry(path: Path | None = None) -> PatternRegistry:
    target = path or DEFAULT_PATTERNS_FILE
    if not target.is_file():
        raise PatternsLoadError(f"patterns file not found: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PatternsLoadError(f"could not read patterns from {target}: {e}") from e
    if not isinstance(data, dict):
        raise PatternsLoadError(f"patterns file must be a JSON object: {target}")
    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int):
        raise PatternsLoadError(
            f"patterns file missing schema_version int: {target}"
        )
    if schema_version != 1:
        raise PatternsLoadError(
            f"patterns schema_version={schema_version}, this code handles only 1"
        )
    compiled: list[tuple[str, re.Pattern[str]]] = []
    categories = data.get("categories", [])
    if not isinstance(categories, list):
        raise PatternsLoadError("'categories' must be a list")
    for cat in categories:
        if not isinstance(cat, dict):
            raise PatternsLoadError(f"category entry must be an object: {cat!r}")
        name = cat.get("name")
        if not isinstance(name, str) or not name:
            raise PatternsLoadError(f"category missing name string: {cat!r}")
        patterns_list = cat.get("patterns", [])
        if not isinstance(patterns_list, list):
            raise PatternsLoadError(f"category {name}: patterns must be a list")
        for pat in patterns_list:
            if not isinstance(pat, str):
                raise PatternsLoadError(
                    f"category {name}: non-string pattern: {pat!r}"
                )
            try:
                compiled.append((name, re.compile(pat)))
            except re.error as e:
                raise PatternsLoadError(
                    f"category {name}: pattern {pat!r} did not compile: {e}"
                ) from e
    return PatternRegistry(
        schema_version=schema_version,
        compiled=tuple(compiled),
    )


# Layer 3 of normalize_for_scan: TR39-style confusables, Latin targets.
# Curated, not the full Unicode confusables.txt (stdlib-only posture): the
# Cyrillic + Greek letters that render identically to Latin in common fonts.
# Non-exhaustive by design — the pattern list itself is reactive (see module
# docstring); extend as real bypasses surface.
# Keys are \uXXXX escapes, never raw glyphs: a raw Cyrillic key that was
# accidentally typed as its Latin twin would be a silently-dead map entry
# no reviewer could see.
_CONFUSABLES = str.maketrans({
    # Cyrillic lowercase
    "\u0430": "a",  # CYRILLIC SMALL LETTER A
    "\u0435": "e",  # CYRILLIC SMALL LETTER IE
    "\u043e": "o",  # CYRILLIC SMALL LETTER O
    "\u0440": "p",  # CYRILLIC SMALL LETTER ER
    "\u0441": "c",  # CYRILLIC SMALL LETTER ES
    "\u0443": "y",  # CYRILLIC SMALL LETTER U
    "\u0445": "x",  # CYRILLIC SMALL LETTER HA
    "\u0456": "i",  # CYRILLIC SMALL LETTER BYELORUSSIAN-UKRAINIAN I
    "\u0457": "i",  # CYRILLIC SMALL LETTER YI
    "\u0455": "s",  # CYRILLIC SMALL LETTER DZE
    "\u0458": "j",  # CYRILLIC SMALL LETTER JE
    "\u04bb": "h",  # CYRILLIC SMALL LETTER SHHA
    "\u0501": "d",  # CYRILLIC SMALL LETTER KOMI DE
    "\u051b": "q",  # CYRILLIC SMALL LETTER QA
    "\u051d": "w",  # CYRILLIC SMALL LETTER WE
    "\u0451": "e",  # CYRILLIC SMALL LETTER IO
    # Cyrillic uppercase
    "\u0410": "A",  # CYRILLIC CAPITAL LETTER A
    "\u0412": "B",  # CYRILLIC CAPITAL LETTER VE
    "\u0415": "E",  # CYRILLIC CAPITAL LETTER IE
    "\u041a": "K",  # CYRILLIC CAPITAL LETTER KA
    "\u041c": "M",  # CYRILLIC CAPITAL LETTER EM
    "\u041d": "H",  # CYRILLIC CAPITAL LETTER EN
    "\u041e": "O",  # CYRILLIC CAPITAL LETTER O
    "\u0420": "P",  # CYRILLIC CAPITAL LETTER ER
    "\u0421": "C",  # CYRILLIC CAPITAL LETTER ES
    "\u0422": "T",  # CYRILLIC CAPITAL LETTER TE
    "\u0423": "Y",  # CYRILLIC CAPITAL LETTER U
    "\u0425": "X",  # CYRILLIC CAPITAL LETTER HA
    "\u0406": "I",  # CYRILLIC CAPITAL LETTER BYELORUSSIAN-UKRAINIAN I
    "\u0405": "S",  # CYRILLIC CAPITAL LETTER DZE
    "\u0408": "J",  # CYRILLIC CAPITAL LETTER JE
    # Greek lowercase
    "\u03b1": "a",  # GREEK SMALL LETTER ALPHA
    "\u03b5": "e",  # GREEK SMALL LETTER EPSILON
    "\u03b7": "n",  # GREEK SMALL LETTER ETA
    "\u03b9": "i",  # GREEK SMALL LETTER IOTA
    "\u03ba": "k",  # GREEK SMALL LETTER KAPPA
    "\u03bd": "v",  # GREEK SMALL LETTER NU
    "\u03bf": "o",  # GREEK SMALL LETTER OMICRON
    "\u03c1": "p",  # GREEK SMALL LETTER RHO
    "\u03c4": "t",  # GREEK SMALL LETTER TAU
    "\u03c5": "u",  # GREEK SMALL LETTER UPSILON
    "\u03c7": "x",  # GREEK SMALL LETTER CHI
    # Greek uppercase
    "\u0391": "A",  # GREEK CAPITAL LETTER ALPHA
    "\u0392": "B",  # GREEK CAPITAL LETTER BETA
    "\u0395": "E",  # GREEK CAPITAL LETTER EPSILON
    "\u0396": "Z",  # GREEK CAPITAL LETTER ZETA
    "\u0397": "H",  # GREEK CAPITAL LETTER ETA
    "\u0399": "I",  # GREEK CAPITAL LETTER IOTA
    "\u039a": "K",  # GREEK CAPITAL LETTER KAPPA
    "\u039c": "M",  # GREEK CAPITAL LETTER MU
    "\u039d": "N",  # GREEK CAPITAL LETTER NU
    "\u039f": "O",  # GREEK CAPITAL LETTER OMICRON
    "\u03a1": "P",  # GREEK CAPITAL LETTER RHO
    "\u03a4": "T",  # GREEK CAPITAL LETTER TAU
    "\u03a5": "Y",  # GREEK CAPITAL LETTER UPSILON
    "\u03a7": "X",  # GREEK CAPITAL LETTER CHI
    # Latin lookalikes
    "\u0131": "i",  # LATIN SMALL LETTER DOTLESS I
    # IPA / phonetic Latin (advisor-reproduced bypasses 2026-06-12; none NFKC-fold)
    "\u0251": "a",  # LATIN SMALL LETTER ALPHA
    "\u0261": "g",  # LATIN SMALL LETTER SCRIPT G
    "\u0269": "i",  # LATIN SMALL LETTER IOTA
    # Small capitals (render as ASCII capitals at x-height; not NFKC-foldable)
    "\u1d00": "a",  # LATIN LETTER SMALL CAPITAL A
    "\u0299": "b",  # LATIN LETTER SMALL CAPITAL B
    "\u1d04": "c",  # LATIN LETTER SMALL CAPITAL C
    "\u1d05": "d",  # LATIN LETTER SMALL CAPITAL D
    "\u1d07": "e",  # LATIN LETTER SMALL CAPITAL E
    "\ua730": "f",  # LATIN LETTER SMALL CAPITAL F
    "\u0262": "g",  # LATIN LETTER SMALL CAPITAL G
    "\u029c": "h",  # LATIN LETTER SMALL CAPITAL H
    "\u026a": "i",  # LATIN LETTER SMALL CAPITAL I
    "\u1d0a": "j",  # LATIN LETTER SMALL CAPITAL J
    "\u1d0b": "k",  # LATIN LETTER SMALL CAPITAL K
    "\u029f": "l",  # LATIN LETTER SMALL CAPITAL L
    "\u1d0d": "m",  # LATIN LETTER SMALL CAPITAL M
    "\u0274": "n",  # LATIN LETTER SMALL CAPITAL N
    "\u1d0f": "o",  # LATIN LETTER SMALL CAPITAL O
    "\u1d18": "p",  # LATIN LETTER SMALL CAPITAL P
    "\u0280": "r",  # LATIN LETTER SMALL CAPITAL R
    "\ua731": "s",  # LATIN LETTER SMALL CAPITAL S
    "\u1d1b": "t",  # LATIN LETTER SMALL CAPITAL T
    "\u1d1c": "u",  # LATIN LETTER SMALL CAPITAL U
    "\u1d20": "v",  # LATIN LETTER SMALL CAPITAL V
    "\u1d21": "w",  # LATIN LETTER SMALL CAPITAL W
    "\u028f": "y",  # LATIN LETTER SMALL CAPITAL Y
    "\u1d22": "z",  # LATIN LETTER SMALL CAPITAL Z
})


def normalize_for_scan(body: str) -> str:
    """Fold Unicode evasion before pattern matching (BRIDGE-4).

    Three layers, all load-bearing — NFKC alone is a FALSE fix (advisor
    re-verify 2026-06-12): NFKC neither strips U+200B (category Cf survives
    normalization) nor folds Cyrillic U+0456 to Latin i.

      1. strip Cf/format chars (zero-width space/joiner, BOM, LRM/RLM)
      2. NFKC for compatibility forms (fullwidth, ligatures, roman numerals)
      3. TR39-style confusables skeleton: NFD-decompose, drop combining
         marks, fold curated Cyrillic/Greek homoglyphs to Latin

    ASCII is a fixed point of all three layers, so scanning the normalized
    text is strictly stronger than scanning raw for the ASCII pattern set.
    """
    body = "".join(c for c in body if unicodedata.category(c) != "Cf")
    body = unicodedata.normalize("NFKC", body)
    body = unicodedata.normalize("NFD", body)
    body = "".join(c for c in body if unicodedata.category(c) != "Mn")
    body = body.translate(_CONFUSABLES)
    return unicodedata.normalize("NFC", body)


def scan(body: str, registry: PatternRegistry | None = None) -> list[PatternMatch]:
    """Scan body for forbidden patterns. Returns matches; empty list on clean.

    Matching runs against the normalize_for_scan() folding of the body, so
    matched_text reports the folded form of an obfuscated attack.
    """
    if registry is None:
        registry = load_registry()
    body = normalize_for_scan(body)
    matches: list[PatternMatch] = []
    for category, regex in registry.compiled:
        m = regex.search(body)
        if m:
            text = m.group(0)
            matches.append(PatternMatch(
                category=category,
                pattern=regex.pattern,
                matched_text=text[:80],
            ))
    return matches


__all__ = [
    "PatternsLoadError",
    "PatternMatch",
    "PatternRegistry",
    "load_registry",
    "normalize_for_scan",
    "scan",
    "DEFAULT_PATTERNS_FILE",
]
