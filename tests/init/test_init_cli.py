"""Tests for /maw:init CLI — contract §10 test surface.

Covers state detection, substitution, drift detection, --force, --dry-run,
error paths, and idempotency. All stdlib; no third-party YAML.

The CLI is invoked via cli.main(argv=[...]) against a tempdir, with
MAW_BRIDGE_HOME pointed inside the tempdir so the bridge-slug marker
mechanism is sandboxed. _is_maw_repo's only signal is the umbrella
marketplace.json sentinel at the target root — tempdirs don't have one,
so init proceeds; test_marketplace_sentinel_triggers_refusal writes one
and asserts the refusal fires.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from init import cli  # noqa: E402
from init.install import (  # noqa: E402
    ACTION_INSTALL,
    ACTION_OVERWRITE_WITH_BACKUP,
    ACTION_PLANNED,
    ACTION_PLANNED_OVERWRITE,
    ACTION_PLANNED_RECOMMENDED,
    ACTION_SKIP_CURRENT,
    ACTION_WROTE_RECOMMENDED,
    template_to_target_relpath,
)
from init.state import SCHEMA_VERSION, read_state, sha256_file  # noqa: E402

EXIT_OK = cli.EXIT_OK
EXIT_REFUSE = cli.EXIT_REFUSE
EXIT_PARSE = cli.EXIT_PARSE
EXIT_PERMISSION = cli.EXIT_PERMISSION
EXIT_IO = cli.EXIT_IO


def run_init(argv: list[str], *, repo_root: Path) -> tuple[int, str, str]:
    """Invoke cli.main; return (exit_code, stdout, stderr).

    Always sandboxes MAW_BRIDGE_HOME inside the tempdir so the cli's
    home-touching code does not modify the host filesystem.
    """
    os.environ["MAW_BRIDGE_HOME"] = str(repo_root / "_test_bridge_home")
    out = io.StringIO()
    err = io.StringIO()
    full_argv = ["--repo-root", str(repo_root), *argv]
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(full_argv)
    return rc, out.getvalue(), err.getvalue()


class CLITestBase(unittest.TestCase):
    """Common fixture: tempdir + .git dir, with MAW_BRIDGE_HOME sandboxed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tmp.name)
        # Default: make it look like a git repo so the §1 prerequisite passes.
        (self.repo_root / ".git").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def install_fresh(self, *extra_argv: str) -> tuple[int, str, str]:
        return run_init(["--profile", "generic", *extra_argv], repo_root=self.repo_root)


# ---------------------------------------------------------------------------
# State detection (§2)
# ---------------------------------------------------------------------------

