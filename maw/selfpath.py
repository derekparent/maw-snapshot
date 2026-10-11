"""selfpath: where THIS copy of maw lives, for the commands it prints.

A hint a seat copies ("reply with ...", "restack with ...") has to name a
launcher that exists on the machine reading it. The plugin can be loaded from a
marketplace cache copy, a local clone or a pinned worktree, so no fixed path is
right. `CLAUDE_PLUGIN_ROOT` is substituted in command bodies and exported to
hooks, but it is NOT in the environment of a seat's Bash call, which is where
these modules run. So the root is taken from this file's own location, with
symlinks resolved.
"""
from __future__ import annotations

import shlex
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent


def launcher(name: str = "maw-run") -> str:
    """The shell-quoted absolute path of one of this plugin's bin/ launchers."""
    return shlex.quote(str(PLUGIN_ROOT / "bin" / name))


MAW_RUN = launcher()


if __name__ == "__main__":
    print(MAW_RUN)
