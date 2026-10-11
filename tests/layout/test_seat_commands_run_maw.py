"""Seat commands run maw's code, never the target repo's (issue #324).

`python3 -m X` puts the CWD first on sys.path. A seat runs its commands from the
TARGET repo, so a repo with a top-level `net/`, `guards/`, `bridge/`, `review/`
or `opconf.py` would run its own module in place of maw's, including
`guards.session_auth merge` (the merge gate) and `net.deck` (the Deck writer).
PYTHONSAFEPATH / -P fix that only on Python 3.11+, and macOS's /usr/bin/python3
is 3.9, so every seat-facing command goes through `bin/maw-run`, which drops
the cwd entry on any Python (#326).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAW = ROOT / "maw"
sys.path.insert(0, str(MAW))

PACKAGES = sorted(p.name for p in MAW.iterdir()
                  if p.is_dir() and (p / "__init__.py").exists()) + ["opconf"]
# A maw module started by bare `python3 -m`, with or without a PYTHONPATH prefix.
BARE = re.compile(r"(?<![\w/.-])python3 -m (?:%s)\b" % "|".join(map(re.escape, PACKAGES)))
DOCUMENTED = re.compile(r'maw-run"? ((?:%s)(?:\.[a-z_]+)*)' % "|".join(map(re.escape, PACKAGES)))


def seat_surfaces() -> "list[Path]":
    """Everything a seat reads and runs from a target repo's cwd: its commands,
    its hooks, and the two docs seats are told to follow."""
    return sorted(list((MAW / "commands").glob("*.md")) + list((MAW / "hooks").glob("*.sh"))
                  + [MAW / "docs" / "cos-batch-arm.md", MAW / "docs" / "watch-recipes.md", MAW / "docs" / "ADVISOR-PLAYBOOK.md"])


def module_launches_without_a_pinned_cwd() -> "list[str]":
    """Every argv list in maw's own Python that starts `python3 -m` (or
    `sys.executable, "-m"`) in a call with no cwd=: those run from whatever cwd
    the seat is in, which for a seat is the target repo (#324)."""
    import ast
    hits = []
    for f in sorted(MAW.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
        pinned = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(k.arg == "cwd" for k in node.keywords):
                pinned |= {id(a) for a in node.args}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.List) and len(node.elts) >= 2):
                continue
            first, second = node.elts[0], node.elts[1]
            is_py = (isinstance(first, ast.Constant) and first.value in ("python3", "python")) or (
                isinstance(first, ast.Attribute) and first.attr == "executable")
            if is_py and isinstance(second, ast.Constant) and second.value == "-m" \
                    and id(node) not in pinned:
                hits.append(f"{f.relative_to(ROOT)}:{node.lineno}")
    return hits


class NoBareModuleRuns(unittest.TestCase):
    def test_no_seat_surface_runs_a_maw_module_with_bare_python3_m(self) -> None:
        hits = [f"{f.relative_to(ROOT)}:{i}: {line.strip()[:120]}"
                for f in seat_surfaces()
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
                if BARE.search(line)]
        self.assertEqual(hits, [], "use the plugin's bin/maw-run, not python3 -m (#324)")

    def test_maws_own_python_never_launches_a_module_from_the_seats_cwd(self) -> None:
        self.assertEqual(module_launches_without_a_pinned_cwd(), [],
                         "launch maw modules via bin/maw-run (or with cwd= maw's tree)")

    def test_the_hints_a_seat_copies_go_through_maw_run(self) -> None:
        from bridge import cli as bridge_cli
        from net import registry, transport
        from review import freshness

        def entry(name: str) -> registry.Entry:
            return registry.Entry(name=name, role=name, repo_root="", cwd="", pid=1, tty=None,
                                  tmux_pane=None, session_id=name, started_at="", last_seen="")

        hints = {
            "reply hint": transport.wrap_message("x", entry("executor")),
            "recv pointer": transport.pointer_body(entry("executor"), entry("advisor"), 1, "hi"),
            "ingest hint": bridge_cli.ingest_command(1, "advisor"),
            "restack hint": freshness.RESTACK_CMD,
        }
        for name, text in hints.items():
            with self.subTest(name):
                self.assertIsNone(BARE.search(text), text)
                self.assertIn("/bin/maw-run ", text.replace('"', ""))


