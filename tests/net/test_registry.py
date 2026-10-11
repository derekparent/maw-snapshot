"""Tests for net.registry — registration, discovery, crash-safe pruning."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from net import registry  # noqa: E402


def fake_runner_factory(responses: dict[str, tuple[int, str]]):
    """Map 'cmd arg0' prefixes to (returncode, stdout)."""

    def runner(cmd: list[str], stdin_text: str | None = None):
        key = " ".join(cmd[:2])
        rc, out = responses.get(key, (1, ""))
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    return runner


def foreign_live_pid(case: unittest.TestCase) -> int:
    """A live pid that is never this process's session: a helper child. The
    literal 1 is NOT that: a suite whose parent is pid 1 (launched detached,
    then re-parented) resolves its own lineage to 1 through the ppid fallback,
    and "a foreign holder at pid 1" becomes this process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])

    def stop() -> None:
        proc.kill()
        proc.wait()

    case.addCleanup(stop)
    return proc.pid


class RefusalAuditAndLockTest(unittest.TestCase):
    """Registry hardening (2026-06-12): NET-3 per-name register lock +
    succession item (e) — refused registry claims leave an audit artifact,
    parity with wire.record_refusal (gate #8)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self.tmp.name})
        self.env.start()
        self._tmux = os.environ.pop("TMUX_PANE", None)

    def tearDown(self) -> None:
        if self._tmux is not None:
            os.environ["TMUX_PANE"] = self._tmux
        self.env.stop()
        self.tmp.cleanup()

    def _register(self, name: str, role: str = "tester"):
        return registry.register(name=name, role=role,
                                 runner=fake_runner_factory({}))

    def _refusals(self) -> list[dict]:
        f = registry.registry_refusals_file()
        if not f.exists():
            return []
        return [json.loads(line) for line in
                f.read_text(encoding="utf-8").splitlines()]

    def test_register_collision_refusal_is_audited(self) -> None:
        self._register("alpha")
        stolen = registry.load_entry("alpha")
        assert stolen is not None
        stolen.pid = foreign = foreign_live_pid(self)
        registry._write_entry(stolen)
        with self.assertRaises(registry.NetRegistryError):
            self._register("alpha")
        recs = self._refusals()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "register_name_collision")
        self.assertEqual(recs[0]["name"], "alpha")
        self.assertEqual(recs[0]["owner_pid"], foreign)
        self.assertIn("ts", recs[0])

    def test_heartbeat_lineage_refusal_is_audited(self) -> None:
        self._register("alpha")
        with mock.patch.object(registry, "verify_caller_is", return_value=False):
            with self.assertRaises(registry.NetRegistryError):
                registry.heartbeat("alpha", runner=fake_runner_factory({}))
        recs = self._refusals()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "heartbeat_lineage")
        self.assertEqual(recs[0]["name"], "alpha")
        self.assertIn("masks a dark peer", recs[0]["reason"])

    def test_heartbeat_unknown_name_not_audited(self) -> None:
        # Unknown-name is an operator typo, not a security refusal — it
        # must not pollute the refusals artifact.
        with self.assertRaises(registry.NetRegistryError):
            registry.heartbeat("nobody", runner=fake_runner_factory({}))
        self.assertEqual(self._refusals(), [])

    def test_successful_register_leaves_no_refusal(self) -> None:
        self._register("alpha")
        self.assertEqual(self._refusals(), [])

    def test_register_blocks_while_name_lock_held(self) -> None:
        # NET-3 deterministic serialization proof: flock conflicts apply
        # across fds, so a thread re-acquiring the same lock path blocks.
        from bridge.format import _acquire_lock, _release_lock
        registry.registry_dir().mkdir(parents=True, exist_ok=True)
        fd = _acquire_lock(registry.registry_dir() / ".alpha.reglock")
        done = threading.Event()
        result: dict = {}

        def worker() -> None:
            try:
                result["entry"] = self._register("alpha")
            except Exception as e:  # surfaced via assertion below
                result["error"] = e
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            self.assertFalse(
                done.wait(timeout=0.8),
                "register must block while the per-name lock is held",
            )
        finally:
            _release_lock(fd)
        self.assertTrue(done.wait(timeout=30))
        t.join(timeout=5)
        self.assertNotIn("error", result, result.get("error"))
        self.assertIsNotNone(registry.load_entry("alpha"))


class RegistryDirTest(unittest.TestCase):
    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"MAW_NET_DIR": "/tmp/x"}):
            self.assertEqual(registry.net_dir(), Path("/tmp/x"))
            self.assertEqual(registry.registry_dir(), Path("/tmp/x/registry"))

    def test_default_under_claude_bridge(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAW_NET_DIR", None)
            self.assertTrue(
                str(registry.net_dir()).endswith(".claude/bridge/switchboard")
            )


class RegistryLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self.tmp.name})
        self.env.start()
        # detect_tmux_pane() consults TMUX_PANE before the runner, so a real
        # tmux env (running the suite from inside tmux — the operational rule)
        # would leak past the mock and record a real pane. Scrub it so these
        # tests are env-independent. patch.dict restores the full env on stop.
        os.environ.pop("TMUX_PANE", None)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _register(self, name: str = "alpha", role: str = "executor") -> registry.Entry:
        # runner that reports no claude ancestor and no tmux server
        runner = fake_runner_factory({})
        return registry.register(name=name, role=role, runner=runner)

    def test_register_and_load(self) -> None:
        entry = self._register()
        loaded = registry.load_entry("alpha")
        assert loaded is not None
        self.assertEqual(loaded.name, "alpha")
        self.assertEqual(loaded.role, "executor")
        self.assertEqual(loaded.pid, entry.pid)
        self.assertFalse(loaded.tmux_push)

    def test_invalid_name_rejected(self) -> None:
        with self.assertRaises(registry.NetRegistryError):
            self._register(name="Bad Name!")

    def test_who_lists_live_and_prunes_dead(self) -> None:
        self._register("alpha")
        self._register("beta", role="advisor")
        # corpse: valid entry whose pid can't exist
        corpse = registry.registry_dir() / "ghost.json"
        data = json.loads((registry.registry_dir() / "alpha.json").read_text())
        data.update(name="ghost", pid=2**22 + 1)
        corpse.write_text(json.dumps(data))

        names = {e.name for e in registry.who()}
        self.assertEqual(names, {"alpha", "beta"})
        self.assertFalse(corpse.exists(), "dead entry must be pruned at read time")

    def test_who_skips_torn_files_without_crashing(self) -> None:
        self._register("alpha")
        (registry.registry_dir() / "torn.json").write_text("{not json")
        names = {e.name for e in registry.who()}
        self.assertEqual(names, {"alpha"})

    def test_name_collision_with_live_holder_rejected(self) -> None:
        self._register("alpha")
        stolen = registry.load_entry("alpha")
        assert stolen is not None
        stolen.pid = foreign_live_pid(self)
        registry._write_entry(stolen)
        with self.assertRaises(registry.NetRegistryError):
            self._register("alpha")

    def test_collision_is_refused_when_this_process_was_reparented_to_pid_1(self) -> None:
        # A detached suite: the parent is pid 1, so the ppid fallback names 1
        # as this session's owner. A foreign live holder is still refused.
        with mock.patch.object(registry.os, "getppid", return_value=1):
            own = self._register("alpha")
            self.assertEqual(own.pid, 1)
            stolen = registry.load_entry("alpha")
            assert stolen is not None
            stolen.pid = foreign_live_pid(self)
            registry._write_entry(stolen)
            with self.assertRaises(registry.NetRegistryError):
                self._register("alpha")

    def test_reregister_same_pid_keeps_started_at(self) -> None:
        first = self._register("alpha")
        second = self._register("alpha")
        self.assertEqual(first.started_at, second.started_at)

    def test_heartbeat_updates_last_seen(self) -> None:
        self._register("alpha")
        before = registry.load_entry("alpha")
        assert before is not None
        # Same fake lineage as registration: this caller IS the owner.
        entry = registry.heartbeat("alpha", runner=fake_runner_factory({}))
        self.assertGreaterEqual(entry.last_seen, before.last_seen)

    def test_heartbeat_unknown_raises(self) -> None:
        with self.assertRaises(registry.NetRegistryError):
            registry.heartbeat("nobody", runner=fake_runner_factory({}))

    def test_foreign_heartbeat_refused(self) -> None:
        # The masking hole (advisor, PR #7 review): a same-named session
        # elsewhere refreshing an entry it doesn't own would hide a
        # genuinely dark peer from net.watch. Entry owner here is the
        # ppid-fallback lineage; the foreign caller resolves to a claude
        # ancestor at a different pid.
        self._register("alpha")
        foreign = fake_runner_factory(
            {"ps -o": (0, "1 ttys009 claude --resume")}
        )
        with self.assertRaises(registry.NetRegistryError) as ctx:
            registry.heartbeat("alpha", runner=foreign)
        self.assertIn("owned by", str(ctx.exception))
        # last_seen must be untouched by the refused attempt
        entry = registry.load_entry("alpha")
        assert entry is not None

    def test_unregister(self) -> None:
        self._register("alpha")
        self.assertTrue(registry.unregister("alpha"))
        self.assertFalse(registry.unregister("alpha"))
        self.assertIsNone(registry.load_entry("alpha"))

    def test_is_stale(self) -> None:
        entry = self._register("alpha")
        self.assertFalse(registry.is_stale(entry))
        entry.last_seen = "2020-01-01T00:00:00Z"
        self.assertTrue(registry.is_stale(entry))
        entry.last_seen = "garbage"
        self.assertTrue(registry.is_stale(entry))


class DetectionTest(unittest.TestCase):
    def test_tmux_pane_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"TMUX_PANE": "%7"}):
            self.assertEqual(registry.detect_tmux_pane("/dev/ttys001"), "%7")

    def test_tmux_pane_from_tty_match(self) -> None:
        runner = fake_runner_factory(
            {"tmux list-panes": (0, "%0 /dev/ttys004\n%1 /dev/ttys009\n")}
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX_PANE", None)
            self.assertEqual(
                registry.detect_tmux_pane("/dev/ttys009", runner=runner), "%1"
            )
            self.assertIsNone(
                registry.detect_tmux_pane("/dev/ttys111", runner=runner)
            )

    def test_no_tmux_server_means_no_pane(self) -> None:
        runner = fake_runner_factory({"tmux list-panes": (1, "")})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX_PANE", None)
            self.assertIsNone(
                registry.detect_tmux_pane("/dev/ttys004", runner=runner)
            )

    def test_find_claude_ancestor_walks_tree(self) -> None:
        me = os.getpid()
        rows = {
            me: (100, "ttys004", "python3 -m net.cli register"),
            100: (50, "ttys004", "/bin/zsh -il"),
            50: (1, "ttys004", "claude"),
        }

        def runner(cmd: list[str], stdin_text: str | None = None):
            pid = int(cmd[-1])
            if pid in rows:
                ppid, tty, command = rows[pid]
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{ppid} {tty} {command}\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        pid, tty = registry.find_claude_ancestor(runner=runner)
        self.assertEqual(pid, 50)
        self.assertEqual(tty, "/dev/ttys004")

    def test_find_claude_ancestor_fallback_is_parent(self) -> None:
        runner = fake_runner_factory({})  # ps always fails
        pid, tty = registry.find_claude_ancestor(runner=runner)
        self.assertEqual(pid, os.getppid())
        self.assertIsNone(tty)

    def _lineage(self, rows: dict, registered_pid: int) -> str:
        def runner(cmd: list[str], stdin_text: str | None = None):
            pid = int(cmd[-1])
            if pid in rows:
                ppid, tty, command = rows[pid]
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{ppid} {tty} {command}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        entry = registry.Entry(
            name="me", role="executor", repo_root="/r", cwd="/r",
            pid=registered_pid, tty=None, tmux_pane=None, session_id="s",
            started_at="2026-06-10T06:00:00Z", last_seen="2026-06-10T06:00:00Z",
        )
        return registry.caller_lineage(entry, runner=runner, start_pid=900)

    def test_caller_lineage_tells_an_unfinished_walk_from_another_session(self) -> None:
        tree = {900: (100, "ttys004", "python3 -m net.cli recv x --check"),
                100: (50, "ttys004", "/bin/sh maw-watch-inbox x"),
                50: (1, "ttys004", "claude")}
        self.assertEqual(self._lineage(tree, 50), "match")
        self.assertEqual(self._lineage(tree, 51), "mismatch")
        # one `ps` call failed part-way up
        self.assertEqual(self._lineage({900: tree[900]}, 50), "unknown")
        self.assertEqual(self._lineage({}, 50), "unknown")
        # the watcher's shell was re-parented: pid 1 before any claude
        orphan = {900: (100, "??", "python3 -m net.cli recv x --check"),
                  100: (1, "??", "/bin/sh maw-watch-inbox x")}
        self.assertEqual(self._lineage(orphan, 50), "unknown")
        # a registered pid that is on the path but is not the claude process
        # is not a match: only the nearest claude ancestor counts
        self.assertEqual(self._lineage(tree, 100), "mismatch")

    def test_verify_caller_pins_identity_to_lineage(self) -> None:
        runner = fake_runner_factory({})  # ancestor resolves to getppid()
        own = registry.Entry(
            name="me", role="executor", repo_root="/r", cwd="/r",
            pid=os.getppid(), tty=None, tmux_pane=None, session_id="s",
            started_at="2026-06-10T06:00:00Z", last_seen="2026-06-10T06:00:00Z",
        )
        self.assertTrue(registry.verify_caller_is(own, runner=runner))
        impostor = registry.Entry(
            name="other", role="advisor", repo_root="/r", cwd="/r",
            pid=foreign_live_pid(self), tty=None, tmux_pane=None, session_id="s2",
            started_at="2026-06-10T06:00:00Z", last_seen="2026-06-10T06:00:00Z",
        )
        self.assertFalse(registry.verify_caller_is(impostor, runner=runner))


class LogicalRepoIdentityTest(unittest.TestCase):
    """repo_common_dir / same_repo — the switchboard recognizing N per-seat
    worktrees as one logical repo (backlog item 1, acceptance #3)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self.tmp.name})
        self.env.start()
        os.environ.pop("TMUX_PANE", None)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_git_common_dir_empty_when_git_unavailable(self) -> None:
        runner = fake_runner_factory({})  # 'git rev-parse' → (1, '')
        self.assertEqual(registry.git_common_dir(runner=runner), "")

    def test_git_common_dir_returns_abs_path(self) -> None:
        runner = fake_runner_factory({"git rev-parse": (0, "/shared/repo/.git")})
        self.assertEqual(registry.git_common_dir(runner=runner), "/shared/repo/.git")

    def test_register_records_common_dir(self) -> None:
        runner = fake_runner_factory({"git rev-parse": (0, "/shared/repo/.git")})
        registry.register(name="alpha", role="executor", runner=runner)
        loaded = registry.load_entry("alpha")
        assert loaded is not None
        self.assertEqual(loaded.repo_common_dir, "/shared/repo/.git")

    def test_two_worktrees_recognized_as_one_repo(self) -> None:
        # Distinct repo_root (two per-seat worktrees) but the SAME shared .git
        # → same_repo True, and both appear in who(): discover-by-role for
        # stand-down still works when repo_roots differ.
        runner = fake_runner_factory({"git rev-parse": (0, "/shared/repo/.git")})
        registry.register(
            name="advisor", role="advisor",
            repo_root="/home/u/.maw-worktrees/repo-advisor", runner=runner,
        )
        registry.register(
            name="executor", role="executor",
            repo_root="/home/u/.maw-worktrees/repo-executor", runner=runner,
        )
        adv = registry.load_entry("advisor")
        exe = registry.load_entry("executor")
        assert adv is not None and exe is not None
        self.assertNotEqual(adv.repo_root, exe.repo_root)
        self.assertTrue(registry.same_repo(adv, exe))
        names = {e.name for e in registry.who()}
        self.assertEqual(names, {"advisor", "executor"})

    def test_same_repo_distinct_repos_false(self) -> None:
        a = registry.Entry(
            name="a", role="executor", repo_root="/x", cwd="/x", pid=1,
            tty=None, tmux_pane=None, session_id="s", started_at="t",
            last_seen="t", repo_common_dir="/repoA/.git",
        )
        b = registry.Entry(
            name="b", role="advisor", repo_root="/y", cwd="/y", pid=1,
            tty=None, tmux_pane=None, session_id="s", started_at="t",
            last_seen="t", repo_common_dir="/repoB/.git",
        )
        self.assertFalse(registry.same_repo(a, b))

    def test_same_repo_falls_back_to_repo_root(self) -> None:
        # Entries written before repo_common_dir existed (default '') compare
        # on repo_root — backward-compatible behavior.
        a = registry.Entry(
            name="a", role="executor", repo_root="/same", cwd="/same", pid=1,
            tty=None, tmux_pane=None, session_id="s", started_at="t", last_seen="t",
        )
        b = registry.Entry(
            name="b", role="advisor", repo_root="/same", cwd="/same", pid=1,
            tty=None, tmux_pane=None, session_id="s", started_at="t", last_seen="t",
        )
        self.assertEqual(a.repo_common_dir, "")
        self.assertTrue(registry.same_repo(a, b))

    def test_entry_loads_without_repo_common_dir_field(self) -> None:
        # MIGRATION: a registry file written by cached code (no
        # repo_common_dir key) must still load — defaulted field, not a crash.
        legacy = {
            "name": "legacy", "role": "executor", "repo_root": "/r", "cwd": "/r",
            "pid": os.getpid(), "tty": None, "tmux_pane": None, "session_id": "s",
            "started_at": "2026-06-10T06:00:00Z", "last_seen": "2026-06-10T06:00:00Z",
            "transport": {"tmux_push": False},
        }
        path = registry.registry_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
        loaded = registry.load_entry("legacy")
        assert loaded is not None
        self.assertEqual(loaded.repo_common_dir, "")
        # and it survives a who() pass (read path)
        self.assertIn("legacy", {e.name for e in registry.who()})

    def test_entry_loads_with_unknown_future_field(self) -> None:
        # FORWARD-COMPAT (the live bug, 2026-06-16): a registry file written by
        # NEWER code (an added key the current reader doesn't know) must STILL
        # load — not be silently skipped, which read 'no live sessions' and
        # nearly took the pair dark. Unknown keys are ignored, not fatal.
        future = {
            "name": "fut", "role": "executor", "repo_root": "/r", "cwd": "/r",
            "pid": os.getpid(), "tty": None, "tmux_pane": None, "session_id": "s",
            "started_at": "2026-06-10T06:00:00Z", "last_seen": "2026-06-10T06:00:00Z",
            "transport": {"tmux_push": False},
            "repo_common_dir": "/r/.git",
            "some_future_field": "from a newer schema",  # the key that broke strict readers
        }
        path = registry.registry_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "fut.json").write_text(json.dumps(future), encoding="utf-8")
        loaded = registry.load_entry("fut")
        assert loaded is not None
        self.assertEqual(loaded.name, "fut")
        self.assertEqual(loaded.repo_common_dir, "/r/.git")
        self.assertIn("fut", {e.name for e in registry.who()})

    def test_torn_entry_missing_required_field_still_skipped(self) -> None:
        # The tolerant reader must NOT swallow genuine garbage: a file missing a
        # REQUIRED field is still treated as torn (fail-loud preserved).
        path = registry.registry_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "torn.json").write_text(json.dumps({"name": "torn"}), encoding="utf-8")
        self.assertIsNone(registry.load_entry("torn"))
        self.assertNotIn("torn", {e.name for e in registry.who()})


