#!/usr/bin/env python3
"""Canonical test runner — one denominator for every green claim.

Why this exists (advisor field report, 2026-06-10): `unittest discover`
silently aborts on the tests/init ↔ maw/init package-name collision, so
the suite was run via a hand-maintained module list in CLAUDE.md — which
goes stale the moment a test file lands (it silently omitted
test_channel_ask for a month, and an independent verify run missed it the
same way). This runner globs the test tree, so a new tests/<pkg>/test_*.py
is in the suite the moment it exists.

Usage (from the repo root):
    python3 tests/run_all.py            # run everything
    python3 tests/run_all.py --list     # print the module list, don't run
"""
from __future__ import annotations

import atexit
import ipaddress
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def discover_modules() -> list[str]:
    """Every test_*.py under tests/ at ANY depth, as dotted module names.

    rglob, not a fixed-depth glob: a top-level tests/test_x.py or a nested
    tests/pkg/sub/test_y.py must join the denominator too — a depth-bound
    pattern would recreate the silent-omission class this runner exists
    to kill.
    """
    return sorted(
        ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
        for path in (REPO_ROOT / "tests").rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


class NetworkBlocked(RuntimeError):
    pass


def _is_loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    if not isinstance(host, str):
        return False
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


# Everything a live seat (or the multiplexer hosting it) exports. A prefix, not
# a list: a list goes stale the day a new MAW_ variable lands.
SEAT_ENV_PREFIXES = ("MAW_", "HERDR_", "TMUX")
# The harness variables maw code reads.
SEAT_ENV_NAMES = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR", "CLAUDE_SESSION_ID")


def isolate_seat_env(environ: "dict[str, str] | None" = None) -> list[str]:
    """The suite never reads the seat it happens to run in.

    Run from an advisor or executor pane, the process inherits MAW_BRIDGE_ROLE,
    MAW_NET_NAME, MAW_NET_DIR (the LIVE switchboard), MAW_SEAT_MODEL, MAW_HUMAN
    and the multiplexer's socket and pane ids. A test that forgets to set one
    then passes or fails by where it was launched, and one that forgets
    MAW_NET_DIR writes to the live switchboard. Every such variable is dropped
    before a test module is imported; tests that need a value set it
    themselves. MAW_NET_DIR is then pointed at a throwaway directory, so the
    default (the real ~/.claude/bridge/switchboard) is out of reach too.

    Returns the names dropped. Call it FIRST: the other isolation steps set
    MAW_ variables of their own.
    """
    env = os.environ if environ is None else environ
    dropped = sorted(k for k in list(env)
                     if k.startswith(SEAT_ENV_PREFIXES) or k in SEAT_ENV_NAMES)
    for k in dropped:
        del env[k]
    scratch = tempfile.mkdtemp(prefix="maw-suite-net-")
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    env["MAW_NET_DIR"] = scratch
    return dropped


def isolate_operator_config() -> None:
    """The suite never reads the operator's own ~/.maw/operator.toml.

    Otherwise a test passes on the operator's Mac because his key ids and repo
    list are configured, and fails on a fresh clone. Tests that need a value
    set it themselves (env or their own MAW_OPERATOR_CONFIG).
    """
    sys.path.insert(0, str(REPO_ROOT / "maw"))
    import opconf

    os.environ["MAW_OPERATOR_CONFIG"] = os.devnull + ".maw-suite-no-operator-config"
    for env_var, _default in opconf.KEYS.values():
        os.environ.pop(env_var, None)


def install_offline_guard() -> None:
    """The suite never talks to the network (Lane C0, rev-2 suite hygiene).

    Jev is forced off and its key stripped, and any TCP/UDP connect to a
    non-loopback address raises NetworkBlocked, so a test that would reach the
    network fails loudly instead of flaking or leaking. Unix sockets and
    loopback (local test servers) are allowed.
    """
    os.environ["MAW_JEV"] = "0"
    os.environ.pop("TYPESAFE_API_KEY", None)
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(sock: socket.socket, address: object) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise NetworkBlocked(f"test suite is offline: connect to {address!r} blocked")

    def connect(self: socket.socket, address: object) -> None:
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        check(self, address)
        return real_connect_ex(self, address)

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and not _is_loopback(name) and name != socket.gethostname():
            raise NetworkBlocked(f"test suite is offline: DNS lookup of {name!r} blocked")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    modules = discover_modules()
    if not modules:
        print("run_all: no test modules found under tests/", file=sys.stderr)
        return 1
    if "--list" in args:
        print("\n".join(modules))
        return 0

    # Imports must resolve the same way the documented invocation does:
    # from the repo root, with the test packages' own sys.path inserts
    # handling maw/.
    sys.path.insert(0, str(REPO_ROOT))
    isolate_seat_env()
    isolate_operator_config()
    install_offline_guard()
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    print(f"run_all: {len(modules)} modules")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
