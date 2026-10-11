"""Stack detection for /maw:init (PRD §4.1.1).

Walks a target repo's manifest files (package.json, requirements.txt,
pyproject.toml, Cargo.toml, Package.swift, *.swift) to determine which
profile template applies. Returns a Detection with stack + framework +
manifest evidence; the caller composes the profile name.

Order of checks (first-match-wins per PRD §4.1.1):
  1. package.json     → node-<framework>      (next | react | express | plain)
  2. requirements.txt
     or pyproject.toml → python-<framework>   (flask | fastapi | django | plain)
  3. Cargo.toml       → rust-plain
  4. Package.swift
     or root *.swift  → swift-plain
  5. None             → generic

Two safety gates per PRD §6 Phase 1a:
  - Refuses non-git dirs unless --no-git is passed.
  - Refuses git submodules (.git is a file, not a dir) unless --submodule-leaf
    is passed. Submodule init writes substrate that the parent repo would
    incorporate via `git submodule update` — likely not what the user wants.

Stdlib-only: tomllib (3.11+) for pyproject.toml, json for package.json,
re for requirements.txt line parsing.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Frameworks we explicitly recognize. Anything else maps to "plain".
NODE_FRAMEWORKS = ("next", "react", "express")
PYTHON_FRAMEWORKS = ("flask", "fastapi", "django")

# requirements.txt line shape: package_name[==<spec>][; markers]
# Conservative: name is the first run of [a-zA-Z0-9_.-] chars.
_REQ_LINE_RE = re.compile(r"^\s*([a-zA-Z0-9][a-zA-Z0-9_.\-]*)")


class StackDetectionError(Exception):
    """Cannot detect stack or refused due to safety gate."""


class NotAGitRepoError(StackDetectionError):
    """Target dir is not a git repo and --no-git wasn't passed."""


class IsGitSubmoduleError(StackDetectionError):
    """Target is a git submodule and --submodule-leaf wasn't passed."""


@dataclass(frozen=True)
class Detection:
    stack: str  # "node" | "python" | "rust" | "swift" | "generic"
    framework: str  # "next" | "flask" | ... | "plain" (always set)
    manifests_seen: tuple[str, ...] = field(default_factory=tuple)

    @property
    def profile(self) -> str:
        """Profile name composed from stack + framework. Generic stack → "generic".

        Phase 1c TODO (senior 2026-05-08, flags 1 + 3):

        FLAG 1 (template coverage gap): this composer can emit ~9 distinct
        profile names (python-flask | python-fastapi | python-django |
        node-next | node-react | node-express | rust-plain | swift-plain |
        generic). v0.6 ships only 4 templates (python-flask-railway,
        python-fastapi-railway, node-next-vercel, generic). When /maw:init
        in Phase 1c looks up the template for an unsupported profile name,
        it must pick one of:
          (a) fall back to generic when no specific template exists
          (b) add the 5 missing templates before /maw:init ships
          (c) refuse-to-init repos outside the supported set
        Decision must be conscious — silent (a) is the riskiest because it
        hides "I don't really support your stack" behind a working
        init that produces an under-specified config.

        FLAG 3 (deploy-target axis is separate from stack-framework):
        detection emits "python-flask"; the matching template ships as
        "python-flask-railway". Deploy target (railway | vercel | fly |
        docker | none) is orthogonal to stack-framework — Phase 1c needs
        an explicit resolver (function or table), not implicit string
        surgery, that maps (stack, framework, deploy_target) → template
        path. Deploy-target detection itself is Phase 1c work (vercel.json,
        railway.toml, fly.toml, Dockerfile presence + user prompt).
        """
        if self.stack == "generic":
            return "generic"
        return f"{self.stack}-{self.framework}"


def _check_git(root: Path, no_git: bool, submodule_leaf: bool) -> None:
    git_path = root / ".git"
    if not git_path.exists():
        if no_git:
            return
        raise NotAGitRepoError(
            f"{root} is not a git repository; pass --no-git to override "
            f"(maw substrate is git-aware; non-git use is opt-in)"
        )
    if git_path.is_file() and not submodule_leaf:
        raise IsGitSubmoduleError(
            f"{root} appears to be a git submodule (.git is a file pointing "
            f"at the parent's gitdir); pass --submodule-leaf to opt in. "
            f"Submodule substrate is usually not what you want."
        )


def _detect_node(root: Path) -> tuple[str, list[str]]:
    """Read package.json's dependencies + devDependencies; pick the first known framework."""
    manifest = root / "package.json"
    seen = ["package.json"]
    try:
        with open(manifest, "rb") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "plain", seen
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section.keys())
    for fw in NODE_FRAMEWORKS:
        if fw in deps:
            return fw, seen
    return "plain", seen