class HerdrMuxTest(unittest.TestCase):
    """A1 — the mux field: a seat records whether it is tmux- or herdr-hosted."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ, {"MAW_NET_DIR": self.tmp.name}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        for var in ("TMUX_PANE", "HERDR_ENV", "HERDR_PANE_ID"):
            os.environ.pop(var, None)

    def test_detect_herdr_pane(self) -> None:
        self.assertIsNone(registry.detect_herdr_pane({}))
        self.assertIsNone(registry.detect_herdr_pane({"HERDR_ENV": "1"}))  # no pane id
        self.assertIsNone(
            registry.detect_herdr_pane({"HERDR_PANE_ID": "1-2"})  # not in herdr
        )
        self.assertEqual(
            registry.detect_herdr_pane({"HERDR_ENV": "1", "HERDR_PANE_ID": "1-2"}),
            "1-2",
        )

    def test_register_tmux_seat_is_mux_tmux(self) -> None:
        entry = registry.register(
            name="tmuxseat", role="executor", runner=fake_runner_factory({})
        )
        self.assertEqual(entry.mux, "tmux")
        self.assertIsNone(entry.herdr_pane)
        self.assertFalse(entry.herdr_push)

    def test_register_herdr_seat_records_mux_and_pane(self) -> None:
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "2-1"
        entry = registry.register(
            name="herdrseat", role="advisor", runner=fake_runner_factory({})
        )
        self.assertEqual(entry.mux, "herdr")
        self.assertEqual(entry.herdr_pane, "2-1")
        self.assertTrue(entry.herdr_push)
        self.assertTrue(entry.pushable)
        # round-trips through the registry file
        loaded = registry.load_entry("herdrseat")
        self.assertEqual(loaded.mux, "herdr")
        self.assertEqual(loaded.herdr_pane, "2-1")

    def test_pre_herdr_entry_loads_with_tmux_default(self) -> None:
        # Forward-compat: a registry file written before the mux field loads
        # with mux="tmux" (no migration, Postel).
        path = registry.registry_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "old.json").write_text(
            json.dumps(
                {
                    "name": "old", "role": "executor", "repo_root": "/r",
                    "cwd": "/r", "pid": os.getpid(), "tty": None,
                    "tmux_pane": "%3", "session_id": "s", "started_at": "t",
                    "last_seen": "t", "transport": {"tmux_push": True},
                }
            ),
            encoding="utf-8",
        )
        loaded = registry.load_entry("old")
        self.assertEqual(loaded.mux, "tmux")
        self.assertIsNone(loaded.herdr_pane)

    def test_register_records_pinned_model(self) -> None:
        os.environ["MAW_SEAT_MODEL"] = "claude-fable-5"
        try:
            entry = registry.register(
                name="advseat", role="advisor", runner=fake_runner_factory({})
            )
        finally:
            os.environ.pop("MAW_SEAT_MODEL", None)
        self.assertEqual(entry.model, "claude-fable-5")
        self.assertEqual(registry.load_entry("advseat").model, "claude-fable-5")

    def test_register_without_pinned_model_is_blank(self) -> None:
        os.environ.pop("MAW_SEAT_MODEL", None)
        entry = registry.register(
            name="bareseat", role="executor", runner=fake_runner_factory({})
        )
        self.assertEqual(entry.model, "")

    def test_herdr_pane_consistent_tell(self) -> None:
        herdr = registry.Entry(
            name="a", role="advisor", repo_root="/r", cwd="/r", pid=1, tty=None,
            tmux_pane=None, session_id="s", started_at="t", last_seen="t",
            mux="herdr", herdr_pane="2-1",
        )
        tmux = registry.Entry(
            name="b", role="executor", repo_root="/r", cwd="/r", pid=1, tty=None,
            tmux_pane="%1", session_id="s", started_at="t", last_seen="t",
        )
        # not a herdr entry, or no live env -> N/A (None)
        self.assertIsNone(registry.herdr_pane_consistent(tmux, {"HERDR_ENV": "1", "HERDR_PANE_ID": "2-1"}))
        self.assertIsNone(registry.herdr_pane_consistent(herdr, {}))
        # live env matches / mismatches the registered pane
        self.assertTrue(
            registry.herdr_pane_consistent(herdr, {"HERDR_ENV": "1", "HERDR_PANE_ID": "2-1"})
        )
        self.assertFalse(
            registry.herdr_pane_consistent(herdr, {"HERDR_ENV": "1", "HERDR_PANE_ID": "9-9"})
        )




class ReregisterKeepsSessionIdTest(unittest.TestCase):
    """#212: a SessionStart re-register inside the SAME claude process
    (compaction, /clear) must keep the session_id the armed token is bound to;
    a different or recycled process must never inherit it."""

    SENTINEL = "advisor-original-session"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self.tmp.name})
        self.env.start()
        os.environ.pop("TMUX_PANE", None)
        os.environ.pop("MAW_SESSION_ID", None)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _runner(start_offset_s: float | None):
        """ps lineage finds no claude (ppid fallback); `ps -p` reports a
        process start `start_offset_s` from now, or fails when None."""
        responses: dict[str, tuple[int, str]] = {}
        if start_offset_s is not None:
            lstart = time.strftime(
                "%a %b %d %H:%M:%S %Y", time.localtime(time.time() + start_offset_s)
            )
            responses["ps -p"] = (0, lstart + "\n")
        return fake_runner_factory(responses)

    def _seed(self, *, pid: int | None = None, transcript: str = "/t/orig.jsonl") -> None:
        """An existing advisor entry, first registered an hour ago."""
        registry.register(name="advisor", role="advisor",
                          transcript_path=transcript, runner=self._runner(-7200))
        data = json.loads((registry.registry_dir() / "advisor.json").read_text())
        data["session_id"] = self.SENTINEL
        data["started_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        if pid is not None:
            data["pid"] = pid
        (registry.registry_dir() / "advisor.json").write_text(json.dumps(data))

    def _reregister(self, start_offset_s: float | None, transcript: str = "", **kw):
        return registry.register(name="advisor", role="advisor",
                                 transcript_path=transcript,
                                 runner=self._runner(start_offset_s), **kw)

    def test_same_process_reregister_keeps_session_id_and_transcript(self) -> None:
        self._seed()
        entry = self._reregister(-7200)
        self.assertEqual(entry.session_id, self.SENTINEL)
        self.assertEqual(registry.load_entry("advisor").session_id, self.SENTINEL)
        self.assertEqual(entry.transcript_path, "/t/orig.jsonl")

    def test_same_process_fresh_transcript_path_wins(self) -> None:
        self._seed()
        entry = self._reregister(-7200, transcript="/t/new.jsonl")
        self.assertEqual(entry.session_id, self.SENTINEL)
        self.assertEqual(entry.transcript_path, "/t/new.jsonl")

    def test_recycled_pid_does_not_inherit(self) -> None:
        # Same pid, but the process started AFTER the entry was first
        # registered: the old owner died and the pid was reused.
        self._seed()
        entry = self._reregister(-60)
        self.assertNotEqual(entry.session_id, self.SENTINEL)
        self.assertEqual(entry.transcript_path, "")

    def test_unreadable_start_time_does_not_inherit(self) -> None:
        self._seed()
        entry = self._reregister(None)
        self.assertNotEqual(entry.session_id, self.SENTINEL)

    def test_dead_different_pid_does_not_inherit(self) -> None:
        self._seed(pid=2**22 + 1)
        entry = self._reregister(-7200)
        self.assertNotEqual(entry.session_id, self.SENTINEL)
        self.assertEqual(entry.transcript_path, "")

    def test_live_different_pid_still_refused(self) -> None:
        self._seed(pid=foreign_live_pid(self))
        with self.assertRaises(registry.NetRegistryError):
            self._reregister(-7200)
        self.assertEqual(registry.load_entry("advisor").session_id, self.SENTINEL)

    def test_other_seat_name_does_not_inherit(self) -> None:
        self._seed()
        entry = registry.register(name="executor", role="executor",
                                  runner=self._runner(-7200))
        self.assertNotEqual(entry.session_id, self.SENTINEL)

    def test_no_existing_entry_mints(self) -> None:
        entry = self._reregister(-7200)
        self.assertTrue(entry.session_id.startswith("advisor-"))
        self.assertNotEqual(entry.session_id, self.SENTINEL)

    def test_explicit_session_id_wins(self) -> None:
        self._seed()
        entry = self._reregister(-7200, session_id="explicit-sid")
        self.assertEqual(entry.session_id, "explicit-sid")

    def test_arm_identity_survives_hook_reregister(self) -> None:
        from guards import session_auth
        self._seed()
        before, why = session_auth._registry_identity("advisor", runner=self._runner(-7200))
        self.assertIsNotNone(before, why)
        self._reregister(-7200, transcript="/t/after-compact.jsonl")
        after, why = session_auth._registry_identity("advisor", runner=self._runner(-7200))
        self.assertEqual(before, after, why)
        self.assertEqual(after[0], self.SENTINEL)


class SameSecondSessionIdTest(unittest.TestCase):
    """#338: the grid launcher brings seats up in the same second, and every
    pair names its reviewer `advisor`. Two such seats on two switchboards (two
    repos) must still get distinct session ids, or a batch manifest refuses."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clean = mock.patch.dict(os.environ, {}, clear=False)
        clean.start()
        self.addCleanup(clean.stop)
        for k in ("TMUX_PANE", "MAW_SESSION_ID", "HERDR_ENV", "HERDR_PANE_ID"):
            os.environ.pop(k, None)

    def _register_in(self, repo: str) -> "registry.Entry":
        with mock.patch.dict(os.environ, {"MAW_NET_DIR": os.path.join(self.tmp.name, repo)}), \
             mock.patch.object(registry, "utcnow_iso", return_value="2026-10-02T20:06:39Z"):
            return registry.register(name="advisor", role="advisor",
                                     repo_root=f"/r/{repo}",
                                     runner=fake_runner_factory({}))

    def test_two_repos_same_second_get_distinct_ids(self) -> None:
        a, b = self._register_in("maw"), self._register_in("peer-repo")
        self.assertNotEqual(a.session_id, b.session_id)
        for e in (a, b):
            self.assertTrue(e.session_id.startswith("advisor-20261002T200639Z-"), e.session_id)

    def test_minted_id_is_batch_line_safe(self) -> None:
        sid = registry.mint_session_id("advisor", "2026-10-02T20:06:39Z")
        self.assertRegex(sid, r"^advisor-20261002T200639Z-[0-9a-f]{6}$")