@unittest.skipIf(shutil.which("python3") is None, "python3 not on PATH")
class ShadowingRepoRunsMawsCode(unittest.TestCase):
    """The falsifier from the issue: from a dir holding a module that prints
    SHADOWED, every module the seat commands run is maw's own."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.modules = sorted({m for f in seat_surfaces()
                               for m in DOCUMENTED.findall(f.read_text(encoding="utf-8"))})
        self.assertIn("guards.session_auth", self.modules)
        self.assertIn("net.deck", self.modules)
        self.modules = sorted(set(self.modules) | {"repin.check"})
        for mod in self.modules:
            parts = mod.split(".")
            d = self.repo.joinpath(*parts[:-1])
            d.mkdir(parents=True, exist_ok=True)
            for i in range(1, len(parts)):
                (self.repo.joinpath(*parts[:i]) / "__init__.py").touch()
            (d / f"{parts[-1]}.py").write_text("print('SHADOWED')\n", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "MAW_"))}
        self.env["HOME"] = str(self.repo / "home")

    def run_in_repo(self, argv: "list[str]", **env) -> str:
        cp = subprocess.run(argv, cwd=str(self.repo), env={**self.env, **env},
                            capture_output=True, text=True, timeout=60)
        return cp.stdout + cp.stderr

    def test_the_old_form_ran_the_target_repos_code(self) -> None:
        # The control: without the fix, a shadowing repo wins.
        out = self.run_in_repo(["python3", "-m", "guards.session_auth", "--help"],
                               PYTHONPATH=str(MAW))
        self.assertIn("SHADOWED", out)

    def test_every_documented_module_runs_maws_own(self) -> None:
        for mod in self.modules:
            with self.subTest(mod):
                out = self.run_in_repo([str(MAW / "bin" / "maw-run"), mod, "--help"])
                self.assertNotIn("SHADOWED", out)
                # A typo'd module in a doc would also never print SHADOWED.
                self.assertNotIn("No module named", out)

    def test_the_merge_deploy_and_the_deadman_relay_run_maws_code(self) -> None:
        # These run AUTOMATICALLY from the seat's cwd (after every merge; from
        # the watchdog loop). Capture the argv each one builds, then run that
        # launcher + module with --help from the shadowing repo: the real
        # deploy/readback/send must not run here, only the module resolution.
        import io
        import contextlib
        from guards import session_auth as sa
        from net import deadman, watch
        seen: "list[list[str]]" = []

        def runner(cmd, *a, **k):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with contextlib.redirect_stderr(io.StringIO()):
            deadman._default_pager(
                watch.Finding(kind="dark-session", subject="advisor", detail="d", age_min=9),
                "executor", runner=runner)
        # The merge wrapper's deploy + readback launcher (test_session_auth pins
        # that merge_pr calls exactly this argv prefix).
        seen.append([sa.MAW_RUN, "repin.check", "sync"])
        launches = [c[:2] for c in seen if c and c[0].endswith("/bin/maw-run")]
        mods = {c[1] for c in launches}
        self.assertTrue({"repin.check", "net.cli"} <= mods, f"captured: {seen}")
        for launcher, mod in launches:
            with self.subTest(mod):
                out = self.run_in_repo([launcher, mod, "--help"])
                self.assertNotIn("SHADOWED", out)
                self.assertNotIn("No module named", out)

    @unittest.skipUnless(Path("/usr/bin/python3").exists(), "no system python3")
    def test_also_on_the_system_python_before_safepath(self) -> None:
        # macOS ships 3.9, where neither PYTHONSAFEPATH nor -P exists.
        path = "/usr/bin:" + self.env.get("PATH", "")
        for mod in ("guards.session_auth", "net.deck", "net.cli"):
            with self.subTest(mod):
                out = self.run_in_repo([str(MAW / "bin" / "maw-run"), mod, "--help"], PATH=path)
                self.assertNotIn("SHADOWED", out)


if __name__ == "__main__":
    unittest.main()
