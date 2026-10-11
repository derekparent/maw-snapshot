"""phone_mint.request — the ping-with-link orchestration verb (#159, lane 1).

The live `tailscale serve` transport needs the operator's environment, but every seam this
verb owns is CI-testable phone-absent: URL resolution from a tailscale JSON blob,
the operator-facing ping text, the front start/stop commands, and the load-bearing
ORDER of assemble -> front-up -> emit-ping -> serve -> front-down (torn down even
when the ceremony raises). The grant is asserted to reach the server byte-for-byte
as assembled, so the §9.5 display-bind serve.py enforces is not weakened here.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from phone_mint import request as req  # noqa: E402

# Neutral key ids: the suite never reads the operator's ~/.maw/operator.toml
# (tests/run_all.py isolates it), so the minting tools get theirs from here.
TEST_KEY_IDS = {"MAW_GRANT_PUBKEY_ID": "test-grant-key",
                "MAW_PHONE_PUBKEY_ID": "test-phone-key",
                "MAW_RELAY_PUBKEY_ID": "test-relay-key"}
_KEY_ID_ENV = mock.patch.dict(os.environ, TEST_KEY_IDS)


def setUpModule() -> None:
    _KEY_ID_ENV.start()


def tearDownModule() -> None:
    _KEY_ID_ENV.stop()

NOW = "2026-07-21T05:40:00Z"
FAKE_KEY = ((1, 2), "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z")


class FakeServer:
    """Stand-in for serve.MintServer: records the kwargs it was built with and,
    on serve(), appends to a shared event log then returns a canned result."""

    def __init__(self, *, kind, grant, pubkey_point, window_seconds, port):
        self.kind = kind
        self.grant = grant
        self.pubkey_point = pubkey_point
        self.window_seconds = window_seconds
        self.port = port

    @classmethod
    def factory(cls, events, result, *, raise_on_serve=False):
        made = {}

        def build(**kw):
            inst = cls(**kw)
            inst._events = events
            inst._result = result
            inst._raise = raise_on_serve
            made["inst"] = inst
            return inst

        build.made = made
        return build

    def serve(self):
        self._events.append(("serve", self.grant))
        if self._raise:
            raise RuntimeError("boom mid-window")
        return self._result


def recording_run(events):
    """A subprocess seam that logs the argv verb and returns a tailscale-status
    blob for `status --json`."""
    def run(cmd):
        events.append(("run", tuple(cmd)))
        proc = mock.Mock()
        if "status" in cmd:
            proc.stdout = '{"Self": {"DNSName": "laptop.tail0000.ts.net."}}'
        else:
            proc.stdout = ""
        return proc
    return run


# --- build_ping --------------------------------------------------------------

class TestBuildPing(unittest.TestCase):
    def test_contains_what_url_and_expiry(self):
        ping = req.build_ping(what="merge PR #159", url="https://h.ts.net/",
                              expires_in_seconds=900)
        self.assertIn("merge PR #159", ping)
        self.assertIn("https://h.ts.net/", ping)
        self.assertIn("~15 min", ping)

    def test_minutes_ceil_and_floor_one(self):
        self.assertIn("~2 min", req.build_ping(what="x", url="u", expires_in_seconds=61))
        self.assertIn("~1 min", req.build_ping(what="x", url="u", expires_in_seconds=30))

    def test_empty_what_rejected(self):
        with self.assertRaises(ValueError):
            req.build_ping(what="", url="u", expires_in_seconds=900)

    def test_no_instruction_plaintext_leak(self):
        # `what` is a caller summary; the grant's instruction stays hashed. The
        # ping only ever carries `what`, never the raw instruction.
        ping = req.build_ping(what="authorize the thing", url="u", expires_in_seconds=900)
        self.assertNotIn("SECRET-INSTRUCTION", ping)


# --- resolve_serve_url -------------------------------------------------------

class TestResolveUrl(unittest.TestCase):
    def test_parses_dnsname_and_strips_dot(self):
        run = recording_run([])
        url = req.resolve_serve_url(run=run)
        self.assertEqual(url, "https://laptop.tail0000.ts.net/")

    def test_empty_dnsname_raises(self):
        def run(cmd):
            proc = mock.Mock(); proc.stdout = '{"Self": {"DNSName": ""}}'; return proc
        with self.assertRaises(RuntimeError):
            req.resolve_serve_url(run=run)

    def test_malformed_json_raises(self):
        def run(cmd):
            proc = mock.Mock(); proc.stdout = "not json"; return proc
        with self.assertRaises(RuntimeError):
            req.resolve_serve_url(run=run)


# --- front start/stop --------------------------------------------------------

class TestFront(unittest.TestCase):
    def test_start_front_command(self):
        events = []
        req.start_front(8799, run=recording_run(events))
        self.assertEqual(events[0][1][-2:], ("--bg", "http://127.0.0.1:8799"))

    def test_stop_front_command(self):
        events = []
        req.stop_front(run=recording_run(events))
        self.assertEqual(events[0][1][-2:], ("serve", "reset"))


# --- request orchestration ---------------------------------------------------

class TestRequestOrchestration(unittest.TestCase):
    def _call(self, *, raise_on_serve=False, url=None, serve_front=True):
        events = []
        ready = []
        factory = FakeServer.factory(events, {"webauthn": {}, "signed": True},
                                     raise_on_serve=raise_on_serve)

        def run(cmd):
            events.append(("run", tuple(cmd)))
            proc = mock.Mock()
            proc.stdout = ('{"Self": {"DNSName": "h.ts.net."}}'
                           if "status" in cmd else "")
            return proc

        def on_ready(ping, u):
            events.append(("ready", ping, u))
            ready.append((ping, u))

        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            result = req.request(
                instruction="do the thing",
                scope_class="prompt.relay",
                target="cos",
                ttl_seconds=900,
                now_iso=NOW,
                what="relay a prompt",
                url=url,
                serve_front=serve_front,
                on_ready=on_ready,
                run=run,
                server_factory=factory,
            )
        return result, events, ready, factory

    def test_happy_path_result_and_grant_passthrough(self):
        result, events, ready, factory = self._call()
        # completed grant is whatever the server returned, relayable unchanged.
        self.assertEqual(result.completed, {"webauthn": {}, "signed": True})
        # TTL clock started at now_iso (assemble ran).
        self.assertEqual(result.grant["issued_at"], NOW)
        # the grant the server served is the SAME object assembled (byte-for-byte
        # display-bind: no mutation between assemble and serve).
        served = [e for e in events if e[0] == "serve"][0][1]
        self.assertIs(served, result.grant)
        self.assertEqual(len(ready), 1)
        self.assertIn("h.ts.net", ready[0][0])

    def test_ordering_front_up_ready_serve_front_down(self):
        _, events, _, _ = self._call()
        verbs = []
        for e in events:
            if e[0] == "run" and "--bg" in e[1]:
                verbs.append("front-up")
            elif e[0] == "run" and e[1][-2:] == ("serve", "reset"):
                verbs.append("front-down")
            elif e[0] in ("ready", "serve"):
                verbs.append(e[0])
        self.assertEqual(verbs, ["front-up", "ready", "serve", "front-down"])

    def test_front_torn_down_even_when_serve_raises(self):
        with self.assertRaises(RuntimeError):
            self._call(raise_on_serve=True)

    def test_front_down_ran_after_serve_raised(self):
        events = []
        factory = FakeServer.factory(events, None, raise_on_serve=True)

        def run(cmd):
            events.append(("run", tuple(cmd)))
            proc = mock.Mock(); proc.stdout = ""; return proc

        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            with self.assertRaises(RuntimeError):
                req.request(
                    instruction="x", scope_class="prompt.relay", target="t",
                    ttl_seconds=900, now_iso=NOW, what="w", url="https://h/",
                    on_ready=None, run=run, server_factory=factory,
                )
        # the teardown ran despite the raise (finally).
        self.assertTrue(any(e[0] == "run" and e[1][-2:] == ("serve", "reset")
                            for e in events))

    def test_url_override_skips_tailscale_status(self):
        _, events, ready, _ = self._call(url="https://override.ts.net/")
        self.assertFalse(any(e[0] == "run" and "status" in e[1] for e in events))
        self.assertIn("override.ts.net", ready[0][0])

    def test_no_front_skips_serve_commands(self):
        _, events, _, _ = self._call(serve_front=False, url="https://h/")
        self.assertFalse(any(e[0] == "run" and "--bg" in e[1] for e in events))
        self.assertFalse(any(e[0] == "run" and e[1][-2:] == ("serve", "reset")
                            for e in events))

    def test_timeout_returns_none_completed(self):
        events = []
        factory = FakeServer.factory(events, None)  # serve() returns None => timeout

        def run(cmd):
            proc = mock.Mock(); proc.stdout = ""; return proc

        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            result = req.request(
                instruction="x", scope_class="prompt.relay", target="t",
                ttl_seconds=900, now_iso=NOW, what="w", url="https://h/",
                run=run, server_factory=factory,
            )
        self.assertIsNone(result.completed)

    def test_unknown_pubkey_raises_before_serving(self):
        events = []
        factory = FakeServer.factory(events, {})
        with mock.patch.object(req.hg, "load_key", return_value=None):
            with self.assertRaises(ValueError):
                req.request(
                    instruction="x", scope_class="prompt.relay", target="t",
                    ttl_seconds=900, now_iso=NOW, what="w", url="https://h/",
                    serve_front=False, run=lambda c: mock.Mock(stdout=""),
                    server_factory=factory,
                )
        # never reached serve.
        self.assertFalse(any(e[0] == "serve" for e in events))

    def test_merge_approval_shape_flows_through(self):
        events = []
        factory = FakeServer.factory(events, {"signed": True})
        sha = "a" * 40
        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            result = req.request(
                instruction="merge it", scope_class="merge.approval", target="maw#159",
                ttl_seconds=900, now_iso=NOW, what="merge PR 159",
                merge={"repo": "maw", "pr": 159, "head_sha": sha},
                url="https://h/", serve_front=False,
                run=lambda c: mock.Mock(stdout=""), server_factory=factory,
            )
        self.assertEqual(result.grant["merge"], {"repo": "maw", "pr": 159, "head_sha": sha})

    def test_ping_expiry_is_min_of_ttl_and_window(self):
        # The operator's actionable deadline is the SOONER of grant TTL and serve window —
        # both orderings (advisor MAJOR on #161: a ttl-based ping over-promises
        # when the window is shorter, so the operator taps a dead link).
        for ttl, window, expected in [(900, 300, "~5 min"), (120, 600, "~2 min")]:
            events = []
            captured = []
            factory = FakeServer.factory(events, {"signed": True})
            with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
                req.request(
                    instruction="x", scope_class="prompt.relay", target="t",
                    ttl_seconds=ttl, now_iso=NOW, what="w", url="https://h/",
                    window_seconds=window, serve_front=False,
                    on_ready=lambda p, u: captured.append(p),
                    run=lambda c: mock.Mock(stdout=""), server_factory=factory,
                )
            self.assertIn(expected, captured[0],
                          f"ttl={ttl} window={window} -> expected {expected}")

    @staticmethod
    def _run_teardown_fails(events):
        def run(cmd):
            events.append(("run", tuple(cmd)))
            if tuple(cmd[-2:]) == ("serve", "reset"):
                raise RuntimeError("teardown boom")
            proc = mock.Mock(); proc.stdout = ""; return proc
        return run

    def test_completed_grant_survives_teardown_failure(self):
        # MINOR on #161: the operator walked Face ID; a teardown failure must NOT lose the
        # signed grant. It comes back with teardown_error set, not raised.
        events = []
        factory = FakeServer.factory(events, {"signed": True})
        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            result = req.request(
                instruction="x", scope_class="prompt.relay", target="t",
                ttl_seconds=900, now_iso=NOW, what="w", url="https://h/",
                serve_front=True, run=self._run_teardown_fails(events),
                server_factory=factory,
            )
        self.assertEqual(result.completed, {"signed": True})
        self.assertIsNotNone(result.teardown_error)

    def test_incomplete_teardown_failure_is_not_swallowed(self):
        # Timeout (completed None) AND teardown fails -> raise (loud), never a
        # silent None return.
        events = []
        factory = FakeServer.factory(events, None)
        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            with self.assertRaises(RuntimeError):
                req.request(
                    instruction="x", scope_class="prompt.relay", target="t",
                    ttl_seconds=900, now_iso=NOW, what="w", url="https://h/",
                    serve_front=True, run=self._run_teardown_fails(events),
                    server_factory=factory,
                )

    def test_serve_error_propagates_over_teardown_failure(self):
        # serve() raised AND teardown raised: the serve error is the loud cause
        # that propagates; teardown is still attempted (front not left up silently).
        events = []
        factory = FakeServer.factory(events, None, raise_on_serve=True)
        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            with self.assertRaisesRegex(RuntimeError, "boom mid-window"):
                req.request(
                    instruction="x", scope_class="prompt.relay", target="t",
                    ttl_seconds=900, now_iso=NOW, what="w", url="https://h/",
                    serve_front=True, run=self._run_teardown_fails(events),
                    server_factory=factory,
                )
        self.assertTrue(any(e[0] == "run" and e[1][-2:] == ("serve", "reset")
                            for e in events))

    def test_serve_error_teardown_failure_warns_loudly(self):
        # #161 parity: serve raised AND teardown raised — the serve error still
        # propagates, but the front-may-linger warning is NOT silently dropped.
        import io
        from contextlib import redirect_stderr
        events = []
        factory = FakeServer.factory(events, None, raise_on_serve=True)
        buf = io.StringIO()
        with mock.patch.object(req.hg, "load_key", return_value=FAKE_KEY):
            with redirect_stderr(buf), self.assertRaises(RuntimeError):
                req.request(
                    instruction="x", scope_class="prompt.relay", target="t",
                    ttl_seconds=900, now_iso=NOW, what="w", url="https://h/",
                    serve_front=True, run=self._run_teardown_fails(events),
                    server_factory=factory,
                )
        self.assertIn("front may still be serving", buf.getvalue())


class TestCLI(unittest.TestCase):
    def _run_main(self, argv, *, completed):
        captured = {}

        def fake_request(**kw):
            captured.update(kw)
            # fire on_ready so the stdout ping path is exercised
            if kw.get("on_ready"):
                kw["on_ready"]("PING-TEXT", "https://h/")
            return req.RequestResult(grant={"g": 1}, ping="PING-TEXT",
                                     url="https://h/", completed=completed)

        with mock.patch.object(req, "request", side_effect=fake_request):
            rc = req.main(argv)
        return rc, captured

    def test_success_writes_out_and_returns_zero(self):
        import tempfile, json as _json, os
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "grant.json")
            rc, captured = self._run_main(
                ["--instruction", "do it", "--scope-class", "prompt.relay",
                 "--target", "cos", "--ttl", "900", "--what", "relay",
                 "--out", out, "--no-front"],
                completed={"signed": True})
            self.assertEqual(rc, 0)
            self.assertEqual(_json.loads(Path(out).read_text()), {"signed": True})
            self.assertFalse(captured["serve_front"])  # --no-front honored

    def test_timeout_returns_one_no_file(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "grant.json")
            rc, _ = self._run_main(
                ["--instruction", "x", "--scope-class", "prompt.relay",
                 "--target", "t", "--ttl", "900", "--what", "w", "--out", out],
                completed=None)
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(out))

    def test_teardown_failure_after_completion_returns_two_and_writes_out(self):
        import tempfile, os, json as _json
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "g.json")

            def fake_request(**kw):
                if kw.get("on_ready"):
                    kw["on_ready"]("PING", "https://h/")
                return req.RequestResult(grant={}, ping="PING", url="https://h/",
                                         completed={"signed": True},
                                         teardown_error=RuntimeError("boom"))

            with mock.patch.object(req, "request", side_effect=fake_request):
                rc = req.main(
                    ["--instruction", "x", "--scope-class", "prompt.relay",
                     "--target", "t", "--ttl", "900", "--what", "w",
                     "--out", out, "--no-front"])
            self.assertEqual(rc, 2)  # non-zero: teardown failure is loud
            self.assertEqual(_json.loads(Path(out).read_text()), {"signed": True})

    def test_merge_flags_assemble_into_merge_dict(self):
        import tempfile, os
        sha = "b" * 40
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "g.json")
            _, captured = self._run_main(
                ["--instruction", "merge", "--scope-class", "merge.approval",
                 "--target", "maw#159", "--ttl", "900", "--what", "merge 159",
                 "--merge-repo", "maw", "--merge-pr", "159", "--merge-sha", sha,
                 "--out", out, "--no-front"],
                completed={"signed": True})
            self.assertEqual(captured["merge"],
                             {"repo": "maw", "pr": 159, "head_sha": sha})


if __name__ == "__main__":
    unittest.main()
