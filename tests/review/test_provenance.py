"""Tests for review.provenance — the build-from-SHA gate (backlog item 3)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from review import provenance  # noqa: E402


def _git(args: list[str], cwd: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: str) -> str:
    subprocess.run(["git", "init", "-b", "main", path], capture_output=True, check=True)
    _git(["config", "user.name", "t"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    (Path(path) / "README").write_text("seed\n", encoding="utf-8")
    _git(["add", "README"], path)
    _git(["commit", "-m", "seed"], path)
    return _git(["rev-parse", "HEAD"], path)


class RepoTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = str(Path(self.tmp.name) / "repo")
        os.makedirs(self.repo)
        self.sha = _init_repo(self.repo)
        self.addCleanup(self.tmp.cleanup)


class HeadAndCleanTest(RepoTestBase):
    def test_head_sha(self) -> None:
        self.assertEqual(provenance.head_sha(self.repo), self.sha)

    def test_is_clean_true_then_false(self) -> None:
        self.assertTrue(provenance.is_clean(self.repo))
        (Path(self.repo) / "README").write_text("changed\n", encoding="utf-8")
        self.assertFalse(provenance.is_clean(self.repo))


class StampTest(RepoTestBase):
    def setUp(self) -> None:
        super().setUp()
        # Realistic: the artifact is a build output OUTSIDE the repo (or
        # gitignored), so building it does not dirty the source tree.
        self.build = Path(self.tmp.name) / "build"
        self.build.mkdir()

    def test_stamp_file_artifact_writes_sidecar_beside(self) -> None:
        art = self.build / "app.bin"
        art.write_text("binary", encoding="utf-8")
        prov = provenance.stamp(str(art), self.repo, now="2026-06-16T00:00:00Z")
        self.assertEqual(prov.sha, self.sha)
        self.assertFalse(prov.dirty)  # repo is clean; the artifact lives outside it
        sidecar = art.with_name("app.bin" + provenance.PROVENANCE_FILENAME)
        self.assertTrue(sidecar.exists())

    def test_stamp_dir_artifact_writes_sidecar_inside(self) -> None:
        bundle = self.build / "App.app"
        bundle.mkdir()
        provenance.stamp(str(bundle), self.repo, now="2026-06-16T00:00:00Z")
        self.assertTrue((bundle / provenance.PROVENANCE_FILENAME).exists())

    def test_stamp_records_dirty(self) -> None:
        art = Path(self.repo) / "app.bin"
        art.write_text("binary", encoding="utf-8")  # untracked → dirty tree
        prov = provenance.stamp(str(art), self.repo, now="2026-06-16T00:00:00Z")
        self.assertTrue(prov.dirty)

    def test_stamp_missing_artifact_errors(self) -> None:
        with self.assertRaises(provenance.ProvenanceError):
            provenance.stamp(str(Path(self.repo) / "nope"), self.repo)


class VerifyTest(RepoTestBase):
    def _stamp(self, name: str = "app.bin") -> Path:
        art = Path(self.repo) / name
        art.write_text("x", encoding="utf-8")
        _git(["add", name], self.repo)
        _git(["commit", "-m", "add art"], self.repo)
        self.sha = _git(["rev-parse", "HEAD"], self.repo)
        provenance.stamp(str(art), self.repo, now="2026-06-16T00:00:00Z")
        return art

    def test_verify_ok_when_sha_matches_and_clean(self) -> None:
        art = self._stamp()
        res = provenance.verify(str(art), self.sha)
        self.assertTrue(res.ok)

    def test_verify_short_sha_prefix(self) -> None:
        art = self._stamp()
        res = provenance.verify(str(art), self.sha[:12])
        self.assertTrue(res.ok)

    def test_verify_rejects_empty_sha_gate_bypass(self) -> None:
        # Advisor CHANGES-REQUESTED: an empty/unset --sha ("$UNSET") must NOT
        # false-pass via startswith(''). This is the security teeth.
        art = self._stamp()
        for bad in ("", "   ", "\t\n"):
            res = provenance.verify(str(art), bad)
            self.assertFalse(res.ok, f"empty sha {bad!r} must not pass")
            self.assertIn("INVALID", res.reason)

    def test_verify_rejects_short_and_nonhex_sha(self) -> None:
        art = self._stamp()
        for bad in ("abc", "123456", "zzzzzzz", "HEAD"):  # <7 hex or non-hex
            res = provenance.verify(str(art), bad)
            self.assertFalse(res.ok, f"weak sha {bad!r} must not pass")

    def test_verify_reverse_prefix_branch_removed(self) -> None:
        # Old reverse branch exp.startswith(prov.sha) let a longer/garbage exp
        # pass against the recorded full SHA. A 41-hex exp must now be STALE.
        art = self._stamp()
        longer = self.sha + "a"  # 41 hex chars; old code passed, new must not
        res = provenance.verify(str(art), longer)
        self.assertFalse(res.ok)
        self.assertIn("STALE", res.reason)

    def test_verify_fails_when_missing(self) -> None:
        art = Path(self.repo) / "unstamped.bin"
        art.write_text("x", encoding="utf-8")
        res = provenance.verify(str(art), self.sha)
        self.assertFalse(res.ok)
        self.assertIn("NO provenance", res.reason)

    def test_verify_fails_when_stale(self) -> None:
        art = self._stamp()
        res = provenance.verify(str(art), "0" * 40)  # different SHA under review
        self.assertFalse(res.ok)
        self.assertIn("STALE", res.reason)

    def test_verify_fails_when_dirty(self) -> None:
        # stamp from a dirty tree (untracked artifact present)
        art = Path(self.repo) / "app.bin"
        art.write_text("x", encoding="utf-8")
        provenance.stamp(str(art), self.repo, now="2026-06-16T00:00:00Z")
        sha = provenance.head_sha(self.repo)
        res = provenance.verify(str(art), sha)
        self.assertFalse(res.ok)
        self.assertIn("DIRTY", res.reason)

    def test_verify_allows_dirty_when_opted_out(self) -> None:
        art = Path(self.repo) / "app.bin"
        art.write_text("x", encoding="utf-8")
        provenance.stamp(str(art), self.repo, now="2026-06-16T00:00:00Z")
        sha = provenance.head_sha(self.repo)
        res = provenance.verify(str(art), sha, require_clean=False)
        self.assertTrue(res.ok)

    def test_malformed_provenance_raises(self) -> None:
        art = Path(self.repo) / "app.bin"
        art.write_text("x", encoding="utf-8")
        sidecar = art.with_name("app.bin" + provenance.PROVENANCE_FILENAME)
        sidecar.write_text("{not json", encoding="utf-8")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.verify(str(art), self.sha)


class ArtifactBindingTest(RepoTestBase):
    """L0 #7: provenance binds the ARTIFACT (its digest), not just a sidecar."""

    def setUp(self) -> None:
        super().setUp()
        self.build = Path(self.tmp.name) / "build"
        self.build.mkdir()

    def _art(self, text: str = "binary v1") -> Path:
        art = self.build / "app.bin"
        art.write_text(text, encoding="utf-8")
        provenance.stamp(str(art), self.repo, now="2026-09-25T00:00:00Z")
        return art

    def test_replacing_the_artifact_after_stamping_fails(self) -> None:
        art = self._art()
        art.write_text("a different binary", encoding="utf-8")
        res = provenance.verify(str(art), self.sha)
        self.assertFalse(res.ok, res.reason)
        self.assertIn("digest", res.reason)

    def test_a_sidecar_without_its_artifact_fails(self) -> None:
        art = self._art()
        art.unlink()
        res = provenance.verify(str(art), self.sha)
        self.assertFalse(res.ok, res.reason)

    def test_a_directory_artifact_is_bound_to_its_contents(self) -> None:
        app = self.build / "My.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "exe").write_text("v1", encoding="utf-8")
        provenance.stamp(str(app), self.repo, now="2026-09-25T00:00:00Z")
        self.assertTrue(provenance.verify(str(app), self.sha).ok)
        (app / "Contents" / "exe").write_text("v2", encoding="utf-8")
        self.assertFalse(provenance.verify(str(app), self.sha).ok)

    def test_directory_digest_does_not_collide_across_the_path_content_boundary(self) -> None:
        a = self.build / "a.app"
        b = self.build / "b.app"
        a.mkdir(); b.mkdir()
        (a / "x").write_bytes(b"\0F\0y\0z")  # content that mimics a record
        (b / "x").write_bytes(b"")
        (b / "y").write_bytes(b"z")
        self.assertNotEqual(provenance.artifact_digest(a), provenance.artifact_digest(b))

    def test_null_dirty_is_malformed_not_clean(self) -> None:
        art = self._art()
        side = art.with_name(art.name + provenance.PROVENANCE_FILENAME)
        data = json.loads(side.read_text(encoding="utf-8"))
        data["dirty"] = None
        side.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.verify(str(art), self.sha)

    def test_a_non_hex_recorded_sha_is_malformed(self) -> None:
        art = self._art()
        side = art.with_name(art.name + provenance.PROVENANCE_FILENAME)
        data = json.loads(side.read_text(encoding="utf-8"))
        data["sha"] = self.sha[:10] + "ZZ"
        side.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.verify(str(art), self.sha[:10])


