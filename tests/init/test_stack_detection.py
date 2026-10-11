"""Tests for init.stack_detection."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from init.stack_detection import (  # noqa: E402
    Detection,
    IsGitSubmoduleError,
    NotAGitRepoError,
    detect,
    is_xcode_env,
)


class StackTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Default: make it look like a git repo so safety gates pass
        (self.root / ".git").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class GitGateTests(StackTestBase):
    def setUp(self) -> None:
        # Override base setUp: don't pre-create .git
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def test_non_git_dir_refuses_by_default(self):
        with self.assertRaises(NotAGitRepoError):
            detect(self.root)

    def test_non_git_dir_with_no_git_flag_proceeds(self):
        d = detect(self.root, no_git=True)
        self.assertEqual(d.stack, "generic")

    def test_submodule_refuses_by_default(self):
        # Submodule has .git as a FILE pointing at parent's gitdir
        (self.root / ".git").write_text("gitdir: ../.git/modules/foo\n")
        with self.assertRaises(IsGitSubmoduleError):
            detect(self.root)

    def test_submodule_with_flag_proceeds(self):
        (self.root / ".git").write_text("gitdir: ../.git/modules/foo\n")
        d = detect(self.root, submodule_leaf=True)
        self.assertEqual(d.stack, "generic")

    def test_normal_git_repo_proceeds(self):
        (self.root / ".git").mkdir()
        d = detect(self.root)
        self.assertEqual(d.stack, "generic")


class NodeStackTests(StackTestBase):
    def test_next_detected(self):
        self._write("package.json", json.dumps({
            "name": "test", "dependencies": {"next": "14.0.0", "react": "18.0.0"}
        }))
        d = detect(self.root)
        self.assertEqual(d.stack, "node")
        self.assertEqual(d.framework, "next")  # next listed before react in NODE_FRAMEWORKS
        self.assertEqual(d.profile, "node-next")
        self.assertEqual(d.manifests_seen, ("package.json",))

    def test_react_without_next(self):
        self._write("package.json", json.dumps({
            "dependencies": {"react": "18.0.0"}
        }))
        d = detect(self.root)
        self.assertEqual(d.framework, "react")

    def test_express_detected(self):
        self._write("package.json", json.dumps({
            "dependencies": {"express": "4.0.0"}
        }))
        d = detect(self.root)
        self.assertEqual(d.framework, "express")

    def test_node_plain_no_known_framework(self):
        self._write("package.json", json.dumps({
            "dependencies": {"lodash": "4.0.0"}
        }))
        d = detect(self.root)
        self.assertEqual(d.framework, "plain")
        self.assertEqual(d.profile, "node-plain")

    def test_dev_dependencies_also_consulted(self):
        self._write("package.json", json.dumps({
            "devDependencies": {"next": "14.0.0"}
        }))
        d = detect(self.root)
        self.assertEqual(d.framework, "next")

    def test_corrupt_package_json_falls_back_to_plain(self):
        self._write("package.json", "{not valid json")
        d = detect(self.root)
        self.assertEqual(d.stack, "node")
        self.assertEqual(d.framework, "plain")


class PythonStackTests(StackTestBase):
    def test_flask_via_requirements_txt(self):
        self._write("requirements.txt", "flask==3.0.0\nrequests>=2.0\n")
        d = detect(self.root)
        self.assertEqual(d.stack, "python")
        self.assertEqual(d.framework, "flask")
        self.assertEqual(d.profile, "python-flask")
        self.assertIn("requirements.txt", d.manifests_seen)

    def test_fastapi_via_pyproject_pep621(self):
        self._write("pyproject.toml", '''
[project]
name = "x"
dependencies = ["fastapi>=0.100", "uvicorn"]
''')
        d = detect(self.root)
        self.assertEqual(d.framework, "fastapi")
        self.assertIn("pyproject.toml", d.manifests_seen)

    def test_django_via_requirements(self):
        self._write("requirements.txt", "Django==4.2\n")
        d = detect(self.root)
        self.assertEqual(d.framework, "django")  # case-insensitive

    def test_python_plain_no_known_framework(self):
        self._write("requirements.txt", "requests\n")
        d = detect(self.root)
        self.assertEqual(d.framework, "plain")

    def test_pyproject_poetry_layout(self):
        self._write("pyproject.toml", '''
[tool.poetry]
name = "x"
[tool.poetry.dependencies]
python = "^3.11"
flask = "^3.0"
''')
        d = detect(self.root)
        self.assertEqual(d.framework, "flask")

    def test_corrupt_pyproject_falls_back(self):
        self._write("pyproject.toml", "[project\nname = no")
        d = detect(self.root)
        self.assertEqual(d.stack, "python")
        self.assertEqual(d.framework, "plain")

    def test_requirements_comments_ignored(self):
        self._write("requirements.txt", "# a comment\n# flask  (commented)\nrequests\n")
        d = detect(self.root)
        self.assertEqual(d.framework, "plain")

    def test_requirements_with_markers(self):
        self._write("requirements.txt", "fastapi>=0.100; python_version >= '3.9'\n")
        d = detect(self.root)
        self.assertEqual(d.framework, "fastapi")

    def test_both_pyproject_and_requirements_seen(self):
        self._write("pyproject.toml", '[project]\nname="x"\n')
        self._write("requirements.txt", "flask\n")
        d = detect(self.root)
        self.assertEqual(d.framework, "flask")
        self.assertIn("pyproject.toml", d.manifests_seen)
        self.assertIn("requirements.txt", d.manifests_seen)


class RustStackTests(StackTestBase):
    def test_cargo_toml(self):
        self._write("Cargo.toml", '[package]\nname = "x"\nversion = "0.1.0"\n')
        d = detect(self.root)
        self.assertEqual(d.stack, "rust")
        self.assertEqual(d.framework, "plain")
        self.assertEqual(d.profile, "rust-plain")
        self.assertEqual(d.manifests_seen, ("Cargo.toml",))


class SwiftStackTests(StackTestBase):
    def test_package_swift(self):
        self._write("Package.swift", "// swift-tools-version:5.9\n")
        d = detect(self.root)
        self.assertEqual(d.stack, "swift")
        self.assertEqual(d.framework, "plain")

    def test_root_swift_files_only(self):
        self._write("App.swift", "// swift app\n")
        d = detect(self.root)
        self.assertEqual(d.stack, "swift")
        self.assertEqual(d.framework, "plain")


class PrecedenceTests(StackTestBase):
    """First-match-wins per PRD §4.1.1."""

    def test_node_wins_over_python(self):
        self._write("package.json", json.dumps({"dependencies": {"next": "14"}}))
        self._write("requirements.txt", "flask\n")
        d = detect(self.root)
        self.assertEqual(d.stack, "node")
        self.assertEqual(d.framework, "next")

    def test_python_wins_over_rust(self):
        self._write("requirements.txt", "flask\n")
        self._write("Cargo.toml", '[package]\nname="x"\n')
        d = detect(self.root)
        self.assertEqual(d.stack, "python")

    def test_rust_wins_over_swift(self):
        self._write("Cargo.toml", '[package]\nname="x"\n')
        self._write("Package.swift", "// x\n")
        d = detect(self.root)
        self.assertEqual(d.stack, "rust")


class GenericTests(StackTestBase):
    def test_empty_dir_is_generic(self):
        d = detect(self.root)
        self.assertEqual(d.stack, "generic")
        self.assertEqual(d.profile, "generic")
        self.assertEqual(d.manifests_seen, ())

    def test_random_files_dont_trigger_detection(self):
        self._write("README.md", "# x\n")
        self._write("Makefile", "all:\n\techo hi\n")
        d = detect(self.root)
        self.assertEqual(d.stack, "generic")


class ProfileNameTests(unittest.TestCase):
    """Detection.profile compositional behavior."""

    def test_profile_for_known_framework(self):
        self.assertEqual(
            Detection(stack="python", framework="flask").profile,
            "python-flask",
        )

    def test_profile_for_plain(self):
        self.assertEqual(
            Detection(stack="node", framework="plain").profile,
            "node-plain",
        )

    def test_profile_for_generic(self):
        self.assertEqual(
            Detection(stack="generic", framework="plain").profile,
            "generic",
        )


class IsXcodeEnvTests(StackTestBase):
    """is_xcode_env: the predicate the tools-awareness hook uses to decide
    whether to surface the Xcode MCP line. Broader than detect()'s swift
    branch — an app repo nests Swift under a target dir and carries an
    .xcodeproj/.xcworkspace/Podfile that detect() would call generic."""

    def test_xcodeproj_bundle_is_xcode(self):
        (self.root / "Gizmo.xcodeproj").mkdir()
        self.assertTrue(is_xcode_env(self.root))

    def test_xcworkspace_bundle_is_xcode(self):
        (self.root / "Gizmo.xcworkspace").mkdir()
        self.assertTrue(is_xcode_env(self.root))

    def test_package_swift_is_xcode(self):
        self._write("Package.swift", "// swift-tools-version:5.9\n")
        self.assertTrue(is_xcode_env(self.root))

    def test_podfile_is_xcode(self):
        self._write("Podfile", "platform :ios, '16.0'\n")
        self.assertTrue(is_xcode_env(self.root))

    def test_root_swift_source_is_xcode(self):
        self._write("App.swift", "import SwiftUI\n")
        self.assertTrue(is_xcode_env(self.root))

    def test_nested_swift_source_is_xcode(self):
        # The common layout: sources under a target dir, not at root.
        self._write("Sources/ContentView.swift", "import SwiftUI\n")
        self.assertTrue(is_xcode_env(self.root))

    def test_python_repo_is_not_xcode(self):
        self._write("requirements.txt", "flask\n")
        self.assertFalse(is_xcode_env(self.root))

    def test_node_repo_is_not_xcode(self):
        self._write("package.json", json.dumps({"dependencies": {"next": "14"}}))
        self.assertFalse(is_xcode_env(self.root))

    def test_empty_repo_is_not_xcode(self):
        self.assertFalse(is_xcode_env(self.root))

    def test_missing_dir_does_not_raise(self):
        # Pure predicate: never raises, even on a nonexistent path.
        self.assertFalse(is_xcode_env(self.root / "nope"))


if __name__ == "__main__":
    unittest.main()
