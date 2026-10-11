"""The Python floor, said in one line.

`/maw:init` reads pyproject.toml with tomllib, which arrived in Python 3.11.
On an older `python3` (stock macOS ships 3.9) the import died with a traceback
that named a module, not a remedy. This module must itself run on that older
interpreter, so it uses nothing newer than 3.6 syntax and imports only sys.
"""
import sys

MIN_PYTHON = (3, 11)
REFUSE_EXIT = 1


def floor_message(version_info=None, executable=None):
    """One line for an interpreter below the floor, or None when it is fine."""
    info = tuple(sys.version_info if version_info is None else version_info)
    if info[:2] >= MIN_PYTHON:
        return None
    exe = sys.executable if executable is None else executable
    return (
        "maw init needs Python {}.{} or later; `python3` on your PATH is {}.{}.{} ({}). "
        "Install a newer Python, put it first on PATH, and run /maw:init again.".format(
            MIN_PYTHON[0], MIN_PYTHON[1], info[0], info[1], info[2], exe or "unknown path")
    )


def require(version_info=None, executable=None):
    """Exit 1 with that one line on stderr when the interpreter is too old."""
    message = floor_message(version_info, executable)
    if message is not None:
        sys.stderr.write(message + "\n")
        raise SystemExit(REFUSE_EXIT)