class BuildWrapperTest(RepoTestBase):
    """L0 #7: a controlled build captures source identity BEFORE building and
    refuses to stamp if it changed underneath the build."""

    def setUp(self) -> None:
        super().setUp()
        self.out = Path(self.tmp.name) / "out.bin"

    def test_build_stamps_the_identity_captured_before_the_build(self) -> None:
        prov = provenance.build(
            [sys.executable, "-c", f"open({str(self.out)!r}, 'w').write('built')"],
            str(self.out), self.repo, now="2026-09-25T00:00:00Z")
        self.assertEqual(prov.sha, self.sha)
        self.assertTrue(provenance.verify(str(self.out), self.sha).ok)

    def test_a_head_that_moves_during_the_build_is_refused(self) -> None:
        script = (f"import subprocess; open({str(self.out)!r}, 'w').write('built');"
                  f"subprocess.run(['git', '-C', {self.repo!r}, 'commit', '-q',"
                  f" '--allow-empty', '-m', 'moved'], check=True)")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.build([sys.executable, "-c", script], str(self.out), self.repo)
        side = self.out.with_name(self.out.name + provenance.PROVENANCE_FILENAME)
        self.assertFalse(side.exists(), "a build whose source moved was stamped")

    def test_a_failed_build_is_not_stamped(self) -> None:
        with self.assertRaises(provenance.ProvenanceError):
            provenance.build([sys.executable, "-c", "raise SystemExit(3)"],
                             str(self.out), self.repo)