def _detect_python(root: Path) -> tuple[str, list[str]]:
    """Read pyproject.toml [project.dependencies] + requirements.txt; pick first known framework."""
    seen: list[str] = []
    deps: set[str] = set()

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        seen.append("pyproject.toml")
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        # PEP 621 [project] table
        project = data.get("project", {}) if isinstance(data, dict) else {}
        if isinstance(project, dict):
            for entry in project.get("dependencies", []) or []:
                if isinstance(entry, str):
                    name = _project_dep_name(entry)
                    if name:
                        deps.add(name)
            optional = project.get("optional-dependencies", {})
            if isinstance(optional, dict):
                for group in optional.values():
                    if not isinstance(group, list):
                        continue
                    for entry in group:
                        if isinstance(entry, str):
                            name = _project_dep_name(entry)
                            if name:
                                deps.add(name)
        # Poetry / non-PEP-621 layouts: tool.poetry.dependencies
        tool = data.get("tool", {}) if isinstance(data, dict) else {}
        poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
        if isinstance(poetry, dict):
            poetry_deps = poetry.get("dependencies", {})
            if isinstance(poetry_deps, dict):
                deps.update(name.lower() for name in poetry_deps.keys())

    requirements = root / "requirements.txt"
    if requirements.is_file():
        seen.append("requirements.txt")
        try:
            text = requirements.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _REQ_LINE_RE.match(line)
            if m:
                deps.add(m.group(1).lower())

    for fw in PYTHON_FRAMEWORKS:
        if fw in deps:
            return fw, seen
    return "plain", seen


def _project_dep_name(entry: str) -> str | None:
    """Strip PEP 508 spec/markers off a dependency string. 'flask>=2.0; python_version>=3.10' → 'flask'."""
    m = _REQ_LINE_RE.match(entry)
    return m.group(1).lower() if m else None


def is_xcode_env(root: Path) -> bool:
    """True when `root` is an iOS/Xcode-shaped repo.

    Used by the tools-awareness SessionStart hook to decide whether to also
    surface the Xcode MCP line (build/run the iOS app). Deliberately broader
    than `detect`'s swift branch: an app repo can carry an .xcodeproj /
    .xcworkspace / Podfile with its Swift sources nested under a target dir
    (not at root), so `detect` would call it "generic" while it is plainly an
    iOS build environment. We treat ANY of these as the signal:
      - an .xcodeproj or .xcworkspace bundle (anywhere at the top level)
      - Package.swift (SwiftPM)
      - a Podfile (CocoaPods)
      - any *.swift source (root or one level down, to catch the common
        `Sources/`, `<AppName>/` layouts without an unbounded tree walk)

    Pure predicate: never raises, never touches git gates — the hook is
    fail-open and calls this on every session, so a thrown exception here
    would be a regression. Returns False on any I/O error.
    """
    try:
        root = root.resolve()
        for child in root.iterdir():
            suffix = child.suffix.lower()
            if suffix in (".xcodeproj", ".xcworkspace"):
                return True
        if (root / "Package.swift").is_file():
            return True
        if (root / "Podfile").is_file():
            return True
        # Swift sources at root or exactly one level down (Sources/, App/, …).
        if any(root.glob("*.swift")):
            return True
        if any(root.glob("*/*.swift")):
            return True
    except OSError:
        return False
    return False


def detect(
    root: Path,
    *,
    no_git: bool = False,
    submodule_leaf: bool = False,
) -> Detection:
    """Detect the stack of a target repo.

    Raises NotAGitRepoError or IsGitSubmoduleError if the safety gates trip.
    Otherwise returns a Detection. Order of checks per PRD §4.1.1.
    """
    root = root.resolve()
    _check_git(root, no_git=no_git, submodule_leaf=submodule_leaf)

    if (root / "package.json").is_file():
        framework, seen = _detect_node(root)
        return Detection(stack="node", framework=framework, manifests_seen=tuple(seen))

    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        framework, seen = _detect_python(root)
        return Detection(stack="python", framework=framework, manifests_seen=tuple(seen))

    if (root / "Cargo.toml").is_file():
        return Detection(stack="rust", framework="plain", manifests_seen=("Cargo.toml",))

    if (root / "Package.swift").is_file():
        return Detection(stack="swift", framework="plain", manifests_seen=("Package.swift",))

    swift_files = sorted(p.name for p in root.glob("*.swift"))
    if swift_files:
        return Detection(
            stack="swift",
            framework="plain",
            manifests_seen=tuple(swift_files),
        )

    return Detection(stack="generic", framework="plain", manifests_seen=())


__all__ = [
    "StackDetectionError",
    "NotAGitRepoError",
    "IsGitSubmoduleError",
    "Detection",
    "detect",
    "is_xcode_env",
    "NODE_FRAMEWORKS",
    "PYTHON_FRAMEWORKS",
]
