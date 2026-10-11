"""Risk gear — path-classified review depth (backlog item 6).

Review ceremony doesn't scale down: a 1-line docs PR got the same full
reproduce+suite as a runtime change. The risk gear classifies a PR's changed
paths into a tier and recommends a proportionate gate, so the heavy ceremony
fires where it matters and a docs-only change gets a light pass.

Fail-safe: the ceremony only scales DOWN for provably-light changes (docs only).
An unrecognized path is UNKNOWN → full review. A PR mixing tiers gets the
most-thorough tier present (a docs+runtime PR is runtime, never docs).
"""
from __future__ import annotations

from dataclasses import dataclass

RUNTIME = "runtime"   # code — full adversarial review + suite
ORACLE = "oracle"     # UI / external-service — full + state-forcing harness (items 2/3)
UNKNOWN = "unknown"   # unrecognized — full, fail-safe
DOCS = "docs"         # docs/trivial — light (diff + scope), skip suite

# Most-thorough first: the tier of a multi-tier PR is the first one present.
_PRECEDENCE = (ORACLE, RUNTIME, UNKNOWN, DOCS)

_DOCS_EXT = (".md", ".txt", ".rst")
_DOCS_DIR = ("docs/", "findings/")
_ORACLE_EXT = (
    ".swift", ".kt", ".tsx", ".jsx", ".vue", ".html", ".css", ".scss",
    ".storyboard", ".xib", ".xcassets",
)
_ORACLE_DIR = ("/ui/", "ui/", "/components/", "components/", "/views/", "views/")
_RUNTIME_EXT = (
    ".py", ".sh", ".js", ".ts", ".rb", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
)


@dataclass(frozen=True)
class Gate:
    tier: str
    suite: bool          # run the canonical test suite
    adversarial: str     # depth of adversarial review
    worktree: bool       # reproduce in a clean worktree at the SHA
    harness: bool        # state-forcing harness + provenance (items 2/3)
    summary: str


def classify_path(path: str) -> str:
    """Tier for a single changed path.

    EXTENSION is authoritative (a file's type is its extension), so a code file
    in a docs dir — `docs/deploy.py` — is runtime, not waved through as docs
    (the fail-safe direction). Directory hints only classify extensionless
    files.
    """
    p = path.lower().lstrip("./")
    base = p.rsplit("/", 1)[-1]
    # 0. markdown the model EXECUTES (slash commands, agents, skills, CLAUDE.md)
    #    is behaviour, not documentation, whatever its extension says.
    if base == "claude.md" or base == "skill.md" or any(
            f"/{d}/" in f"/{p}" for d in ("commands", "agents", "skills")):
        return RUNTIME
    # 1. extension is authoritative
    if p.endswith(_ORACLE_EXT):
        return ORACLE
    if p.endswith(_RUNTIME_EXT):
        return RUNTIME
    if p.endswith(_DOCS_EXT) or base in ("license", "readme"):
        return DOCS
    # 2. extensionless / fallback: directory hints
    if any(d in p for d in _ORACLE_DIR):
        return ORACLE
    if "/hooks/" in p or p.startswith("hooks/"):
        return RUNTIME
    if any(d in p for d in _DOCS_DIR):
        return DOCS
    return UNKNOWN


def classify_paths(paths: list[str]) -> str:
    """Tier for a PR — the most-thorough tier among its changed paths.

    Empty change set is UNKNOWN (fail-safe): a PR that changed nothing detectable
    should not be waved through as docs.
    """
    tiers = {classify_path(p) for p in paths if p.strip()}
    if not tiers:
        return UNKNOWN
    for t in _PRECEDENCE:
        if t in tiers:
            return t
    return UNKNOWN


def recommend(tier: str) -> Gate:
    if tier == DOCS:
        return Gate(
            tier=DOCS, suite=False, adversarial="light (diff + scope)",
            worktree=False, harness=False,
            summary="docs/trivial: read the diff + confirm scope; skip the suite",
        )
    if tier == ORACLE:
        return Gate(
            tier=ORACLE, suite=True, adversarial="full", worktree=True, harness=True,
            summary="UI/external-oracle: full review + state-forcing harness + "
            "build-from-SHA provenance (items 2/3) — a screenshot is not evidence",
        )
    if tier == RUNTIME:
        return Gate(
            tier=RUNTIME, suite=True, adversarial="full", worktree=True, harness=False,
            summary="runtime code: full adversarial review, reproduce the suite in "
            "a clean worktree at the SHA",
        )
    return Gate(
        tier=UNKNOWN, suite=True, adversarial="full", worktree=True, harness=False,
        summary="unrecognized paths: full review (fail-safe) — classify them and "
        "tighten the gear if this recurs",
    )


def gate_for_paths(paths: list[str]) -> Gate:
    return recommend(classify_paths(paths))
