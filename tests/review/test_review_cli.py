"""Tests for review.cli — review-request assembly + ephemeral worktree (item 6)."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import cli  # noqa: E402


def _fake_runner(responses: dict):
    """Map 'git diff' / 'gh pr' prefixes (first 2 tokens) to (rc, stdout)."""
    def run(cmd, cwd=None):
        key = " ".join(cmd[:2])
        rc, out = responses.get(key, (0, ""))
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
    return run


class ChangedFilesTest(unittest.TestCase):
    def test_parses_name_only(self) -> None:
        r = _fake_runner({"git diff": (0, "maw/net/cli.py\ndocs/x.md\n")})
        self.assertEqual(cli.changed_files("main", r), ["maw/net/cli.py", "docs/x.md"])

    def test_empty(self) -> None:
        r = _fake_runner({"git diff": (0, "\n")})
        self.assertEqual(cli.changed_files("main", r), [])


class ResolveRefTest(unittest.TestCase):
    def test_pr_number_uses_gh(self) -> None:
        r = _fake_runner({"gh pr": (0, "abc123def456\n")})
        self.assertEqual(cli.resolve_ref_sha("42", r), "abc123def456")

    def test_ref_uses_git_rev_parse(self) -> None:
        r = _fake_runner({"git rev-parse": (0, "deadbeef\n")})
        self.assertEqual(cli.resolve_ref_sha("feature/x", r), "deadbeef")

    def test_gh_failure_raises(self) -> None:
        r = _fake_runner({"gh pr": (1, "")})
        with self.assertRaises(cli.ReviewError):
            cli.resolve_ref_sha("99", r)


class AssembleRequestTest(unittest.TestCase):
    def test_fills_sha_branch_tier_and_targets_placeholder(self) -> None:
        r = _fake_runner({
            "git rev-parse": (0, "feat/x\n"),  # both rev-parse calls return this; fine for the test
            "git diff": (0, "maw/net/cli.py\n"),
        })
        # rev-parse HEAD and rev-parse --abbrev-ref both hit "git rev-parse"
        text = cli.assemble_request("main", runner=r)
        self.assertIn("REVIEW REQUEST", text)
        self.assertIn("RISK TIER: runtime", text)
        self.assertIn("ADVERSARIAL TARGETS", text)
        self.assertIn("<TARGET 1>", text)
        self.assertIn("RUN `python3 tests/run_all.py`", text)  # runtime → suite required

    def test_docs_only_skips_suite(self) -> None:
        r = _fake_runner({
            "git rev-parse": (0, "docs/x\n"),
            "git diff": (0, "README.md\n"),
        })
        text = cli.assemble_request("main", runner=r)
        self.assertIn("RISK TIER: docs", text)
        self.assertIn("suite skipped", text)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class ReviewConfigTest(unittest.TestCase):
    def _repo(self, text: str | None) -> str:
        return text or ""

    def test_no_config_is_empty(self) -> None:
        self.assertEqual(cli.parse_review_config(''), {})

    def test_review_block(self) -> None:
        r = self._repo('maw:\n  profile: "x"\nreview:\n  suite_cmd: "npx vitest run"  # c\n'
                       '  link: [".venv", \'node_modules\', "vendor/bundle"]\nstack:\n  suite_cmd: "no"\n')
        self.assertEqual(cli.parse_review_config(r), {
            "suite_cmd": "npx vitest run",
            "link": [".venv", "node_modules", "vendor/bundle"]})

    def test_top_level_shorthand_and_no_block(self) -> None:
        r = self._repo("suite_cmd: python3 tests/run_all.py\nmaw:\n  profile: \"x\"\n")
        self.assertEqual(cli.parse_review_config(r), {"suite_cmd": "python3 tests/run_all.py"})
        self.assertEqual(cli.parse_review_config('maw:\n  profile: "x"\n'), {})


    def test_stack_test_command_is_a_fallback_key(self) -> None:
        r = self._repo('stack:\n  test_command: "make check"\n')
        self.assertEqual(cli.parse_review_config(r), {"test_command": "make check"})

    def test_values_are_read_whole(self) -> None:
        P = cli.parse_review_config
        self.assertEqual(P("review:\n  suite_cmd: 'echo \"a # b\" it''s'\n"),
                         {"suite_cmd": 'echo "a # b" it\'s'})
        self.assertEqual(P('review:\n  suite_cmd: "echo \\"x\\" # y"   # trailing\n'),
                         {"suite_cmd": 'echo "x" # y'})
        self.assertEqual(P("review:\n  suite_cmd: npm test   # run\n  link: []\n"),
                         {"suite_cmd": "npm test", "link": []})
        # an empty value is "absent", and foreign keys / nesting are left alone
        self.assertEqual(P('stack:\n  test_command: ""\n  notes: |\n    suite_cmd: x\n'), {})
        self.assertEqual(P("review:\n  other:\n    suite_cmd: nested\n  suite_cmd: real\n"),
                         {"suite_cmd": "real"})
        # review.suite_cmd beats the top-level shorthand in either order
        self.assertEqual(P("suite_cmd: a\nreview:\n  suite_cmd: b\n"), {"suite_cmd": "b"})
        self.assertEqual(P("review:\n  suite_cmd: b\nsuite_cmd: a\n"), {"suite_cmd": "b"})

    def test_unreadable_shapes_are_refused_not_truncated(self) -> None:
        for text, why in (
            ('review:\n  suite_cmd: "a" && rm -rf x\n', "after the closing quote"),
            ('review:\n  suite_cmd: "a"#x\n', "after the closing quote"),
            ("review:\n  suite_cmd: |\n    a\n    b\n", "block scalars"),
            ("review:\n  suite_cmd: >-\n    a\n", "block scalars"),
            ("suite_cmd: |\n  x\n", "block scalars"),
            ("review:\n  suite_cmd:\n    a b\n", "continues on this line"),
            ("review:\n  suite_cmd: a\n    b\n", "continues on this line"),
            ("review:\n  link:\n    - .venv\n", "block-style lists"),
            ("review:\n  link:\n  - .venv\n", "block-style lists"),
            ('review:\n  link: [".venv",\n    "x"]\n', "not closed on this line"),
            ("review:\n  link: .venv\n", "one-line list"),
            ("review:\n  link: [a, b] c\n", "after the list"),
            ("review:\n  link: [a,, b]\n", "empty or unreadable"),
            ("review: {suite_cmd: x}\n", "block mapping"),
            ("{review: {suite_cmd: x}}\n", "flow-style"),
            ('review:\n  "suite_cmd": x\n', "quoted keys"),
            ("review:\n  suite_cmd: a\n  suite_cmd: b\n", "duplicate"),
            ("review:\n  suite_cmd: a\nreview:\n  suite_cmd: b\n", "duplicate"),
            ('review:\n  suite_cmd: echo "a #b"\n', "cut the command short"),
            ("review:\n  suite_cmd: echo a: b\n", "': '"),
            ('review:\n  suite_cmd: "a\\nb"\n', "unsupported escape"),
            ('review:\n  suite_cmd: "abc\n', "unterminated quote"),
            ("review:\n\tsuite_cmd: a\n", "tab in indentation"),
            ('stack:\n  test_command: "a" b\n', "after the closing quote"),
            ("review:\n    link: [a]\n  suite_cmd: x\n", "inconsistent indentation"),
            ("review:\n  <<: *base\n", "merge keys"),
        ):
            with self.assertRaises(cli.ConfigRefused, msg=text) as cm:
                cli.parse_review_config(text)
            self.assertIn(why, str(cm.exception), text)
            self.assertIn("line ", str(cm.exception), text)

    def test_default_links_cover_both_venv_spellings(self) -> None:
        self.assertIn("venv", cli.DEFAULT_LINKS)
        self.assertIn(".venv", cli.DEFAULT_LINKS)


class LinkEnvDirsTest(unittest.TestCase):
    def test_refuses_nested_link_through_a_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
                tempfile.TemporaryDirectory() as outside:
            (Path(a) / "vendor").mkdir()
            (Path(a) / "vendor" / "bundle").mkdir()
            (Path(b) / "vendor").symlink_to(outside)  # committed symlink redirect
            out = io.StringIO()
            got = cli.link_env_dirs(a, b, ["vendor/bundle"], out)
            self.assertEqual(got, [])
            self.assertEqual(os.listdir(outside), [])
            self.assertIn("outside the worktree", out.getvalue())

    def test_refuses_absolute_link_path(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
                tempfile.TemporaryDirectory() as outside:
            out = io.StringIO()
            got = cli.link_env_dirs(a, b, [outside], out)
            self.assertEqual(got, [])
            self.assertEqual(os.listdir(b), [])
            self.assertIn("refusing", out.getvalue())

    def test_odd_existing_symlink_is_a_clean_refusal_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "node_modules").mkdir()
            (Path(b) / "node_modules").symlink_to(Path(b) / "nowhere")  # dangling
            out = io.StringIO()
            got = cli.link_env_dirs(a, b, ["node_modules"], out)
            self.assertEqual(got, [])
            # a path blocked by a plain file in the parent chain also refuses cleanly
            (Path(a) / "vendor").mkdir()
            (Path(a) / "vendor" / "bundle").mkdir()
            (Path(b) / "vendor").write_text("file")
            out = io.StringIO()
            got = cli.link_env_dirs(a, b, ["vendor/bundle"], out)
            self.assertEqual(got, [])
            self.assertIn("refusing", out.getvalue())

    def test_links_existing_skips_missing_tracked_and_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "node_modules").mkdir()
            (Path(a) / ".venv").mkdir()
            (Path(b) / ".venv").mkdir()  # already present in worktree (tracked)
            out = io.StringIO()
            got = cli.link_env_dirs(a, b, ["node_modules", ".venv", "missing", "../x"], out)
            self.assertEqual(got, ["node_modules"])
            self.assertTrue((Path(b) / "node_modules").is_symlink())
            self.assertFalse((Path(b) / ".venv").is_symlink())
            self.assertIn("refusing", out.getvalue())


class ReviewPrWorktreeTest(unittest.TestCase):
    """Real-git: the ephemeral worktree lifecycle (create → suite → auto-clean)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-b", "main", self.repo], capture_output=True, check=True)
        _git(["config", "user.name", "t"], self.repo)
        _git(["config", "user.email", "t@t"], self.repo)
        _git(["config", "commit.gpgsign", "false"], self.repo)
        (Path(self.repo) / "README").write_text("seed\n", encoding="utf-8")
        _git(["add", "README"], self.repo)
        _git(["commit", "-m", "seed"], self.repo)
        self.base = _git(["rev-parse", "HEAD"], self.repo)
        (Path(self.repo) / "code.py").write_text("x = 1\n", encoding="utf-8")
        _git(["add", "code.py"], self.repo)
        _git(["commit", "-m", "add code"], self.repo)
        self.head = _git(["rev-parse", "HEAD"], self.repo)
        self.addCleanup(self.tmp.cleanup)

    def _runner(self):
        def run(cmd, cwd=None):
            return subprocess.run(cmd, cwd=cwd or self.repo, capture_output=True, text=True)
        return run

    def _worktree_count(self) -> int:
        return len([ln for ln in _git(["worktree", "list"], self.repo).splitlines() if ln.strip()])

    def test_creates_runs_and_cleans_up(self) -> None:
        before = self._worktree_count()
        out = io.StringIO()
        rc = cli.review_pr(
            self.head, self.base, suite_cmd=["true"], runner=self._runner(), out=out,
        )
        self.assertEqual(rc, 0)
        self.assertIn("RISK TIER: runtime", out.getvalue())  # code.py changed
        self.assertEqual(self._worktree_count(), before, "ephemeral worktree must be removed")

    def test_links_env_dirs_and_suite_sees_them(self) -> None:
        (Path(self.repo) / "node_modules").mkdir()
        (Path(self.repo) / "node_modules" / "marker").write_text("1", encoding="utf-8")
        out = io.StringIO()
        rc = cli.review_pr(
            self.head, self.base, suite_cmd=cli.suite_argv("test -f node_modules/marker"),
            runner=self._runner(), out=out, link=cli.DEFAULT_LINKS, link_src=self.repo,
        )
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("linked into worktree: node_modules", out.getvalue())
        self.assertTrue((Path(self.repo) / "node_modules" / "marker").exists(), "target survives cleanup")

    def test_no_link_means_suite_cannot_see_env(self) -> None:
        (Path(self.repo) / "node_modules").mkdir()
        rc = cli.review_pr(self.head, self.base, suite_cmd=cli.suite_argv("test -d node_modules"),
                           runner=self._runner(), out=io.StringIO())
        self.assertEqual(rc, 1)

    def test_cmd_pr_uses_config_suite_cmd_and_flag_overrides(self) -> None:
        (Path(self.repo) / ".maw").mkdir()
        (Path(self.repo) / ".maw" / "config.yaml").write_text(
            'review:\n  suite_cmd: "echo CONFIGSUITE"\n', encoding="utf-8")
        _git(["add", ".maw"], self.repo)
        _git(["commit", "-m", "cfg"], self.repo)
        _git(["branch", "cfgbase"], self.repo)
        self.base = "cfgbase"
        import contextlib
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            for argv, want in ((["pr", self.head, "--base", self.base], "CONFIGSUITE"),
                               (["pr", self.head, "--base", self.base, "--suite-cmd", "echo FLAGSUITE"], "FLAGSUITE")):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(argv)
                self.assertEqual(rc, 0, buf.getvalue())
                self.assertIn(want, buf.getvalue())
                self.assertIn(f"suite: RUN echo {want}", buf.getvalue())
        finally:
            os.chdir(old)

    def _pr_via_cli(self, argv, cwd):
        import contextlib
        old = os.getcwd()
        os.chdir(cwd)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = cli.main(argv)
            return rc, buf.getvalue()
        finally:
            os.chdir(old)

    def _commit_cfg(self, text: str, msg: str = "cfg", cwd: "str | None" = None) -> str:
        cwd = cwd or self.repo
        (Path(cwd) / ".maw").mkdir(exist_ok=True)
        (Path(cwd) / ".maw" / "config.yaml").write_text(text, encoding="utf-8")
        _git(["add", ".maw"], cwd)
        _git(["commit", "-m", msg], cwd)
        return _git(["rev-parse", "HEAD"], cwd)

    def test_config_comes_from_the_default_branch_not_the_pr(self) -> None:
        self._commit_cfg('review:\n  suite_cmd: "echo BASESUITE"\n')
        _git(["checkout", "-q", "-b", "pr"], self.repo)
        head = self._commit_cfg('review:\n  suite_cmd: "echo PWNED"\n', "pr weakens suite")
        rc, text = self._pr_via_cli(["pr", head, "--base", "main"], self.repo)
        self.assertEqual(rc, 0, text)
        self.assertIn("suite: RUN echo BASESUITE", text)
        self.assertIn("suite_cmd from refs/heads/main:.maw/config.yaml", text)
        self.assertNotIn("PWNED", text)

    def test_pr_adding_config_cannot_choose_suite_when_default_has_none(self) -> None:
        _git(["checkout", "-q", "-b", "pr"], self.repo)
        (Path(self.repo) / ".secret").mkdir()  # untracked in the main checkout
        self._commit_cfg(
            'review:\n  suite_cmd: "true"\n  link: [".secret"]\nstack:\n  test_command: "true"\n',
            "pr adds config")
        cfg, src = cli.load_default_config(self.repo)
        self.assertEqual(cfg, {})
        self.assertIn("no config on refs/heads/main", src)
        rc, text = self._pr_via_cli(["pr", "HEAD", "--base", "main"], self.repo)
        self.assertIn("suite: RUN python3 tests/run_all.py", text)
        self.assertIn("suite_cmd from default (no config on refs/heads/main)", text)
        self.assertNotIn("RUN true", text)
        self.assertNotIn(".secret", text)
        rc, text = self._pr_via_cli(
            ["pr", "HEAD", "--base", "main", "--suite-cmd", "echo FLAGSUITE"], self.repo)
        self.assertIn("suite: RUN echo FLAGSUITE", text)

    def test_config_read_failure_fails_closed_to_default(self) -> None:
        (Path(self.repo) / ".maw").mkdir()
        (Path(self.repo) / ".maw" / "config.yaml").write_text(
            'review:\n  suite_cmd: "true"\n', encoding="utf-8")  # working tree only
        cfg, src = cli.load_default_config(self.repo)
        self.assertEqual(cfg, {})
        self.assertIn("no config on refs/heads/main", src)

        def boom(argv, cwd=None):
            raise OSError("git unavailable")
        cfg, src = cli.load_default_config(self.repo, runner=boom)
        self.assertEqual(cfg, {})
        self.assertIn("failing closed", src)
        cfg, src = cli.load_default_config(None)
        self.assertEqual(cfg, {})
        self.assertIn("failing closed", src)
        rc, text = self._pr_via_cli(["pr", "HEAD", "--base", "main"], self.repo)
        self.assertNotIn("RUN true", text)

    # -- review round 2026-10-03: unreviewed code must never choose the suite --

    def _origin_and_reviewer(self) -> "tuple[str, str]":
        """Publish self.repo's main to a bare origin and clone it: a real
        remote, so origin/HEAD, refs/remotes/origin/* and tag auto-follow
        behave as they do in a real review. Returns (origin, reviewer clone)."""
        origin = str(Path(self.tmp.name) / "origin.git")
        _git(["clone", "-q", "--bare", self.repo, origin], self.tmp.name)
        clone = str(Path(self.tmp.name) / "reviewer")
        _git(["clone", "-q", origin, clone], self.tmp.name)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], clone)
        return origin, clone

    def _attacker_pr(self, origin: str, *refspecs: str, branch: str = "pr") -> str:
        """In self.repo (the attacker's clone): a branch whose config names a
        hostile suite, pushed with any extra refspecs. Returns its head."""
        _git(["checkout", "-q", "-b", branch], self.repo)
        head = self._commit_cfg('review:\n  suite_cmd: "echo PWNED"\n  link: [".secret"]\n',
                                "pr picks its own suite")
        return head

    def test_tag_shadowing_the_remote_base_cannot_choose_the_suite(self) -> None:
        self._commit_cfg('review:\n  suite_cmd: "echo BASESUITE"\n')
        origin, clone = self._origin_and_reviewer()
        head = self._attacker_pr(origin)
        _git(["tag", "origin/main", head], self.repo)
        _git(["tag", "main", head], self.repo)
        _git(["push", "-q", origin, "pr", "refs/tags/origin/main", "refs/tags/main"], self.repo)
        _git(["fetch", "-q", "origin"], clone)  # a plain fetch auto-follows both tags
        # precondition: the shadow is real here, and the short name reads the PR's config
        short = subprocess.run(["git", "rev-parse", "origin/main"], cwd=clone,
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(short, head, "short origin/main must resolve to the pushed tag")
        shown = subprocess.run(["git", "show", "origin/main:.maw/config.yaml"], cwd=clone,
                               capture_output=True, text=True).stdout
        self.assertIn("PWNED", shown, "the short name reads the PR's config: this is the hole")

        def run(cmd, cwd=None):
            return subprocess.run(cmd, cwd=cwd or clone, capture_output=True, text=True)
        self.assertEqual(cli.resolve_base("main", run), "refs/remotes/origin/main")
        self.assertEqual(cli.resolve_base("origin/main", run), "refs/remotes/origin/main")
        cfg, src = cli.load_default_config(clone)
        self.assertEqual(cfg, {"suite_cmd": "echo BASESUITE"})
        self.assertEqual(src, "refs/remotes/origin/main:.maw/config.yaml")
        for base in ("main", "origin/main"):
            rc, text = self._pr_via_cli(["pr", head, "--base", base], clone)
            self.assertEqual(rc, 0, text)
            self.assertIn("suite: RUN echo BASESUITE", text)
            self.assertIn("suite_cmd from refs/remotes/origin/main:.maw/config.yaml", text)
            self.assertNotIn("PWNED", text)
            # the risk tier is measured against the real base too (the PR touched .maw/)
            self.assertNotIn("RISK TIER: unknown", text)

    def test_branch_shadowing_the_remote_base_cannot_choose_the_suite(self) -> None:
        self._commit_cfg('review:\n  suite_cmd: "echo BASESUITE"\n')
        origin, clone = self._origin_and_reviewer()
        head = self._attacker_pr(origin)
        # remote branches named to look like the base, plus a local branch
        # `origin/main` in the reviewer's clone (refs/heads outranks refs/remotes)
        _git(["push", "-q", origin, "pr", "pr:refs/heads/origin/main",
              "pr:refs/heads/refs/remotes/origin/main"], self.repo)
        _git(["fetch", "-q", "origin"], clone)
        _git(["branch", "origin/main", head], clone)
        short = subprocess.run(["git", "rev-parse", "origin/main"], cwd=clone,
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(short, head, "short origin/main must resolve to the local branch")
        for base in ("main", "origin/main"):
            rc, text = self._pr_via_cli(["pr", head, "--base", base], clone)
            self.assertEqual(rc, 0, text)
            self.assertIn("suite: RUN echo BASESUITE", text)
            self.assertNotIn("PWNED", text)

    def test_non_default_base_never_supplies_config(self) -> None:
        # A stacked review: --base is itself an unreviewed branch carrying a config.
        self._commit_cfg('review:\n  suite_cmd: "echo BASESUITE"\n')
        origin, clone = self._origin_and_reviewer()
        self._attacker_pr(origin, branch="stack1")
        _git(["checkout", "-q", "-b", "stack2"], self.repo)
        (Path(self.repo) / "more.py").write_text("y = 2\n", encoding="utf-8")
        _git(["add", "more.py"], self.repo)
        _git(["commit", "-m", "stacked work"], self.repo)
        head = _git(["rev-parse", "HEAD"], self.repo)
        _git(["push", "-q", origin, "stack1", "stack2"], self.repo)
        _git(["fetch", "-q", "origin"], clone)
        for base in ("stack1", "origin/stack1", "refs/remotes/origin/stack1"):
            rc, text = self._pr_via_cli(["pr", head, "--base", base], clone)
            self.assertEqual(rc, 0, text)
            self.assertIn("suite: RUN echo BASESUITE", text)
            self.assertNotIn("PWNED", text)
            self.assertNotIn(".secret", text)
            self.assertIn(f"--base {base} is not the default branch", text)
            self.assertIn("read from refs/remotes/origin/main:.maw/config.yaml only", text)
        # the default base prints no such note
        rc, text = self._pr_via_cli(["pr", head, "--base", "main"], clone)
        self.assertNotIn("is not the default branch", text)

    def test_unknown_default_branch_refuses_config_and_says_why(self) -> None:
        self._commit_cfg('review:\n  suite_cmd: "echo BASESUITE"\n')
        origin, clone = self._origin_and_reviewer()
        _git(["remote", "set-head", "origin", "-d"], clone)  # origin exists, origin/HEAD does not
        cfg, src = cli.load_default_config(clone)
        self.assertEqual(cfg, {})
        self.assertIn("origin/HEAD is not set", src)
        rc, text = self._pr_via_cli(["pr", "HEAD", "--base", "main"], clone)
        self.assertIn("suite: RUN python3 tests/run_all.py", text)
        self.assertIn("config not read: origin/HEAD is not set", text)
        self.assertNotIn("BASESUITE", text)

    def test_malformed_default_config_refuses_with_a_reason(self) -> None:
        self._commit_cfg('review:\n  suite_cmd: "echo OK" && echo TAIL\n')
        with self.assertRaises(cli.ConfigRefused):
            cli.load_default_config(self.repo)
        before = self._worktree_count()
        rc, text = self._pr_via_cli(["pr", "HEAD", "--base", "main"], self.repo)
        self.assertEqual(rc, 1, text)
        self.assertIn("review REFUSED: refs/heads/main:.maw/config.yaml refused: line 2", text)
        self.assertIn("after the closing quote", text)
        self.assertNotIn("suite: RUN", text)  # nothing ran
        self.assertEqual(self._worktree_count(), before)
        # naming the suite yourself still works, and the refusal is still printed
        rc, text = self._pr_via_cli(
            ["pr", "HEAD", "--base", "main", "--suite-cmd", "echo FLAGSUITE"], self.repo)
        self.assertEqual(rc, 0, text)
        self.assertIn("config ignored", text)
        self.assertIn("suite: RUN echo FLAGSUITE", text)

    def test_stack_test_command_fallback_from_base(self) -> None:
        (Path(self.repo) / ".maw").mkdir()
        (Path(self.repo) / ".maw" / "config.yaml").write_text(
            'stack:\n  test_command: "echo STACKCMD"\n', encoding="utf-8")
        _git(["add", ".maw"], self.repo)
        _git(["commit", "-m", "cfg"], self.repo)
        _git(["branch", "basebr"], self.repo)
        rc, text = self._pr_via_cli(["pr", "HEAD", "--base", "basebr"], self.repo)
        self.assertEqual(rc, 0, text)
        self.assertIn("suite: RUN echo STACKCMD", text)
        self.assertIn("stack.test_command from", text)

    def test_link_src_is_main_checkout_from_a_linked_worktree(self) -> None:
        (Path(self.repo) / ".venv").mkdir()
        (Path(self.repo) / ".venv" / "marker").write_text("1", encoding="utf-8")
        seat = str(Path(self.tmp.name) / "seat")
        _git(["worktree", "add", "--detach", seat, self.head], self.repo)
        rc, text = self._pr_via_cli(
            ["pr", self.head, "--base", self.base, "--suite-cmd", "test -f .venv/marker"], seat)
        self.assertEqual(rc, 0, text)
        self.assertIn("linked into worktree: .venv", text)

    def test_suite_failure_still_cleans_up(self) -> None:
        before = self._worktree_count()
        rc = cli.review_pr(
            self.head, self.base, suite_cmd=["false"], runner=self._runner(), out=io.StringIO(),
        )
        self.assertEqual(rc, 1)  # suite exit code passed through
        self.assertEqual(self._worktree_count(), before, "cleanup must run even on suite failure")

    def _no_review_tempdir_leak(self, run_fn) -> None:
        """Run run_fn with tempfile.tempdir redirected; assert no maw-review-* left."""
        import glob
        import shutil
        import tempfile as tf
        base = tf.mkdtemp()
        old = tf.tempdir
        tf.tempdir = base
        try:
            run_fn()
            self.assertEqual(
                glob.glob(str(Path(base) / "maw-review-*")), [],
                "review_pr must not leak its temp parent dir (advisor Note 1)",
            )
        finally:
            tf.tempdir = old
            shutil.rmtree(base, ignore_errors=True)

    def test_no_tempdir_leak_on_success(self) -> None:
        self._no_review_tempdir_leak(lambda: cli.review_pr(
            self.head, self.base, suite_cmd=["true"], runner=self._runner(), out=io.StringIO(),
        ))

    def test_no_tempdir_leak_on_setup_failure(self) -> None:
        # worktree add fails AFTER the temp dir is created → the parent must
        # still be cleaned (the dir is created inside the `with`).
        def runner(cmd, cwd=None):
            if cmd[:3] == ["git", "worktree", "add"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="add boom")
            return subprocess.run(cmd, cwd=cwd or self.repo, capture_output=True, text=True)

        def run():
            with self.assertRaises(cli.ReviewError):
                cli.review_pr(self.head, self.base, suite_cmd=["true"], runner=runner, out=io.StringIO())
        self._no_review_tempdir_leak(run)


class AiAttributionTest(unittest.TestCase):
    """Commit messages in base..head carrying AI attribution block the review."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "r")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-b", "main", self.repo], capture_output=True, check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], self.repo)
        self._n = 0
        self.base = self._commit("seed")

    def _commit(self, msg: str) -> str:
        self._n += 1
        (Path(self.repo) / f"f{self._n}.py").write_text(f"x = {self._n}\n", encoding="utf-8")
        _git(["add", "."], self.repo)
        _git(["commit", "-m", msg], self.repo)
        return _git(["rev-parse", "HEAD"], self.repo)

    def _runner(self):
        def run(cmd, cwd=None):
            return subprocess.run(cmd, cwd=cwd or self.repo, capture_output=True, text=True)
        return run

    def _findings(self):
        return cli.ai_attribution_findings(self.base, "HEAD", self._runner())

    def test_clean_history_has_no_findings(self) -> None:
        self._commit("feat: add thing\n\nRefs #1. Mentions Claude Code plugin loading in prose.")
        self.assertEqual(self._findings(), [])

    def test_co_authored_by_claude_flagged_with_sha(self) -> None:
        sha = self._commit("fix: x\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
        f = self._findings()
        self.assertEqual([x[0] for x in f], [sha])
        self.assertIn("trailer", f[0][1])

    def test_human_co_author_not_flagged(self) -> None:
        self._commit("fix: x\n\nCo-authored-by: advisor2 <advisor@maw.local>")
        self.assertEqual(self._findings(), [])

    def test_generated_with_and_session_trailer_flagged(self) -> None:
        a = self._commit("docs: y\n\n\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)")
        b = self._commit("docs: z\n\nClaude-Session: https://claude.ai/code/session_01abc")
        self.assertEqual({x[0] for x in self._findings()}, {a, b})

    def test_commits_before_base_ignored(self) -> None:
        old = self._commit("old\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        self.base = old
        self._commit("clean")
        self.assertEqual(self._findings(), [])

    def test_trailer_shapes_naming_ai_flagged(self) -> None:
        for i, trailer in enumerate([
            "Assisted-by: Claude Code",
            "Co-developed-by: Copilot <copilot@github.com>",
            "Signed-off-by: ChatGPT <noreply@openai.com>",
            "Generated-by: Cursor Agent",
            "Co-authored-by: Gemini <x@y.z>",
            "Reviewed-by: Bot <noreply@anthropic.com>",
        ]):
            sha = self._commit(f"fix: t{i}\n\n{trailer}")
            self.assertIn(sha, [x[0] for x in self._findings()], trailer)

    def test_human_names_and_lookalike_domains_not_flagged(self) -> None:
        for i, trailer in enumerate([
            "Co-authored-by: Ai Nguyen <ai@example.com>",
            "Signed-off-by: Dana <dana@openai-fans.org>",
            "Reviewed-by: Sam Lee <sam@notanthropic.com.example.org>",
        ]):
            self._commit(f"fix: h{i}\n\n{trailer}")
        self.assertEqual(self._findings(), [])

    def test_generated_line_needs_ai_tool_or_robot(self) -> None:
        self._commit("docs: a\n\nGenerated by the build script from schema.json")
        self._commit("docs: b\n\nGenerated with protoc 25")
        self.assertEqual(self._findings(), [])
        a = self._commit("docs: c\n\nGenerated with ChatGPT")
        b = self._commit("docs: d\n\nGenerated by Codex")
        c = self._commit("docs: e\n\n\U0001F916 Generated with tooling")
        self.assertEqual({x[0] for x in self._findings()}, {a, b, c})

    def test_stale_local_base_vs_origin_base_no_finding(self) -> None:
        # origin/main moves ahead with a commit carrying attribution (already
        # merged upstream); local main stays stale. The PR branch is clean.
        _git(["checkout", "-q", "-b", "pr"], self.repo)
        self._commit("feat: clean pr work")
        _git(["checkout", "-q", "main"], self.repo)
        merged = self._commit("old merged\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        _git(["update-ref", "refs/remotes/origin/main", merged], self.repo)
        _git(["reset", "-q", "--hard", self.base], self.repo)  # local main stale
        _git(["checkout", "-q", "pr"], self.repo)
        _git(["merge", "-q", "--no-edit", merged], self.repo)  # PR contains merged history
        self.assertEqual(cli.ai_attribution_findings("main", "HEAD", self._runner()), [])
        # control: against stale local main the old commit WOULD be swept in
        out = _git(["log", "--format=%H", f"main..HEAD"], self.repo)
        self.assertIn(merged, out)

    def test_review_pr_blocks_even_when_suite_passes(self) -> None:
        sha = self._commit("fix: q\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        out = io.StringIO()
        rc = cli.review_pr(sha, self.base, suite_cmd=["true"], runner=self._runner(), out=out)
        self.assertEqual(rc, cli.ATTRIBUTION_EXIT)
        self.assertIn("BLOCKING: AI attribution", out.getvalue())
        self.assertIn(sha[:12], out.getvalue())

    def test_review_pr_clean_passes(self) -> None:
        sha = self._commit("fix: q")
        rc = cli.review_pr(sha, self.base, suite_cmd=["true"], runner=self._runner(), out=io.StringIO())
        self.assertEqual(rc, 0)

    def test_suite_failure_code_wins(self) -> None:
        sha = self._commit("fix: q\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        rc = cli.review_pr(sha, self.base, suite_cmd=["false"], runner=self._runner(), out=io.StringIO())
        self.assertEqual(rc, 1)

    # -- review round 2026-10-03 -------------------------------------------

    def _commit_file_msg(self, msg: str) -> str:
        """Commit with the message read from a file (large / odd-byte messages)."""
        self._n += 1
        (Path(self.repo) / f"f{self._n}.py").write_text(f"x = {self._n}\n", encoding="utf-8")
        mf = Path(self.tmp.name) / f"msg{self._n}"
        mf.write_text(msg, encoding="utf-8")
        _git(["add", "."], self.repo)
        _git(["commit", "--cleanup=verbatim", "-F", str(mf)], self.repo)
        return _git(["rev-parse", "HEAD"], self.repo)

    def _clone_with_origin(self) -> "tuple[str, str]":
        """(bare origin, reviewer clone): a real remote, so refs/remotes/origin/*
        and tag auto-follow behave as they do in a real review."""
        origin = os.path.join(self.tmp.name, "origin.git")
        _git(["clone", "-q", "--bare", self.repo, origin], self.tmp.name)
        clone = os.path.join(self.tmp.name, "reviewer")
        _git(["clone", "-q", origin, clone], self.tmp.name)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _git(["config", k, v], clone)
        return origin, clone

    def test_tag_shadowing_the_remote_base_cannot_empty_the_range(self) -> None:
        # Anyone with push access pushes a TAG named `origin/main` at their PR
        # head. A plain fetch auto-follows it, and git resolves the short name
        # `origin/main` to the tag before the remote-tracking ref.
        origin, clone = self._clone_with_origin()
        _git(["checkout", "-q", "-b", "pr"], self.repo)
        bad = self._commit("fix: sneaky\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        _git(["tag", "origin/main", bad], self.repo)
        _git(["push", "-q", origin, "pr", "refs/tags/origin/main"], self.repo)
        _git(["fetch", "-q", "origin"], clone)  # plain fetch: the tag follows

        def run(cmd, cwd=None):
            return subprocess.run(cmd, cwd=cwd or clone, capture_output=True, text=True)

        # precondition: the shadow is real in this repo (else the test proves nothing)
        self.assertEqual(_git(["rev-parse", "refs/tags/origin/main"], clone), bad)
        short = subprocess.run(["git", "rev-parse", "origin/main"], cwd=clone,
                               capture_output=True, text=True)
        self.assertEqual(short.stdout.strip(), bad, "short name must resolve to the tag")
        self.assertEqual(_git(["log", "--format=%H", "origin/main..origin/pr"], clone), "",
                         "the short-name range is empty: this is the hole")

        self.assertEqual(cli.resolve_base("main", run), "refs/remotes/origin/main")
        self.assertEqual(cli.resolve_base("origin/main", run), "refs/remotes/origin/main")
        for base in ("main", "origin/main"):
            found = cli.ai_attribution_findings(base, "origin/pr", run)
            self.assertEqual([f[0] for f in found], [bad], base)
        out = io.StringIO()
        rc = cli.review_pr(bad, "main", suite_cmd=["true"], runner=run, out=out)
        self.assertEqual(rc, cli.ATTRIBUTION_EXIT, out.getvalue())

    def test_tag_shadowing_a_local_only_base(self) -> None:
        # No remote: a tag named `main` still outranks refs/heads/main.
        _git(["checkout", "-q", "-b", "pr"], self.repo)
        bad = self._commit("fix: sneaky\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        _git(["tag", "main", bad], self.repo)
        self.assertEqual(cli.resolve_base("main", self._runner()), "refs/heads/main")
        found = cli.ai_attribution_findings("main", "HEAD", self._runner())
        self.assertEqual([f[0] for f in found], [bad])

    def test_resolve_base_leaves_non_branch_names_alone(self) -> None:
        r = self._runner()
        self.assertEqual(cli.resolve_base(self.base, r), self.base)        # a SHA
        self.assertEqual(cli.resolve_base("HEAD~0", r), "HEAD~0")
        self.assertEqual(cli.resolve_base("refs/heads/main", r), "refs/heads/main")
        self.assertEqual(cli.resolve_base("", r), "")

    def test_space_before_the_colon_is_still_a_trailer(self) -> None:
        a = self._commit("fix: a\n\nCo-Authored-By : Claude <noreply@anthropic.com>")
        b = self._commit("fix: b\n\nClaude-Session : abc")
        self.assertEqual({f[0] for f in self._findings()}, {a, b})

    def test_folded_trailer_is_unfolded(self) -> None:
        a = self._commit("fix: a\n\nCo-Authored-By:\n Claude Opus")
        b = self._commit("fix: b\n\nCo-authored-by: Some\n\tBody <noreply@anthropic.com>")
        c = self._commit("fix: c\n\nSigned-off-by: Dana\n  Lee <dana@example.com>")  # human, folded
        got = {f[0] for f in self._findings()}
        self.assertEqual(got, {a, b})
        self.assertNotIn(c, got)

    def test_record_separator_byte_cannot_hide_later_content(self) -> None:
        a = self._commit_file_msg(
            "fix: a\n\nprose \x1e more prose\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
        b = self._commit_file_msg("fix: b \x1f\x1e\n\nClaude-Session: https://claude.ai/code/session_1\n")
        self.assertEqual({f[0] for f in self._findings()}, {a, b})

    def test_overlong_lines_are_capped_and_still_scanned(self) -> None:
        import time
        hostile = [
            "Co-authored-by: x " + "<" * 400_000,
            "generated " * 40_000,
            "Co-authored-by: " + "a@" * 200_000,
        ]
        t0 = time.monotonic()
        for line in hostile:
            self.assertIsNone(cli.attribution_rule(line))
        self.assertLess(time.monotonic() - t0, 5.0, "line scan must stay near-linear")
        pad = "x" * (cli.MAX_LINE * 3)
        for line in (
            f"Co-authored-by: {pad} Claude",
            f"Co-authored-by: {pad} <noreply@anthropic.com>",
            f"{pad} https://claude.ai/code/session_01abc",
            f"{pad} [Claude Code](https://claude.com/claude-code)",
            " " * (cli.MAX_LINE * 2) + "Co-Authored-By: Claude <noreply@anthropic.com>",
        ):
            self.assertIsNotNone(cli.attribution_rule(line), line[-60:])
        self.assertIsNone(cli.attribution_rule(f"prose {pad} mentions Claude Code in passing"))
        sha = self._commit_file_msg(f"fix: long\n\nCo-authored-by: {pad} Claude\n")
        self.assertEqual([f[0] for f in self._findings()], [sha])
        self.assertLessEqual(len(self._findings()[0][2]), 200)

    def test_false_positive_has_a_reviewer_override(self) -> None:
        human = self._commit("fix: a\n\nCo-authored-by: Claude Monet <claude@example.org>")
        quoted = self._commit('docs: b\n\nThe harness appends this footer:\nClaude-Session: <url>')
        real = self._commit("fix: c\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        self.assertEqual({f[0] for f in self._findings()}, {human, quoted, real})
        r = self._runner()
        got = cli.ai_attribution_findings(self.base, "HEAD", r, allow=[human, quoted[:7]])
        self.assertEqual([f[0] for f in got], [real])
        # a too-short prefix is not an override
        got = cli.ai_attribution_findings(self.base, "HEAD", r, allow=[human[:4], ""])
        self.assertEqual({f[0] for f in got}, {human, quoted, real})
        # review_pr: the override is printed, and the un-allowed commit still blocks
        out = io.StringIO()
        rc = cli.review_pr("HEAD", self.base, suite_cmd=["true"], runner=r, out=out,
                           allow_attribution=[human, quoted])
        self.assertEqual(rc, cli.ATTRIBUTION_EXIT)
        self.assertIn("ALLOWED by --allow-attribution", out.getvalue())
        self.assertNotIn(human[:12] + "  ", out.getvalue())
        out = io.StringIO()
        rc = cli.review_pr("HEAD", self.base, suite_cmd=["true"], runner=r, out=out,
                           allow_attribution=[human, quoted, real])
        self.assertEqual(rc, 0, out.getvalue())

    def test_commits_cli_allow_flag(self) -> None:
        import contextlib
        human = self._commit("fix: a\n\nCo-authored-by: Claude Monet <claude@example.org>")
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            for extra, want_rc in (([], cli.ATTRIBUTION_EXIT), (["--allow-attribution", human], 0)):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["commits", "--base", self.base, *extra])
                self.assertEqual(rc, want_rc, buf.getvalue())
            self.assertIn("ALLOWED by --allow-attribution", buf.getvalue())
        finally:
            os.chdir(old)


class LintRequestTest(unittest.TestCase):
    """C3: a review request that names no adversarial target is refused."""

    def lint(self, text: str):
        from review.cli import lint_request
        return lint_request(text)

    def test_the_unfilled_template_is_refused(self) -> None:
        from review.cli import assemble_request
        tmpl = ("REVIEW REQUEST\nSHA: abc\n\nADVERSARIAL TARGETS (REQUIRED - only you can state these):\n"
                "  - <TARGET 1>\n  - <TARGET 2>\n")
        n, problems = self.lint(tmpl)
        self.assertEqual(n, 0)
        self.assertTrue(problems)

    def test_missing_section_is_refused(self) -> None:
        n, problems = self.lint("REVIEW REQUEST: PR #9 @ abc. Please review.")
        self.assertEqual(n, 0)
        self.assertIn("no ADVERSARIAL TARGETS section", problems[0])

    def test_empty_words_are_refused(self) -> None:
        for body in ("- none", "- TBD", "- n/a", "1. todo", "- race"):
            n, problems = self.lint("Adversarial targets:\n" + body + "\n")
            self.assertEqual(n, 0, body)
            self.assertTrue(problems, body)

    def test_named_targets_pass_in_every_list_style(self) -> None:
        text = ("REVIEW REQUEST: PR #219 @ 2b49021.\n"
                "Adversarial targets:\n"
                "1. token hijack via re-register by a recycled pid\n"
                "2) the +1s slack between lstart and started_at\n"
                "- lstart parsing on a non-English locale\n"
                "  (continuation line of item 3)\n"
                "* started_at semantics across same-pid re-registers\n"
                "Next: the D-fixes.\n"
                "- this item is after the section and must not count\n")
        n, problems = self.lint(text)
        self.assertEqual(problems, [])
        self.assertEqual(n, 4)

    def test_inline_target_on_the_heading_line_counts(self) -> None:
        n, problems = self.lint("Adversarial target: the precheck refusal path under rc 3\n")
        self.assertEqual((n, problems), (1, []))

    def test_cli_exit_codes(self) -> None:
        import io
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from review import cli
        with tempfile.TemporaryDirectory() as t:
            good, bad = Path(t) / "good.txt", Path(t) / "bad.txt"
            good.write_text("Adversarial targets:\n- races between two concurrent registers\n")
            bad.write_text("Adversarial targets:\n- <TARGET 1>\n")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(cli.main(["lint", str(good)]), 0)
                self.assertEqual(cli.main(["lint", str(bad)]), 2)
            self.assertIn("REFUSED", out.getvalue())

    def test_real_review_requests_from_this_run_pass(self) -> None:
        # The requests actually sent on 2026-09-22 must not be refused.
        for name in ("rr219.txt", "rr221.txt", "rrB.txt", "rrC0.txt"):
            p = Path(__file__).parent / "fixtures" / name
            n, problems = self.lint(p.read_text())
            self.assertEqual(problems, [], name)
            self.assertGreaterEqual(n, 4, name)


class SuiteCmdPipefailTest(unittest.TestCase):
    """Lane D row 16: a failing stage in a piped --suite-cmd must fail the review,
    a quoted command must survive, and the label must name what actually ran."""

    def run_cmd(self, cmd: str) -> int:
        return subprocess.run(cli.suite_argv(cmd), capture_output=True, text=True, timeout=60).returncode

    def test_a_failing_stage_before_a_pipe_is_not_exit_zero(self) -> None:
        self.assertNotEqual(self.run_cmd("false | cat"), 0)
        self.assertNotEqual(self.run_cmd("sh -c 'exit 3' | tail -1"), 0)
        self.assertEqual(self.run_cmd("true | cat"), 0)

    def test_a_quoted_command_runs_as_typed(self) -> None:
        self.assertEqual(self.run_cmd("sh -c 'exit 3'"), 3)

    def test_label_names_the_suite_that_ran(self) -> None:
        gate = cli.recommend("unknown")
        self.assertIn("RUN pytest -q | tail -3", cli._gate_block(gate, "pytest -q | tail -3"))
        self.assertIn("RUN python3 tests/run_all.py", cli._gate_block(gate))


if __name__ == "__main__":
    unittest.main()
