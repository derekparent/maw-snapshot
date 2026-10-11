"""opconf — the operator's machine-local settings, kept out of the repo.

A few values maw uses belong to the operator, not to maw: where seats find
seeding handoffs, the fleet board's script and fragment dir, the ids of the
operator's committed signing keys, the repos they classified no-echo, and the
Deck artifact seats report runs to. They
used to be hardcoded with one operator's values. Now each is looked up in order:

  1. its env var (see KEYS);
  2. `~/.maw/operator.toml` (untracked; MAW_OPERATOR_CONFIG overrides the path);
  3. maw's neutral default.

Never fails open into someone's private path and never wedges a caller: a
missing file is silent, and a malformed one is one stderr warning and then the
default. A key id has no usable default, so `require` raises NotConfigured
where the value is load-bearing. `python3 -m opconf <key>` prints the resolved
value (empty if unset).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: _parse_simple reads maw's own format
    tomllib = None  # type: ignore[assignment]

# key -> (env var, neutral default). An empty default means "not configured".
KEYS: dict[str, tuple[str, str]] = {
    "handoff_dir": ("MAW_HANDOFF_DIR", "~/.maw/handoff/pending"),
    "board_sh": ("MAW_BOARD_SH", ""),
    "board_dir": ("MAW_BOARD_DIR", ""),
    # File seats append their wrap-retro bullet to at close (empty: skip the append).
    "lessons_file": ("MAW_LESSONS_FILE", ""),
    # Which committed key (maw/guards/keys/<id>.pub) a NEW grant or enrollment
    # claims. Verification never reads these: it takes the id from the grant.
    "grant_pubkey_id": ("MAW_GRANT_PUBKEY_ID", ""),
    "phone_pubkey_id": ("MAW_PHONE_PUBKEY_ID", ""),
    "relay_pubkey_id": ("MAW_RELAY_PUBKEY_ID", ""),
    # Comma-separated repo slugs the operator explicitly classified no-echo.
    "no_echo_repos": ("MAW_NO_ECHO_REPOS", ""),
    # The operator's Deck artifact (claude.ai link) that `net.deck render` targets.
    "deck_url": ("MAW_DECK_URL", ""),
    # The session a blocked seat's QUESTION goes to when the caller names nobody.
    # No default: a question sent to a name nobody answers to is a lost question.
    "question_to": ("MAW_QUESTION_TO", ""),
}

_PATH_KEYS = ("handoff_dir", "board_sh", "board_dir", "lessons_file")


class NotConfigured(ValueError):
    """A load-bearing key has no value in the env or the operator file."""


def config_path() -> Path:
    return Path(os.environ.get("MAW_OPERATOR_CONFIG", "~/.maw/operator.toml")).expanduser()


_SIMPLE_LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"\\]*)"\s*(#.*)?$')


def _parse_simple(text: str) -> dict:
    """The subset maw writes: `key = "string"` lines, # comments and blank lines.
    For interpreters without tomllib, so a present file is never silently
    skipped. Any other line raises ValueError."""
    out: dict = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _SIMPLE_LINE.match(line)
        if not m:
            raise ValueError(f"line {n}: not a key = \"string\" line (no tomllib on this python)")
        out[m.group(1)] = m.group(2)
    return out


def _file_values() -> dict:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        if tomllib is not None:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
        else:
            data = _parse_simple(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # tomllib.TOMLDecodeError is a ValueError
        print(f"opconf: ignoring {p}: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str) -> str:
    """The resolved value for `key` ('' when not configured). Unknown key: KeyError."""
    env_var, default = KEYS[key]
    env = os.environ.get(env_var)
    if env:
        return env
    val = _file_values().get(key)
    if isinstance(val, str) and val:
        return val
    if val is not None and not isinstance(val, str):
        print(f"opconf: ignoring {key} in {config_path()}: not a string", file=sys.stderr)
    return default


def require(key: str) -> str:
    """`get`, but a missing value raises NotConfigured naming where to set it."""
    val = get(key)
    if not val:
        raise NotConfigured(
            f"{key} is not configured: set {KEYS[key][0]} or add "
            f'{key} = "..." to {config_path()}')
    return val


def get_list(key: str) -> "tuple[str, ...]":
    """A comma-separated value as a tuple, blanks dropped."""
    return tuple(x.strip() for x in get(key).split(",") if x.strip())


def get_path(key: str) -> "Path | None":
    """`get` as an expanded Path, or None when not configured."""
    val = get(key)
    return Path(val).expanduser() if val else None


def main(argv: "list[str] | None" = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in KEYS:
        print(f"usage: python3 -m opconf {{{'|'.join(KEYS)}}}", file=sys.stderr)
        return 2
    val = get(args[0])
    if val and args[0] in _PATH_KEYS:
        val = str(Path(val).expanduser())
    print(val)
    return 0


if __name__ == "__main__":
    sys.exit(main())