class StateDetectionTests(CLITestBase):

    def test_fresh_repo_installs_full_substrate(self):
        rc, out, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertTrue((self.repo_root / ".maw" / "config.yaml").is_file())
        self.assertTrue((self.repo_root / ".maw" / "state.json").is_file())
        # Every file the generic profile lists must exist on disk.
        # post-merge is the plugin SOURCE repo's deploy hook: a no-op anywhere
        # else, so no profile installs it into a target repo.
        self.assertFalse((self.repo_root / ".git" / "hooks" / "post-merge").exists())
        for rel in (
            ".git/hooks/pre-push",
            ".claude/schemas/handoff.json",
        ):
            self.assertTrue(
                (self.repo_root / rel).is_file(),
                f"missing expected install file: {rel}",
            )
        # hooks: [] on every profile now — repo-local hook scripts are
        # opt-in and must NOT land in .claude/hooks/ by default.
        hooks_dir = self.repo_root / ".claude" / "hooks"
        if hooks_dir.exists():
            self.assertEqual(
                list(hooks_dir.iterdir()), [],
                "no hook files should be installed by default (hooks: [] profile)",
            )

    def test_state_json_written_last(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        state = read_state(self.repo_root)
        self.assertIsNotNone(state)
        # state.json's recorded SHA must match the file on disk for each entry
        for rel, entry in state.files.items():
            disk_sha = sha256_file(self.repo_root / rel)
            self.assertEqual(disk_sha, entry.installed_sha256)

    def test_rerun_no_drift_is_no_op(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("skipped=2", out)
        self.assertIn("installed=0", out)

    def test_rerun_with_drift_writes_recommended_sibling(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        # Drift one file by appending content (sha changes; the test never
        # parses the JSON so this is a valid drift trigger).
        handoff = self.repo_root / ".claude" / "schemas" / "handoff.json"
        handoff.write_text(handoff.read_text() + "# The operator edit\n")
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        sibling = self.repo_root / ".claude" / "schemas" / "handoff.json.maw-recommended"
        self.assertTrue(sibling.is_file())
        # Original file preserved.
        self.assertIn("# The operator edit", handoff.read_text())
        self.assertIn("drifted=1", out)

    def test_a_current_sibling_is_not_rewritten_every_run(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        # The CoS's user-test of #260: init rewrote unchanged siblings every
        # run and reported drifted=N forever.
        handoff = self.repo_root / ".claude" / "schemas" / "handoff.json"
        handoff.write_text(handoff.read_text() + "# The operator edit\n")
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("drifted=1 kept-drift=0", out)
        sibling = handoff.with_name("handoff.json.maw-recommended")
        before = sibling.stat()
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        after = sibling.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns),
                         "an already-current sibling was rewritten")
        self.assertIn("drifted=0 kept-drift=1", out)
        self.assertIn("[skip-preserving-drift] .claude/schemas/handoff.json.maw-recommended", out)
        self.assertIn("# The operator edit", handoff.read_text())

    def test_a_stale_sibling_is_refreshed(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        handoff = self.repo_root / ".claude" / "schemas" / "handoff.json"
        handoff.write_text(handoff.read_text() + "# The operator edit\n")
        sibling = handoff.with_name("handoff.json.maw-recommended")
        sibling.write_text("an old maw version\n")
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("drifted=1 kept-drift=0", out)
        self.assertNotEqual(sibling.read_text(), "an old maw version\n")

    def test_an_existing_claude_dir_without_config_is_adopted_untouched(self):
        # The normal first-run state of a repo that already uses Claude Code.
        claude = self.repo_root / ".claude"
        (claude / "skills" / "mine").mkdir(parents=True)
        settings = claude / "settings.json"
        skill = claude / "skills" / "mine" / "SKILL.md"
        settings.write_bytes(b'{"permissions": {"allow": ["Bash(ls:*)"]}}\n')
        skill.write_bytes(b"---\nname: mine\n---\nmy skill\n")
        before = (settings.read_bytes(), skill.read_bytes())
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertEqual((settings.read_bytes(), skill.read_bytes()), before)
        self.assertIn("adopting an existing .claude/", err)
        self.assertIn("added: ", err)
        self.assertIn(".claude/schemas/handoff.json", err)

    def test_adoption_keeps_a_differing_file_and_says_so(self):
        schema = self.repo_root / ".claude" / "schemas" / "handoff.json"
        schema.parent.mkdir(parents=True)
        schema.write_text('{"mine": true}\n')
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertEqual(schema.read_text(), '{"mine": true}\n')
        self.assertTrue(schema.with_name("handoff.json.maw-recommended").is_file())
        kept_line = [l for l in err.splitlines() if "kept yours" in l][0]
        self.assertIn(".claude/schemas/handoff.json", kept_line)

    def test_adoption_dry_run_shows_the_plan_and_writes_nothing(self):
        claude = self.repo_root / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text("{}\n")
        rc, out, err = run_init(["--dry-run"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("would add: ", err)
        self.assertEqual(sorted(p.name for p in claude.iterdir()), ["settings.json"])
        self.assertFalse((self.repo_root / ".maw" / "config.yaml").exists())

    def test_adoption_never_writes_through_a_symlinked_directory(self):
        # Security review on adoption: a pre-existing .claude/schemas that is a
        # symlink must not carry maw's write outside the repo.
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (self.repo_root / ".claude").mkdir()
        (self.repo_root / ".claude" / "schemas").symlink_to(outside)
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(os.listdir(outside), [], "maw wrote outside the repo")
        self.assertIn("symlink", err + out)

    def _outside_dir(self) -> Path:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        return outside

    def test_force_never_replaces_a_symlinked_file(self):
        # A drifted target that is a symlink. Without --force init only writes a
        # sibling and never reaches the link, so the old version of this test
        # passed with the guard deleted. --force is the path that would replace
        # it (attic copy through the link, then os.replace over it).
        outside = self._outside_dir()
        target = outside / "theirs.json"
        target.write_text("theirs\n")
        (self.repo_root / ".claude" / "schemas").mkdir(parents=True)
        link = self.repo_root / ".claude" / "schemas" / "handoff.json"
        link.symlink_to(target)
        rc, out, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertTrue(link.is_symlink(), "the user's symlink was replaced")
        self.assertEqual(target.read_text(), "theirs\n")
        self.assertEqual(sorted(os.listdir(outside)), ["theirs.json"])
        self.assertIn("symlink", err)

    def test_never_replaces_a_dangling_symlinked_file(self):
        # A dangling link does not exist(), so it looks like a fresh install.
        outside = self._outside_dir()
        (self.repo_root / ".claude" / "schemas").mkdir(parents=True)
        link = self.repo_root / ".claude" / "schemas" / "handoff.json"
        link.symlink_to(outside / "not-yet.json")
        run_init([], repo_root=self.repo_root)
        self.assertTrue(link.is_symlink(), "the user's symlink was replaced")
        self.assertEqual(os.listdir(outside), [])

    def test_never_writes_a_sibling_through_a_symlink(self):
        outside = self._outside_dir()
        theirs = outside / "rec.json"
        theirs.write_text("theirs\n")
        schemas = self.repo_root / ".claude" / "schemas"
        schemas.mkdir(parents=True)
        (schemas / "handoff.json").write_text("{\"edited\": true}\n")
        sibling = schemas / "handoff.json.maw-recommended"
        sibling.symlink_to(theirs)
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertTrue(sibling.is_symlink(), "the sibling symlink was replaced")
        self.assertEqual(theirs.read_text(), "theirs\n")
        self.assertIn("symlink", err)

    def test_force_never_backs_up_through_a_symlinked_attic(self):
        # .claude/maw -> outside: the --force backup would copy the user's file
        # out of the repo. Refuse the overwrite instead; no backup, no write.
        outside = self._outside_dir()
        schemas = self.repo_root / ".claude" / "schemas"
        schemas.mkdir(parents=True)
        mine = schemas / "handoff.json"
        mine.write_text("{\"edited\": true}\n")
        (self.repo_root / ".claude" / "maw").symlink_to(outside)
        rc, out, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertEqual(os.listdir(outside), [], "maw backed up outside the repo")
        self.assertEqual(mine.read_text(), "{\"edited\": true}\n")
        self.assertIn("symlink", err)

    def test_refuses_a_symlinked_maw_dir(self):
        # .maw -> outside would land config.yaml and state.json there.
        outside = self._outside_dir()
        (self.repo_root / ".maw").symlink_to(outside)
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_REFUSE, msg=err)
        self.assertEqual(os.listdir(outside), [], "maw wrote outside the repo")
        self.assertIn(".maw/config.yaml", err)
        # Refused up front: no template landed either.
        self.assertFalse((self.repo_root / ".claude" / "schemas").exists())

    def test_refuses_a_symlinked_gitignore(self):
        outside = self._outside_dir()
        theirs = outside / "gitignore"
        theirs.write_text("node_modules/\n")
        gi = self.repo_root / ".gitignore"
        gi.symlink_to(theirs)
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_REFUSE, msg=err)
        self.assertTrue(gi.is_symlink(), "the user's .gitignore link was replaced")
        self.assertEqual(theirs.read_text(), "node_modules/\n")

    def test_a_kept_pre_push_says_the_push_gate_is_off(self):
        # The CoS's user-test of #260: an adopted repo kept its own pre-push,
        # and the banner still told the user to add MAW_HUMAN=1 as if the gate
        # were on.
        hook = self.repo_root / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho mine\n")
        (self.repo_root / ".claude").mkdir()
        for argv in (["--dry-run"], []):
            with self.subTest(argv=argv):
                rc, out, err = run_init(argv, repo_root=self.repo_root)
                self.assertEqual(rc, EXIT_OK, msg=err)
                self.assertIn("MAW_HUMAN push gate", out)
                self.assertIn("NOT active", out)
                self.assertIn(".git/hooks/pre-push.maw-recommended", out)
        self.assertEqual(hook.read_text(), "#!/bin/sh\necho mine\n")

    def test_maws_own_pre_push_gets_no_gate_warning(self):
        rc, out, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertNotIn("NOT active", out)
        hook = self.repo_root / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho mine\n")
        rc, out, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertNotIn("NOT active", out)

    def test_interrupted_install_resumes(self):
        # Simulate config.yaml present but state.json absent (and some hooks
        # missing). /maw:init should resume cleanly without --force.
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        # Remove state.json and one installed file to simulate interruption.
        (self.repo_root / ".maw" / "state.json").unlink()
        (self.repo_root / ".git" / "hooks" / "pre-push").unlink()
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertTrue((self.repo_root / ".git" / "hooks" / "pre-push").is_file())
        self.assertTrue((self.repo_root / ".maw" / "state.json").is_file())

    def test_target_is_not_git_repo_refuses(self):
        # Strip the .git fixture set up by base.
        shutil.rmtree(self.repo_root / ".git")
        rc, _, err = run_init(["--profile", "generic"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_REFUSE)
        self.assertIn("git", err.lower())


class TargetIsMawRefusalTests(unittest.TestCase):
    """Out of CLITestBase: verifies the maw-repo refusal sentinel."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        # Place a sentinel marketplace.json — the canonical "you're in maw" signal.
        (self.root / ".claude-plugin").mkdir()
        (self.root / ".claude-plugin" / "marketplace.json").write_text("{}\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_marketplace_sentinel_triggers_refusal(self):
        os.environ["MAW_BRIDGE_HOME"] = str(self.root / "_bridge")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli.main(["--repo-root", str(self.root), "--profile", "generic"])
        self.assertEqual(rc, EXIT_REFUSE)
        self.assertIn("maw substrate", err.getvalue())


# ---------------------------------------------------------------------------
# Substitution surface (§6 row: "Substitution placeholder has no value")
# ---------------------------------------------------------------------------

class SubstitutionTests(CLITestBase):

    def test_all_placeholders_resolve_for_generic(self):
        rc, out, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        # No unresolved {{ident}} should remain in any file actually
        # installed (state.json's tracked set), plus config.yaml which is
        # rendered but not state-tracked.
        state = read_state(self.repo_root)
        rels = list(state.files.keys()) + [".maw/config.yaml"]
        self.assertGreater(len(rels), 1)
        for rel in rels:
            text = (self.repo_root / rel).read_text()
            self.assertNotIn("{{", text, f"unresolved placeholder in {rel}")

    def test_pre_push_substitutes_alias_and_branch(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        pre_push = (self.repo_root / ".git" / "hooks" / "pre-push").read_text()
        # alias_suggestion defaults to "mw" in the generic profile.
        self.assertIn("'mw'", pre_push)
        self.assertIn("refs/heads/main", pre_push)

    def test_unrecognized_profile_refuses_with_listing(self):
        rc, out, err = run_init(
            ["--profile", "does-not-exist"], repo_root=self.repo_root
        )
        self.assertEqual(rc, EXIT_REFUSE)
        self.assertIn("does-not-exist", err)
        self.assertIn("generic", err)  # available list mentioned


# ---------------------------------------------------------------------------
# Drift detection (§4)
# ---------------------------------------------------------------------------

class DriftDetectionTests(CLITestBase):

    def setUp(self) -> None:
        super().setUp()
        # Always start from a clean install.
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)

    def test_matching_sha_is_no_drift(self):
        rc, out, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("drifted=0", out)
        self.assertIn("skipped=2", out)

    def test_sha_mismatch_detected(self):
        f = self.repo_root / ".claude" / "schemas" / "handoff.json"
        f.write_text(f.read_text() + "\n# touch\n")
        rc, out, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("drifted=1", out)

    def test_whitespace_only_change_is_drift(self):
        # Even appending a single newline is drift (SHA changes).
        f = self.repo_root / ".git" / "hooks" / "pre-push"
        f.write_text(f.read_text() + "\n")
        rc, out, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("drifted=1", out)

    def test_a_post_merge_hook_from_an_older_install_is_left_alone(self):
        """Repos initialised before the hook left the profiles still have it.
        A re-run and an upgrade neither remove it, rewrite it nor report it."""
        f = self.repo_root / ".git" / "hooks" / "post-merge"
        f.write_text("#!/bin/sh\n# installed by an older maw\nexit 0\n")
        before = f.read_bytes()
        for args in ([], ["--upgrade"]):
            rc, out, err = run_init(args, repo_root=self.repo_root)
            self.assertEqual(rc, EXIT_OK, err)
            self.assertEqual(f.read_bytes(), before)
            self.assertNotIn("post-merge", out)
            self.assertFalse(f.with_name("post-merge.maw-recommended").exists())

    def test_a_tracked_post_merge_hook_from_an_older_install_is_left_alone(self):
        """The real older install: the hook is on disk AND state.json tracks it,
        because that maw's profile listed it. A re-run, an upgrade and a forced
        run leave the file byte for byte, drop the entry no profile backs any
        more, and say nothing about it."""
        from init.state import FileEntry, write_state
        rel = ".git/hooks/post-merge"
        f = self.repo_root / rel
        f.write_text("#!/bin/sh\n# installed by an older maw\nexit 0\n")
        before = f.read_bytes()
        state = read_state(self.repo_root)
        state.files[rel] = FileEntry(template="git-hooks/post-merge",
                                     installed_sha256=sha256_file(f),
                                     installed_at="2026-06-12T00:00:00Z")
        write_state(self.repo_root, state)
        self.assertIn(rel, read_state(self.repo_root).files)
        for args in ([], ["--upgrade"], ["--force"]):
            with self.subTest(args=args):
                rc, out, err = run_init(args, repo_root=self.repo_root)
                self.assertEqual(rc, EXIT_OK, err)
                self.assertEqual(f.read_bytes(), before)
                self.assertNotIn("post-merge", out + err)
                self.assertFalse(f.with_name("post-merge.maw-recommended").exists())
                self.assertNotIn(rel, read_state(self.repo_root).files)
                self.assertIn("drifted=0", out)

    def test_deleted_file_is_reinstalled(self):
        f = self.repo_root / ".git" / "hooks" / "pre-push"
        f.unlink()
        rc, out, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        self.assertTrue(f.is_file())
        self.assertIn("installed=1", out)

    def test_drift_does_not_overwrite_original(self):
        f = self.repo_root / ".claude" / "schemas" / "handoff.json"
        edited = f.read_text() + "\n# The operator keeps this\n"
        f.write_text(edited)
        rc, _, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        # Original still has the operator's edit.
        self.assertEqual(f.read_text(), edited)
        # Sibling exists and is the freshly-rendered version (no operator edit).
        sibling = f.parent / (f.name + ".maw-recommended")
        self.assertTrue(sibling.is_file())
        self.assertNotIn("# The operator keeps this", sibling.read_text())


# ---------------------------------------------------------------------------
# --force semantics (§5)
# ---------------------------------------------------------------------------

class ForceTests(CLITestBase):

    def setUp(self) -> None:
        super().setUp()
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)

    def test_force_overwrites_drifted(self):
        f = self.repo_root / ".claude" / "schemas" / "handoff.json"
        edited = f.read_text() + "\n# The operator edit\n"
        f.write_text(edited)
        rc, out, _ = run_init(["--force"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        # File content reverted to substrate version.
        self.assertNotIn("# The operator edit", f.read_text())

    def test_force_creates_attic_backup(self):
        f = self.repo_root / ".git" / "hooks" / "pre-push"
        f.write_text(f.read_text() + "\n# original drift\n")
        rc, _, _ = run_init(["--force"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        attic = self.repo_root / ".claude" / "maw" / ".attic"
        self.assertTrue(attic.is_dir())
        backups = list(attic.glob("pre-push.*"))
        self.assertEqual(len(backups), 1)
        # Backup matches the edited content, not the freshly-rendered one.
        self.assertIn("# original drift", backups[0].read_text())

    def test_force_backup_timestamp_format(self):
        f = self.repo_root / ".claude" / "schemas" / "handoff.json"
        f.write_text(f.read_text() + "\n")
        rc, _, _ = run_init(["--force"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        # Backup filename ends with the contract §5 UTC ISO8601 shape.
        attic = self.repo_root / ".claude" / "maw" / ".attic"
        names = [p.name for p in attic.iterdir()]
        self.assertTrue(any(".20" in n and n.endswith("Z") for n in names), names)

    def test_force_still_refuses_on_non_git_repo(self):
        shutil.rmtree(self.repo_root / ".git")
        rc, _, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_REFUSE)
        self.assertIn("git", err.lower())

    def test_force_dry_run_is_no_writes(self):
        f = self.repo_root / ".claude" / "schemas" / "handoff.json"
        edited = f.read_text() + "\n# The operator edit\n"
        f.write_text(edited)
        rc, out, _ = run_init(["--force", "--dry-run"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        # File still has the operator's edit (no writes).
        self.assertEqual(f.read_text(), edited)
        # No attic dir was created.
        self.assertFalse((self.repo_root / ".claude" / "maw" / ".attic").exists())
        # Banner mentions a would-overwrite-with-backup action.
        self.assertIn("would-overwrite-with-backup", out)


# ---------------------------------------------------------------------------
# --dry-run semantics
# ---------------------------------------------------------------------------

class DryRunTests(CLITestBase):

    def test_fresh_dry_run_writes_nothing(self):
        rc, out, err = run_init(
            ["--profile", "generic", "--dry-run"], repo_root=self.repo_root
        )
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertFalse((self.repo_root / ".maw" / "config.yaml").exists())
        self.assertFalse((self.repo_root / ".maw" / "state.json").exists())
        self.assertFalse((self.repo_root / ".claude").exists())
        # Banner still prints all planned actions.
        self.assertIn("would-install", out)

    def test_dry_run_drifted_prints_recommended_plan(self):
        # Install first, then drift, then dry-run.
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        f = self.repo_root / ".claude" / "schemas" / "handoff.json"
        f.write_text(f.read_text() + "\n# drift\n")
        rc, out, _ = run_init(["--dry-run"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("would-write-recommended", out)
        # No sibling was written.
        self.assertFalse(
            (self.repo_root / ".claude" / "schemas" / "handoff.json.maw-recommended").exists()
        )

    def test_upgrade_dry_run_prints_upgrade_mode_marker(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        rc, out, _ = run_init(
            ["--upgrade", "--dry-run"], repo_root=self.repo_root
        )
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("upgrade-verbose mode", out)

    def test_dry_run_first_install_does_not_write_state(self):
        rc, _, _ = run_init(
            ["--profile", "generic", "--dry-run"], repo_root=self.repo_root
        )
        self.assertEqual(rc, EXIT_OK)
        self.assertFalse((self.repo_root / ".maw" / "state.json").exists())


# ---------------------------------------------------------------------------
# Error paths (§6)
# ---------------------------------------------------------------------------

class ErrorPathTests(CLITestBase):

    def test_not_a_git_repo_exit_1(self):
        shutil.rmtree(self.repo_root / ".git")
        rc, _, err = run_init(["--profile", "generic"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_REFUSE)
        self.assertIn("git init", err)

    def test_missing_profile_exit_1(self):
        rc, _, err = run_init(["--profile", "nope"], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_REFUSE)

    def test_dot_claude_is_file_exit_2(self):
        (self.repo_root / ".claude").write_text("not-a-dir\n")
        rc, _, err = run_init(
            ["--profile", "generic"], repo_root=self.repo_root
        )
        self.assertEqual(rc, EXIT_PARSE)
        self.assertIn(".claude", err)

    def test_stack_profile_change_refused_without_force(self):
        # First install on python-flask-railway. We have to make the dir look
        # like a python repo so detection wouldn't naturally pick a different
        # profile mid-test.
        rc, _, _ = run_init(
            ["--profile", "python-flask-railway"], repo_root=self.repo_root
        )
        self.assertEqual(rc, EXIT_OK)
        rc, _, err = run_init(
            ["--profile", "node-next-vercel"], repo_root=self.repo_root
        )
        self.assertEqual(rc, EXIT_REFUSE)
        self.assertIn("profile", err.lower())

    def test_permission_denied_on_target_dir_exit_3(self):
        # Create a target dir we can't write into and aim a hook at it.
        if os.geteuid() == 0:
            self.skipTest("permission tests don't behave reliably as root")
        ro_dir = self.repo_root / ".claude"
        ro_dir.mkdir()
        ro_dir.chmod(0o500)  # r-x only
        try:
            rc, _, err = run_init(
                ["--profile", "generic"], repo_root=self.repo_root
            )
            # Either permission (3) or io (4) depending on which write
            # tripped first; either is a non-zero failure with an actionable
            # stderr line.
            self.assertIn(rc, (EXIT_PERMISSION, EXIT_IO))
            self.assertNotEqual(rc, EXIT_OK)
        finally:
            ro_dir.chmod(0o700)

    def test_malformed_state_json_exit_2(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        (self.repo_root / ".maw" / "state.json").write_text("not json")
        rc, _, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_PARSE)
        self.assertIn("state.json", err)


# ---------------------------------------------------------------------------
# Idempotency (§7)
# ---------------------------------------------------------------------------

class IdempotencyTests(CLITestBase):

    def test_double_init_is_no_op(self):
        rc1, _, _ = self.install_fresh()
        self.assertEqual(rc1, EXIT_OK)
        rc2, out, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc2, EXIT_OK)
        self.assertIn("installed=0", out)
        self.assertIn("drifted=0", out)
        self.assertIn("backed-up=0", out)

    def test_state_sha_matches_disk_after_install(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        state = read_state(self.repo_root)
        self.assertEqual(state.schema_version, SCHEMA_VERSION)
        for rel, entry in state.files.items():
            self.assertEqual(
                sha256_file(self.repo_root / rel),
                entry.installed_sha256,
                f"state mismatch for {rel}",
            )

    def test_config_yaml_not_overwritten_on_rerun(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        cfg = self.repo_root / ".maw" / "config.yaml"
        # The operator edits config.yaml after first install.
        mutated = cfg.read_text() + "\n# The operator customization\n"
        cfg.write_text(mutated)
        rc, _, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(cfg.read_text(), mutated)


# ---------------------------------------------------------------------------
# Gitignore + banner observability (§8)
# ---------------------------------------------------------------------------

class ObservabilityTests(CLITestBase):

    def test_banner_has_six_fields(self):
        rc, out, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        # All six contract §8 banner fields must appear.
        for marker in ("[1] Profile:", "[2] Capabilities:", "[3] Per-file",
                       "[4] Bridge slug:", "[5] Summary:", "[6] Next:"):
            self.assertIn(marker, out)

    def test_banner_lists_per_file_actions(self):
        rc, out, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        self.assertIn(ACTION_INSTALL, out)

    def test_banner_summary_counts_match_actions(self):
        rc, out, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        # Generic profile installs 0 hooks (opt-in) + 1 git-hook
        # (pre-push) + 1 schema = 2 files.
        self.assertIn("installed=2", out)

    def test_gitignore_fragment_appended(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        gi = (self.repo_root / ".gitignore").read_text()
        self.assertIn(".maw/*", gi)
        self.assertIn("!.maw/config.yaml", gi)
        self.assertIn(".claude/maw/.attic/", gi)
        # Sentinel markers present so re-runs don't double-append.
        self.assertIn(cli.GITIGNORE_BEGIN, gi)
        self.assertIn(cli.GITIGNORE_END, gi)

    def test_gitignore_fragment_semantics_in_real_git_repo(self):
        import subprocess
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        root = str(self.repo_root)
        subprocess.run(["git", "init", "-q", root], check=True)

        def ignored(rel: str) -> bool:
            r = subprocess.run(["git", "-C", root, "check-ignore", "-q", rel])
            return r.returncode == 0

        self.assertFalse(ignored(".maw/config.yaml"))
        for rel in (".maw/state.json", ".maw/.runtime/x", ".maw/other"):
            self.assertTrue(ignored(rel), rel)

    def test_gitignore_fragment_idempotent(self):
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        rc, _, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)
        gi = (self.repo_root / ".gitignore").read_text()
        # Sentinel appears exactly once.
        self.assertEqual(gi.count(cli.GITIGNORE_BEGIN), 1)

    def test_gitignore_recovers_from_interrupted_prior_run(self):
        """BEGIN-only-no-END (crash between two file writes) must NOT
        double-append. Re-run replaces the BEGIN-through-EOF block with
        the fresh fragment so the result has one BEGIN + one END.
        """
        # Simulate an interrupted prior run: pre-existing user content
        # followed by the BEGIN sentinel with no body and no END.
        gi_path = self.repo_root / ".gitignore"
        gi_path.write_text(f"node_modules/\n{cli.GITIGNORE_BEGIN}\n")
        rc, _, _ = self.install_fresh()
        self.assertEqual(rc, EXIT_OK)
        gi = gi_path.read_text()
        # Exactly one BEGIN and one END after recovery.
        self.assertEqual(gi.count(cli.GITIGNORE_BEGIN), 1)
        self.assertEqual(gi.count(cli.GITIGNORE_END), 1)
        # Fragment body must be present (the full block, not a half-block).
        self.assertIn(".maw/*", gi)
        self.assertIn(".claude/maw/.attic/", gi)
        # Pre-existing user content must be preserved.
        self.assertIn("node_modules/", gi)


class GitignoreMarkerTests(CLITestBase):
    """The block's BEGIN marker was respelled. A repo that carries the former
    marker must end with one block under the current one, and nothing outside
    the block may change. Every expected file here comes from a real init."""

    FORMER = cli.GITIGNORE_BEGIN_FORMER[0]

    def _gi(self) -> str:
        return (self.repo_root / ".gitignore").read_text(encoding="utf-8")

    def _init(self) -> None:
        rc, _, _ = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK)

    def _fresh_block(self) -> str:
        """The block a real init writes into an empty repo."""
        self._init()
        block = self._gi()
        self.assertTrue(block.startswith(cli.GITIGNORE_BEGIN + "\n"), block)
        self.assertTrue(block.endswith(cli.GITIGNORE_END + "\n"), block)
        return block

    def _as_former(self, block: str) -> str:
        former = block.replace(cli.GITIGNORE_BEGIN, self.FORMER)
        self.assertNotEqual(former, block)
        return former

    def test_a_new_install_writes_the_current_marker(self) -> None:
        block = self._fresh_block()
        self.assertEqual(block.count(cli.GITIGNORE_BEGIN), 1)
        self.assertNotIn(self.FORMER, block)
        self.assertIn("/maw:init", cli.GITIGNORE_BEGIN)
        self.assertNotEqual(self.FORMER, cli.GITIGNORE_BEGIN)

    def test_a_block_under_the_former_marker_is_updated_in_place(self) -> None:
        block = self._fresh_block()
        before, after = "node_modules/\n*.log\n\n", "\n# mine\ndist/\n"
        (self.repo_root / ".gitignore").write_text(before + self._as_former(block) + after, encoding="utf-8")
        self._init()
        self.assertEqual(self._gi(), before + block + after)

    def test_a_block_under_the_current_marker_is_left_alone(self) -> None:
        block = self._fresh_block()
        text = "node_modules/\n\n" + block + "dist/\n"
        (self.repo_root / ".gitignore").write_text(text, encoding="utf-8")
        self._init()
        self.assertEqual(self._gi(), text)

    def test_a_repo_with_both_markers_ends_with_exactly_one_block(self) -> None:
        block = self._fresh_block()
        for first, second in ((self._as_former(block), block), (block, self._as_former(block))):
            with self.subTest(first=first.splitlines()[0]):
                (self.repo_root / ".gitignore").write_text(
                    "a/\n" + first + "b/\n" + second + "c/\n", encoding="utf-8")
                self._init()
                self.assertEqual(self._gi(), "a/\n" + block + "b/\n" + "c/\n")

    def test_lines_outside_the_block_are_byte_identical(self) -> None:
        block = self._fresh_block()
        # No final newline, a CRLF line, trailing spaces and a tab: none may move.
        before = "node_modules/  \n\twith-tab\r\n\n\n"
        after = "# --- not maw ---\nlast-line-no-newline"
        (self.repo_root / ".gitignore").write_bytes((before + self._as_former(block) + after).encode("utf-8"))
        self._init()
        got = (self.repo_root / ".gitignore").read_bytes().decode("utf-8")
        self.assertTrue(got.startswith(before))
        self.assertTrue(got.endswith(after))
        self.assertEqual(got, before + block + after)

    def test_a_second_init_is_a_no_op(self) -> None:
        block = self._fresh_block()
        gi = self.repo_root / ".gitignore"
        gi.write_text("x/\n" + self._as_former(block), encoding="utf-8")
        self._init()
        once = gi.read_bytes()
        stamp = gi.stat().st_mtime_ns
        self._init()
        self.assertEqual(gi.read_bytes(), once)
        self.assertEqual(gi.stat().st_mtime_ns, stamp, "the second init rewrote an unchanged .gitignore")
        self.assertFalse(cli._ensure_gitignore_fragment(self.repo_root, dry_run=True))

    def test_a_former_marker_with_no_end_line_is_recovered(self) -> None:
        block = self._fresh_block()
        (self.repo_root / ".gitignore").write_text("keep/\n" + self.FORMER + "\n.maw/*\n", encoding="utf-8")
        self._init()
        self.assertEqual(self._gi(), "keep/\n" + block)

    def test_user_lines_below_a_block_with_no_end_line_are_kept(self) -> None:
        block = self._fresh_block()
        body = block.splitlines()[1]
        (self.repo_root / ".gitignore").write_text(
            "keep/\n" + self.FORMER + "\n" + body + "\nmine/\n" + body + "\nlast", encoding="utf-8")
        self._init()
        # The damaged block is its marker line and the fragment lines right
        # under it. "mine/" ends it; the repeated body line below is the user's.
        self.assertEqual(self._gi(), "keep/\n" + block + "mine/\n" + body + "\nlast")

    def test_a_marker_quoted_inside_a_line_is_not_a_block(self) -> None:
        block = self._fresh_block()
        for marker in (self.FORMER, cli.GITIGNORE_BEGIN):
            with self.subTest(marker=marker):
                text = "# note: " + marker + " is maw's\nkeep/\n" + marker + " trailing\nalso/\n"
                (self.repo_root / ".gitignore").write_text(text, encoding="utf-8")
                self._init()
                self.assertEqual(self._gi(), text + "\n" + block)

    def test_an_end_line_with_no_begin_is_left_alone(self) -> None:
        block = self._fresh_block()
        text = cli.GITIGNORE_END + "\nx/\n"
        (self.repo_root / ".gitignore").write_text(text, encoding="utf-8")
        self._init()
        self.assertEqual(self._gi(), text + "\n" + block)

    def test_a_crlf_file_keeps_its_line_endings_outside_the_block(self) -> None:
        block = self._fresh_block()
        crlf = ("a/\n" + self._as_former(block) + "b/\n").replace("\n", "\r\n")
        (self.repo_root / ".gitignore").write_bytes(crlf.encode("utf-8"))
        self._init()
        once = (self.repo_root / ".gitignore").read_bytes()
        self.assertEqual(once.decode("utf-8"), "a/\r\n" + block + "b/\r\n")
        self._init()
        self.assertEqual((self.repo_root / ".gitignore").read_bytes(), once)

    def test_an_interrupted_block_followed_by_a_whole_one_keeps_one(self) -> None:
        block = self._fresh_block()
        (self.repo_root / ".gitignore").write_text(
            "keep/\n" + self.FORMER + "\n.maw/*\n" + block + "tail/\n", encoding="utf-8")
        self._init()
        self.assertEqual(self._gi(), "keep/\n" + block + "tail/\n")


# ---------------------------------------------------------------------------
# Template path mapping helper
# ---------------------------------------------------------------------------

class Run2GapFillTests(CLITestBase):
    """Run-2 gap-fill for /maw:init: behaviors not exercised by Task E's tests.

    Two gaps closed here:

    - State-SHA stability across no-op re-runs. `test_state_sha_matches_disk_
      after_install` checks the post-install snapshot; this test asserts that
      `installed_sha256` for every tracked file is UNCHANGED across a no-op
      re-run. A future change that re-substitutes templates with non-stable
      output (e.g., embedding a timestamp into a hook body) would silently
      flip per-file SHAs even with no drift on disk — this test fails fast.

    - Bridge-slug collision detection actually consults MAW_BRIDGE_HOME.
      The marker-file path uses the env var consistently with the slug
      computation, so a pre-existing `<slug>/.maw-repo-id` carrying a
      DIFFERENT repo_id must trigger the -2 suffix and emit the WARNING
      line in the banner. Confirms the §9.6 lossless preserve.
    """

    def test_state_shas_stable_across_no_op_rerun(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        before = read_state(self.repo_root)
        before_shas = {
            rel: entry.installed_sha256 for rel, entry in before.files.items()
        }
        self.assertGreater(len(before_shas), 0)

        # Re-run with no drift on disk; SHAs must be byte-stable.
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("drifted=0", out)
        self.assertIn("installed=0", out)

        after = read_state(self.repo_root)
        after_shas = {
            rel: entry.installed_sha256 for rel, entry in after.files.items()
        }
        # Same set of tracked files, same SHA for each. The substrate-version
        # and per-file installed_at fields are allowed to bump; the SHA over
        # the rendered substrate content is the stability contract.
        self.assertEqual(
            set(after_shas), set(before_shas),
            "state.json files set changed across no-op rerun",
        )
        for rel, sha in before_shas.items():
            self.assertEqual(
                after_shas[rel], sha,
                f"SHA drift for {rel}: {sha} → {after_shas[rel]}",
            )
        # And the on-disk files still match those stable SHAs.
        for rel, sha in after_shas.items():
            self.assertEqual(sha256_file(self.repo_root / rel), sha)

    def test_slug_collision_marker_uses_maw_bridge_home(self):
        # Pre-populate MAW_BRIDGE_HOME with a marker for the candidate slug
        # that carries a DIFFERENT repo_id. /maw:init should detect this as
        # a collision, suffix the slug with -2, emit a WARNING in the banner,
        # and persist the -2 form into both config.yaml and the on-disk
        # marker. The whole test depends on the cli + the marker path
        # agreeing on MAW_BRIDGE_HOME (the env var, not ~/.claude/bridge).
        import hashlib as _hashlib

        # Compute what the cli will compute for this repo's slug.
        abs_path = str(self.repo_root.resolve())
        name = self.repo_root.resolve().name.lower()
        safe = "".join(c if c.isalnum() or c == "-" else "-" for c in name).strip("-")
        if not safe:
            safe = "repo"
        digest = _hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]
        candidate_slug = f"{safe}-{digest}"

        # Sandbox MAW_BRIDGE_HOME for the duration of this test; pre-create
        # a colliding marker file claiming the candidate_slug for someone else.
        bridge_home = self.repo_root / "_test_bridge_home"
        (bridge_home / candidate_slug).mkdir(parents=True)
        (bridge_home / candidate_slug / ".maw-repo-id").write_text(
            "ffffffff-ffff-4fff-afff-ffffffffffff\n", encoding="utf-8"
        )

        rc, out, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)

        # Banner emits the WARNING with the original candidate and the
        # suffixed replacement.
        self.assertIn("WARNING", out)
        self.assertIn(candidate_slug, out)
        self.assertIn(f"{candidate_slug}-2", out)

        # config.yaml carries the suffixed slug, not the candidate.
        config_text = (self.repo_root / ".maw" / "config.yaml").read_text()
        self.assertIn(f"{candidate_slug}-2", config_text)

        # Marker for the suffixed slug landed under MAW_BRIDGE_HOME — not
        # under a hardcoded ~/.claude/bridge fallback.
        new_marker = bridge_home / f"{candidate_slug}-2" / ".maw-repo-id"
        self.assertTrue(
            new_marker.is_file(),
            f"expected marker at {new_marker}; tree: "
            f"{[p.name for p in bridge_home.iterdir()]}",
        )
        # And the pre-existing marker is left untouched (lossless preserve).
        original_marker = bridge_home / candidate_slug / ".maw-repo-id"
        self.assertEqual(
            original_marker.read_text().strip(),
            "ffffffff-ffff-4fff-afff-ffffffffffff",
        )


class TemplateMappingTests(unittest.TestCase):

    def test_hooks_maps_to_claude_hooks(self):
        self.assertEqual(
            template_to_target_relpath("hooks/budget-guard.py"),
            ".claude/hooks/budget-guard.py",
        )

    def test_git_hooks_maps_to_git_hooks(self):
        self.assertEqual(
            template_to_target_relpath("git-hooks/pre-push"),
            ".git/hooks/pre-push",
        )

    def test_schemas_maps_to_claude_schemas(self):
        self.assertEqual(
            template_to_target_relpath("schemas/handoff.json"),
            ".claude/schemas/handoff.json",
        )

    def test_unknown_category_raises(self):
        with self.assertRaises(ValueError):
            template_to_target_relpath("agents/nope.md")


class LinkedWorktreeGitHooksTests(unittest.TestCase):
    """#204: init from a LINKED worktree, where `.git` is a gitdir-pointer
    FILE. The old `repo_root / ".git/hooks"` join ENOTDIRed on mkdir; the
    fix resolves the shared hooks dir via `git rev-parse --git-path hooks`.
    Real git processes, not a faked `.git` dir — the fixture class the
    original tests used is exactly what hid this defect.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.main_root = base / "main"
        self.main_root.mkdir()
        git = ["git", "-C", str(self.main_root)]
        for argv in (
            [*git, "init", "-q"],
            [*git, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "seed"],
            [*git, "worktree", "add", "-q", str(base / "wt")],
        ):
            import subprocess
            subprocess.run(argv, check=True, capture_output=True)
        self.wt_root = base / "wt"
        self.assertTrue(
            (self.wt_root / ".git").is_file(),
            "fixture broken: linked worktree .git should be a FILE",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_from_linked_worktree_lands_hooks_in_common_dir(self):
        rc, out, err = run_init(
            ["--profile", "generic"], repo_root=self.wt_root
        )
        self.assertEqual(rc, EXIT_OK, f"stdout={out!r} stderr={err!r}")
        for hook in ("pre-push",):
            self.assertTrue(
                (self.main_root / ".git" / "hooks" / hook).is_file(),
                f"{hook} missing from the shared common hooks dir",
            )
        # And nothing tried to nest under the pointer file.
        self.assertTrue((self.wt_root / ".git").is_file())

    def test_upgrade_from_linked_worktree_exits_zero(self):
        rc, _, _ = run_init(["--profile", "generic"], repo_root=self.wt_root)
        self.assertEqual(rc, EXIT_OK)
        rc, out, err = run_init(
            ["--profile", "generic", "--upgrade"], repo_root=self.wt_root
        )
        self.assertEqual(rc, EXIT_OK, f"stdout={out!r} stderr={err!r}")


class HooksPathNoteTests(unittest.TestCase):
    """RELEASE-NOTES debt: with core.hooksPath set, git ignores .git/hooks.
    The banner must name where the hooks actually landed (real git)."""

    def setUp(self) -> None:
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "r"
        self.root.mkdir()
        patcher = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True,
                       capture_output=True)

    def set_hooks_path(self, value: str) -> None:
        import subprocess
        subprocess.run(["git", "-C", str(self.root), "config", "core.hooksPath",
                        value], check=True, capture_output=True)

    def test_hooks_path_set_prints_where_hooks_landed(self):
        self.set_hooks_path(".myhooks")
        rc, out, err = run_init(["--profile", "generic"], repo_root=self.root)
        self.assertEqual(rc, EXIT_OK, f"stdout={out!r} stderr={err!r}")
        self.assertTrue((self.root / ".myhooks" / "pre-push").is_file())
        self.assertFalse((self.root / ".git" / "hooks" / "pre-push").exists())
        self.assertIn("NOTE: core.hooksPath is set (.myhooks)", out)
        self.assertIn(".myhooks", out.split("NOTE: core.hooksPath")[1])
        self.assertIn("not .git/hooks", out)

    def test_dry_run_says_would_be(self):
        self.set_hooks_path(".myhooks")
        rc, out, _ = run_init(["--profile", "generic", "--dry-run"],
                              repo_root=self.root)
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("hooks would be installed in", out)

    def test_no_hooks_path_prints_no_note(self):
        rc, out, _ = run_init(["--profile", "generic"], repo_root=self.root)
        self.assertEqual(rc, EXIT_OK)
        self.assertNotIn("core.hooksPath", out)


# ---------------------------------------------------------------------------
# The symlink check and the write are one step, and a refusal is not a record
# ---------------------------------------------------------------------------

HANDOFF_REL = ".claude/schemas/handoff.json"


class SymlinkRaceTests(CLITestBase):
    """A symlink swapped in AFTER the through_symlink check (injected by
    wrapping the check, never by timing) must be refused at the write."""

    def _outside_dir(self) -> Path:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        return outside

    def _swap_after_check(self, module, when, swap):
        """Patch module.through_symlink: run the real check, then, the first
        time `when(path)` holds, swap the symlink in before returning."""
        real = module.through_symlink
        fired: list[bool] = []

        def wrapped(repo_root: Path, target_abs: Path) -> bool:
            verdict = real(repo_root, target_abs)
            if not fired and when(target_abs):
                fired.append(True)
                swap()
            return verdict

        patcher = mock.patch.object(module, "through_symlink", side_effect=wrapped)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fired

    def test_parent_dir_swapped_to_a_symlink_after_the_check_is_refused(self):
        import init.install as inst
        outside = self._outside_dir()
        schemas = self.repo_root / ".claude" / "schemas"

        def swap() -> None:
            schemas.parent.mkdir(parents=True, exist_ok=True)
            schemas.symlink_to(outside)

        fired = self._swap_after_check(
            inst, lambda p: str(p).endswith(HANDOFF_REL), swap)
        rc, out, err = self.install_fresh()
        self.assertTrue(fired, "the injected swap never ran")
        self.assertEqual(os.listdir(outside), [], "maw wrote outside the repo")
        self.assertTrue(schemas.is_symlink())
        self.assertIn("symlink", err)
        self.assertNotIn(HANDOFF_REL, read_state(self.repo_root).files)

    def test_dangling_target_swapped_in_after_the_check_is_not_replaced(self):
        import init.install as inst
        outside = self._outside_dir()
        link = self.repo_root / HANDOFF_REL

        def swap() -> None:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(outside / "not-yet.json")

        fired = self._swap_after_check(
            inst, lambda p: str(p).endswith(HANDOFF_REL), swap)
        rc, out, err = self.install_fresh()
        self.assertTrue(fired, "the injected swap never ran")
        self.assertTrue(link.is_symlink(), "the user's symlink was replaced")
        self.assertEqual(os.listdir(outside), [])
        self.assertIn("symlink", err)
        self.assertNotIn(HANDOFF_REL, read_state(self.repo_root).files)

    def test_force_target_swapped_to_a_symlink_after_the_check_is_not_replaced(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        import init.install as inst
        outside = self._outside_dir()
        theirs = outside / "theirs.json"
        theirs.write_text("theirs\n")
        link = self.repo_root / HANDOFF_REL

        def swap() -> None:
            link.unlink()
            link.symlink_to(theirs)

        fired = self._swap_after_check(
            inst, lambda p: str(p).endswith(HANDOFF_REL), swap)
        rc, out, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertTrue(fired, "the injected swap never ran")
        self.assertTrue(link.is_symlink(), "the user's symlink was replaced")
        self.assertEqual(theirs.read_text(), "theirs\n")
        self.assertEqual(sorted(os.listdir(outside)), ["theirs.json"])
        self.assertIn("symlink", err)

    def test_attic_swapped_to_a_symlink_after_the_check_backs_up_nothing_outside(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        import init.install as inst
        outside = self._outside_dir()
        mine = self.repo_root / HANDOFF_REL
        mine.write_text('{"edited": true}\n')
        claude_maw = self.repo_root / ".claude" / "maw"

        def swap() -> None:
            if claude_maw.exists():
                shutil.rmtree(claude_maw)
            claude_maw.symlink_to(outside)

        fired = self._swap_after_check(inst, lambda p: ".attic" in str(p), swap)
        rc, out, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertTrue(fired, "the injected swap never ran")
        self.assertEqual(os.listdir(outside), [], "maw backed up outside the repo")
        self.assertEqual(mine.read_text(), '{"edited": true}\n')
        self.assertIn("symlink", err)

    def test_maw_dir_swapped_to_a_symlink_after_the_cli_check_is_refused(self):
        outside = self._outside_dir()
        maw_dir = self.repo_root / ".maw"

        def swap() -> None:
            maw_dir.symlink_to(outside)

        fired = self._swap_after_check(
            cli, lambda p: str(p).endswith(".maw/state.json"), swap)
        rc, out, err = self.install_fresh()
        self.assertTrue(fired, "the injected swap never ran")
        self.assertEqual(rc, EXIT_REFUSE, msg=err)
        self.assertEqual(os.listdir(outside), [], "maw wrote outside the repo")
        self.assertIn("symlink", err)


class RefusedIsNotRecordedTests(CLITestBase):
    """state.json records what maw put on disk, not what it wanted to."""

    def _set_prior_entry_stamp(self, rel: str, stamp: str) -> None:
        path = self.repo_root / ".maw" / "state.json"
        data = json.loads(path.read_text())
        data["files"][rel]["installed_at"] = stamp
        path.write_text(json.dumps(data, indent=2) + "\n")

    def test_an_adopted_file_kept_as_theirs_records_the_drift_baseline(self):
        # Advisor CR on #315: the kept-edit case keeps its record (the rendered
        # SHA), or `/maw:status` never surfaces its .maw-recommended sibling.
        mine = self.repo_root / HANDOFF_REL
        mine.parent.mkdir(parents=True)
        mine.write_text('{"mine": true}\n')
        rc, out, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertEqual(mine.read_text(), '{"mine": true}\n')
        sibling = mine.with_name(mine.name + ".maw-recommended")
        entry = read_state(self.repo_root).files[HANDOFF_REL]
        self.assertEqual(entry.installed_sha256, sha256_file(sibling))

    def test_taking_maws_version_later_records_it(self):
        mine = self.repo_root / HANDOFF_REL
        mine.parent.mkdir(parents=True)
        mine.write_text('{"mine": true}\n')
        rc, _, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        sibling = mine.with_name(mine.name + ".maw-recommended")
        mine.write_text(sibling.read_text())
        sibling.unlink()
        for argv in ([], ["--upgrade"]):
            with self.subTest(argv=argv):
                rc, out, err = run_init(argv, repo_root=self.repo_root)
                self.assertEqual(rc, EXIT_OK, msg=err)
                self.assertIn("skipped=2", out)
                entry = read_state(self.repo_root).files[HANDOFF_REL]
                self.assertEqual(entry.installed_sha256, sha256_file(mine))

    def test_force_write_refused_after_the_backup_keeps_the_prior_record(self):
        # Advisor MINOR on #315: the backup succeeds, then the target write
        # refuses; the prior entry must survive.
        from init import install as install_mod
        from init.state import SymlinkRefusal
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        self._set_prior_entry_stamp(HANDOFF_REL, "2000-01-01T00:00:00Z")
        mine = self.repo_root / HANDOFF_REL
        mine.write_text('{"edited": true}\n')
        real = install_mod._write_file

        def refuse_handoff(target_abs, content, *, executable, repo_root):
            if target_abs == mine:
                raise SymlinkRefusal(target_abs)
            return real(target_abs, content, executable=executable, repo_root=repo_root)
        with mock.patch.object(install_mod, "_write_file", refuse_handoff):
            rc, _, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertIn("symlink", err)
        entry = read_state(self.repo_root).files[HANDOFF_REL]
        self.assertEqual(entry.installed_at, "2000-01-01T00:00:00Z")

    def test_force_refused_by_a_symlinked_attic_keeps_its_prior_record(self):
        rc, _, err = self.install_fresh()
        self.assertEqual(rc, EXIT_OK, msg=err)
        self._set_prior_entry_stamp(HANDOFF_REL, "2000-01-01T00:00:00Z")
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        mine = self.repo_root / HANDOFF_REL
        mine.write_text('{"edited": true}\n')
        (self.repo_root / ".claude" / "maw").symlink_to(outside)
        rc, _, err = run_init(["--force"], repo_root=self.repo_root)
        self.assertIn("symlink", err)
        entry = read_state(self.repo_root).files[HANDOFF_REL]
        self.assertEqual(entry.installed_at, "2000-01-01T00:00:00Z",
                         "a refused overwrite was recorded as a fresh install")

    def test_a_symlinked_target_is_not_recorded(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        link = self.repo_root / HANDOFF_REL
        link.parent.mkdir(parents=True)
        link.symlink_to(outside / "x.json")
        rc, _, err = run_init([], repo_root=self.repo_root)
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertNotIn(HANDOFF_REL, read_state(self.repo_root).files)


if __name__ == "__main__":
    unittest.main()