class ProvenanceRobustnessTest(RepoTestBase):
    """Review follow-ups on #252: every failure is a ProvenanceError (a failed
    gate), the documented CLI form works, and the digest rules are strict."""

    def setUp(self) -> None:
        super().setUp()
        self.build = Path(self.tmp.name) / "build"
        self.build.mkdir()

    def test_an_unreadable_artifact_fails_as_provenance(self) -> None:
        art = self.build / "app.bin"
        art.write_text("x", encoding="utf-8")
        provenance.stamp(str(art), self.repo, now="t")
        os.chmod(art, 0)
        self.addCleanup(os.chmod, art, 0o644)
        if os.access(art, os.R_OK):
            self.skipTest("running as a user who can read mode-0 files")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.verify(str(art), self.sha)

    def test_an_unreadable_subdirectory_refuses_at_stamp(self) -> None:
        app = self.build / "My.app"
        (app / "locked").mkdir(parents=True)
        (app / "locked" / "f").write_text("x", encoding="utf-8")
        os.chmod(app / "locked", 0)
        self.addCleanup(os.chmod, app / "locked", 0o755)
        if os.access(app / "locked", os.R_OK):
            self.skipTest("running as a user who can read mode-0 dirs")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.stamp(str(app), self.repo, now="t")

    def test_a_non_utf8_sidecar_is_malformed(self) -> None:
        art = self.build / "app.bin"
        art.write_text("x", encoding="utf-8")
        art.with_name(art.name + provenance.PROVENANCE_FILENAME).write_bytes(b"\xff\xfe{")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.verify(str(art), self.sha)

    def test_a_top_level_symlink_to_a_file_is_resolved(self) -> None:
        real = self.build / "real.bin"
        real.write_text("v1", encoding="utf-8")
        link = self.build / "link.bin"
        link.symlink_to(real)
        provenance.stamp(str(link), self.repo, now="t")
        self.assertTrue(provenance.verify(str(link), self.sha).ok)
        real.write_text("v2", encoding="utf-8")
        self.assertFalse(provenance.verify(str(link), self.sha).ok)

    def test_an_allowed_dirty_pass_says_so(self) -> None:
        art = Path(self.repo) / "app.bin"
        art.write_text("x", encoding="utf-8")
        provenance.stamp(str(art), self.repo, now="t")
        res = provenance.verify(str(art), provenance.head_sha(self.repo),
                                require_clean=False)
        self.assertTrue(res.ok)
        self.assertIn("dirty, allowed", res.reason)

    def test_the_documented_build_cli_order_works(self) -> None:
        out = Path(self.tmp.name) / "out.bin"
        rc = provenance.main(["build", str(out), "--repo", self.repo, "--",
                              sys.executable, "-c",
                              f"open({str(out)!r}, 'w').write('b')"])
        self.assertEqual(rc, 0)
        self.assertTrue(provenance.verify(str(out), self.sha).ok)