class ProcStartEpochTest(unittest.TestCase):
    def test_parses_lstart(self) -> None:
        r = fake_runner_factory({"ps -p": (0, "Tue Sep 22 13:29:20 2026\n")})
        got = registry.proc_start_epoch(123, runner=r)
        self.assertEqual(got, time.mktime(time.strptime(
            "Tue Sep 22 13:29:20 2026", "%a %b %d %H:%M:%S %Y")))

    def test_failure_and_garbage_are_none(self) -> None:
        self.assertIsNone(registry.proc_start_epoch(1, runner=fake_runner_factory({})))
        bad = fake_runner_factory({"ps -p": (0, "not a date\n")})
        self.assertIsNone(registry.proc_start_epoch(1, runner=bad))

if __name__ == "__main__":
    unittest.main()


class TombstoneTest(unittest.TestCase):
    """#352: unregister leaves the transcript pointer for a late counter-audit."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"MAW_NET_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _entry(self):
        now = registry.utcnow_iso()
        return registry.Entry(
            name="advisor", role="advisor", repo_root="/r", cwd="/r",
            pid=os.getpid(), tty="/dev/ttys001", tmux_pane=None,
            session_id="s1", started_at=now, last_seen=now,
            transport={}, transcript_path="/t/advisor.jsonl")

    def test_unregister_writes_tombstone_and_deck_falls_back(self) -> None:
        from net import deck
        registry._write_entry(self._entry())
        self.assertTrue(registry.unregister("advisor"))
        self.assertIsNone(registry.load_entry("advisor"))
        tomb = registry.load_tombstone("advisor")
        self.assertEqual(tomb["transcript_path"], "/t/advisor.jsonl")
        path = deck.resolve_transcript(path=None, session_id=None, cwd=None,
                                       seat="advisor")
        self.assertEqual(str(path), "/t/advisor.jsonl")

    def test_tombstone_fallback_announces_on_stderr(self) -> None:
        import contextlib, io
        from net import deck
        registry._write_entry(self._entry())
        registry.unregister("advisor")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            deck.resolve_transcript(path=None, session_id=None, cwd=None, seat="advisor")
        self.assertRegex(err.getvalue(),
                         r"audit fell back to last-known tombstone: session s1, unregistered \d{4}-")

    def test_register_removes_stale_tombstone(self) -> None:
        registry._write_entry(self._entry())
        registry.unregister("advisor")
        self.assertIsNotNone(registry.load_tombstone("advisor"))
        registry.register(name="advisor", role="advisor", repo_root="/r",
                          runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
        self.assertIsNone(registry.load_tombstone("advisor"))

    def test_no_entry_no_tombstone_still_errors(self) -> None:
        from net import deck
        with self.assertRaises(deck.DeckError):
            deck.resolve_transcript(path=None, session_id=None, cwd=None, seat="ghost")


class TombstoneNameHardeningTest(unittest.TestCase):
    """#352 review: a registry file's JSON name must never steer a tombstone
    write outside <net_dir>/tombstones, nor onto another seat's tombstone."""

    def setUp(self) -> None:
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.net = Path(self.base.name) / "a" / "b" / "net"
        env = mock.patch.dict(os.environ, {"MAW_NET_DIR": str(self.net)})
        env.start()
        self.addCleanup(env.stop)

    def _dead_pid(self) -> int:
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def _write_raw(self, stem: str, name: str) -> None:
        now = registry.utcnow_iso()
        e = registry.Entry(
            name=name, role="r", repo_root="/r", cwd="/r", pid=self._dead_pid(),
            tty=None, tmux_pane=None, session_id="s9", started_at=now,
            last_seen=now, transport={}, transcript_path="/t/x.jsonl")
        rdir = registry.registry_dir()
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{stem}.json").write_text(json.dumps(asdict(e)), encoding="utf-8")

    def _all_files(self) -> set[str]:
        return {str(p.relative_to(self.base.name))
                for p in Path(self.base.name).rglob("*") if p.is_file()}

    def test_climbing_name_writes_nothing_outside_and_is_audited(self) -> None:
        self._write_raw("evil", "../../../escaped")
        registry.who()
        files = self._all_files()
        self.assertFalse(any("escaped" in f for f in files), files)
        self.assertFalse((self.net / "tombstones").exists()
                         and any((self.net / "tombstones").iterdir()))
        self.assertIsNone(registry.load_entry("evil"))  # still pruned
        refusals = registry.registry_refusals_file().read_text(encoding="utf-8")
        self.assertIn('"kind": "tombstone"', refusals)
        self.assertIn("evil", refusals)

    def test_name_not_matching_file_stem_refused(self) -> None:
        # A valid-looking name in someone else's file must not overwrite that
        # seat's tombstone.
        self._write_raw("evil", "victim")
        registry.who()
        self.assertIsNone(registry.load_tombstone("victim"))
        self.assertIsNone(registry.load_tombstone("evil"))

    def test_load_and_remove_reject_bad_names(self) -> None:
        self.net.mkdir(parents=True, exist_ok=True)
        outside = self.net / "keep.json"  # = tombstones/../keep.json
        outside.write_text('{"transcript_path": "/t/forged.jsonl"}', encoding="utf-8")
        self.assertIsNone(registry.load_tombstone("../keep"))
        registry.remove_tombstone("../keep")
        self.assertTrue(outside.exists())

    def test_deck_seat_with_traversal_does_not_read_outside(self) -> None:
        from net import deck
        self.net.mkdir(parents=True, exist_ok=True)
        outside = self.net / "keep.json"  # = tombstones/../keep.json
        outside.write_text('{"transcript_path": "/t/forged.jsonl"}', encoding="utf-8")
        with self.assertRaises(deck.DeckError):
            deck.resolve_transcript(path=None, session_id=None, cwd=None, seat="../keep")