class CliTest(RepoTestBase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = provenance.main(argv)
        return rc, out.getvalue()

    def test_stamp_then_verify(self) -> None:
        art = Path(self.repo) / "app.bin"
        art.write_text("x", encoding="utf-8")
        _git(["add", "app.bin"], self.repo)
        _git(["commit", "-m", "art"], self.repo)
        sha = _git(["rev-parse", "HEAD"], self.repo)
        rc, out = self._run(["stamp", str(art), "--repo", self.repo])
        self.assertEqual(rc, 0)
        self.assertIn("stamped", out)
        rc, out = self._run(["verify", str(art), "--sha", sha])
        self.assertEqual(rc, 0)

    def test_verify_stale_exit_2(self) -> None:
        art = Path(self.repo) / "app.bin"
        art.write_text("x", encoding="utf-8")
        provenance.stamp(str(art), self.repo, now="2026-06-16T00:00:00Z")
        rc, out = self._run(["verify", str(art), "--sha", "0" * 40, "--allow-dirty"])
        self.assertEqual(rc, 2)
        self.assertIn("STALE", out)

    def test_head_interpreted_target(self) -> None:
        sha = provenance.head_sha(self.repo)
        rc, out = self._run(["head", "--sha", sha, "--repo", self.repo])
        self.assertEqual(rc, 0)
        rc, out = self._run(["head", "--sha", "0" * 40, "--repo", self.repo])
        self.assertEqual(rc, 2)

    def test_head_rejects_empty_sha(self) -> None:
        # The cmd_head half of the gate bypass — `--sha "$UNSET"`.
        rc, _ = self._run(["head", "--sha", "", "--repo", self.repo])
        self.assertEqual(rc, 2)

    def test_verify_empty_sha_exit_2(self) -> None:
        art = Path(self.repo) / "app.bin"
        art.write_text("x", encoding="utf-8")
        provenance.stamp(str(art), self.repo, now="2026-06-16T00:00:00Z")
        rc, out = self._run(["verify", str(art), "--sha", ""])
        self.assertEqual(rc, 2)
        self.assertIn("INVALID", out)


if __name__ == "__main__":
    unittest.main()
