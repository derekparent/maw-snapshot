"""Batch arm, end to end: a recorded two-seat simulation (run 1002b lane 5, gate 13).

Two advisor seats in two repos, the CoS tooling, the REAL relay handler under
node (relay/dev_server.mjs, the same handler.mjs the Worker serves, including
the page fingerprint), and a simulated phone that signs exactly the grant the
page displays. Then each seat arms from the delivered file through
session_auth.arm. The only stand-ins: the phone's key is a fixed TEST key, the
net send is a stub that records notices, and identities and git are injected.
No real Face ID, Touch ID, switchboard or gh is touched.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "maw"))
sys.path.insert(0, str(ROOT))

from guards import human_grant as hg  # noqa: E402
from guards import session_auth as sa  # noqa: E402
from phone_mint import relay_client as rc  # noqa: E402
from tests.guards.test_session_grant import _git  # noqa: E402
from tests.phone_mint.test_relay import (  # noqa: E402
    NODE, PHONE_PUB, SECRET, TEST_KEY_IDS, phone_sign,
)

SEATS = [
    {"slug": "alpha", "sid": "advisor-20261002T170000Z", "scope": "alpha run: lanes 1-2 only",
     "goal": ["Ship the ingest retry", "Docs for the retry"]},
    {"slug": "beta", "sid": "advisor-20261002T170100Z", "scope": "beta run: the search fix",
     "goal": ["Fix search ranking"]},
]


@unittest.skipIf(NODE is None, "node not installed")
class TwoSeatBatchArm(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {
            **TEST_KEY_IDS,
            "MAW_GRANT_KEYS_DIR": str(root / "keys"), "MAW_GRANTS_DIR": str(root / "grants"),
            "MAW_RELAY_PENDING_DIR": str(root / "pending")})
        env.start()
        self.addCleanup(env.stop)
        (root / "keys").mkdir()
        (root / "keys" / f"{rc.relay_pubkey_id()}.pub").write_text(json.dumps({
            "pubkey_id": rc.relay_pubkey_id(), "x": f"{PHONE_PUB[0]:x}", "y": f"{PHONE_PUB[1]:x}",
            "not_before": "2020-01-01T00:00:00Z", "not_after": "2099-01-01T00:00:00Z"}),
            encoding="utf-8")
        hg.ack_keyset()
        self.root = root
        self.seats = copy.deepcopy(SEATS)
        for seat in self.seats:
            seat["root"] = root / seat["slug"]
            seat["root"].mkdir()
        self.relay = self._start()

    def _start(self) -> str:
        self.proc = subprocess.Popen([NODE, str(ROOT / "relay" / "dev_server.mjs"), "--port", "0"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env={**os.environ, "RELAY_SECRET": SECRET})
        self.addCleanup(self._stop)
        m = re.match(r"listening (\d+)", self.proc.stdout.readline())
        self.assertIsNotNone(m, "dev relay did not start")
        return f"http://127.0.0.1:{m.group(1)}"

    def _stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def advisor_requests(self, now: float) -> "list[Path]":
        """Step 1, in each seat: goal-record, then grant-request --batch."""
        files = []
        for seat in self.seats:
            ok, msg = sa.record_goal(seat["root"], seat["goal"], session_id=seat["sid"])
            self.assertTrue(ok, msg)
            line, why = sa.batch_request(seat["scope"], seat["root"], runner=_git(seat["slug"]),
                                         now_s=now - 60, session_id=seat["sid"], seat="advisor")
            self.assertIsNotNone(line, why)
            f = self.root / f"line-{seat['slug']}.json"
            f.write_text(json.dumps({"line": line}), encoding="utf-8")
            files.append(f)
        return files

    def phone(self, url: str) -> None:
        """The operator opens the ONE link and signs exactly what the page shows."""
        with urllib.request.urlopen(url, timeout=10) as r:
            html = r.read().decode("utf-8")
            served_sha = r.headers.get("x-page-sha256")
        self.assertEqual(served_sha, rc.page_fingerprint())
        m = re.search(r"GRANT = (.*?), CAPTURE = (\".*?\");\n", html, re.S)
        shown, cap = json.loads(m.group(1)), json.loads(m.group(2))
        req = urllib.request.Request(self.relay + cap, method="POST",
                                     data=json.dumps(phone_sign(shown, self.relay)).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

    def arm(self, seat: dict, grant_path: Path):
        return sa.arm(seat["scope"], seat["root"], session_id=seat["sid"], pid=os.getpid(),
                      now_s=time.time(),
                      gate_fn=lambda _p: self.fail("Touch ID must not run on the grant path"),
                      seat="advisor", env={"MAW_BRIDGE_ROLE": "advisor"},
                      runner=_git(seat["slug"]),
                      grant=json.loads(grant_path.read_text(encoding="utf-8")))

    def token(self, seat: dict) -> dict:
        return json.loads(sa._runtime_path(seat["root"], sa.TOKEN_REL).read_text())

    def test_one_signature_arms_both_seats(self) -> None:
        now = time.time()
        files = self.advisor_requests(now)

        # CoS: mint ONE grant (the relay's page fingerprint must match this Mac's).
        grant = rc.assemble_batch(rc.read_batch_lines(files), pubkey_id=rc.relay_pubkey_id(),
                                  now_iso=time.strftime(hg._TS_FMT, time.gmtime(now)))
        rid, url = rc.mint_batch(grant, relay=self.relay, secret=SECRET)
        self.assertEqual([ln["repo"] for ln in grant["batch"]],
                         ["example-org/alpha", "example-org/beta"])

        self.phone(url)

        # CoS: the bounded poll finds the signature on its first pull.
        path, left = rc.pull_batch(rid, secret=SECRET, sleep=lambda _n: self.fail("polled"))
        self.assertGreater(left, 800)

        # CoS: deliver plain notices, one per pair switchboard.
        notices = {}

        def send(net, peer, sender, body):
            self.assertEqual((peer, sender), ("advisor", "cos"))
            notices[net] = body.read_text(encoding="utf-8")
            return subprocess.CompletedProcess([], 0, stdout="delivered\n", stderr="")

        # Each pair's switchboard holds its own live advisor (and its executor).
        boards = {seat["slug"]: [{"name": "executor", "role": "executor", "session_id": "ex"},
                                 {"name": "advisor", "role": "advisor", "session_id": seat["sid"]}]
                  for seat in self.seats}
        rows = rc.deliver_batch(path, sender="cos", send=send, who=lambda net: boards.get(net, []))
        self.assertEqual([ok for _r, ok, _n in rows], [True, True])
        self.assertEqual(sorted(notices), ["alpha", "beta"])

        # Each advisor arms from the file named in ITS notice.
        for i, seat in enumerate(self.seats):
            named = Path(re.search(r"^file: (.+)$", notices[seat["slug"]], re.M).group(1))
            ok, msg = self.arm(seat, named)
            self.assertTrue(ok, msg)
            tok = self.token(seat)
            self.assertEqual(tok["armed_via"], f"grant-batch:{grant['grant_id']}:{i}")
            self.assertEqual(tok["repo"], f"example-org/{seat['slug']}")
            self.assertTrue(tok["goal_sha256"])

        # Single use per line; the base grant id is never spent.
        for i in range(len(self.seats)):
            self.assertTrue(hg.is_consumed(hg.line_claim_id(grant["grant_id"], i)))
        self.assertFalse(hg.is_consumed(grant["grant_id"]))

        # Replay: beta's token is dropped, beta asks again, the same grant refuses.
        sa._runtime_path(self.seats[1]["root"], sa.TOKEN_REL).unlink()
        self.advisor_requests(now)
        ok, msg = self.arm(self.seats[1], path)
        self.assertFalse(ok)
        self.assertIn("already claimed", msg)

    def test_a_seat_cannot_arm_with_the_other_seats_line(self) -> None:
        now = time.time()
        files = self.advisor_requests(now)
        grant = rc.assemble_batch(rc.read_batch_lines(files[:1]), pubkey_id=rc.relay_pubkey_id(),
                                  now_iso=time.strftime(hg._TS_FMT, time.gmtime(now)))
        rid, url = rc.mint_batch(grant, relay=self.relay, secret=SECRET)
        self.phone(url)
        path, _left = rc.pull_batch(rid, secret=SECRET, sleep=lambda _n: None)
        ok, msg = self.arm(self.seats[1], path)  # beta presents a manifest holding only alpha
        self.assertFalse(ok)
        self.assertIn("no line of this grant matches this session", msg)
        self.assertFalse(hg.is_consumed(hg.line_claim_id(grant["grant_id"], 0)))
        ok, msg = self.arm(self.seats[0], path)
        self.assertTrue(ok, msg)


    def test_seats_registered_in_the_same_second_mint_and_arm(self) -> None:
        # #338, effect (a): the grid launcher brings both pairs' advisors up in
        # the same second. Their registry-minted ids must differ, so the
        # one-session-per-line rule (verifier AND the real relay handler) passes.
        from net import registry
        from tests.net.test_registry import fake_runner_factory
        for seat in self.seats:
            with mock.patch.dict(os.environ, {"MAW_NET_DIR": str(self.root / f"sw-{seat['slug']}")}), \
                    mock.patch.object(registry, "utcnow_iso", return_value="2026-10-02T20:06:39Z"):
                for k in ("MAW_SESSION_ID", "TMUX_PANE", "HERDR_ENV", "HERDR_PANE_ID"):
                    os.environ.pop(k, None)
                seat["sid"] = registry.register(name="advisor", role="advisor",
                                                repo_root=str(seat["root"]),
                                                runner=fake_runner_factory({})).session_id
        self.assertNotEqual(self.seats[0]["sid"], self.seats[1]["sid"])
        grant, path = self.mint_signed(time.time())
        for i, seat in enumerate(self.seats):
            ok, msg = self.arm(seat, path)
            self.assertTrue(ok, msg)
            self.assertEqual(self.token(seat)["armed_via"], f"grant-batch:{grant['grant_id']}:{i}")

    def mint_signed(self, now: float) -> "tuple[dict, Path]":
        grant = rc.assemble_batch(rc.read_batch_lines(self.advisor_requests(now)),
                                  pubkey_id=rc.relay_pubkey_id(),
                                  now_iso=time.strftime(hg._TS_FMT, time.gmtime(now)))
        rid, url = rc.mint_batch(grant, relay=self.relay, secret=SECRET)
        self.phone(url)
        path, _left = rc.pull_batch(rid, secret=SECRET, sleep=lambda _n: None)
        return grant, path

    def test_a_stale_relay_page_mints_nothing(self) -> None:
        # This Mac's page differs from the one the relay serves by one byte: the
        # REAL /api/page-sha over HTTP says so, and nothing is posted.
        from phone_mint import serve
        now = time.time()
        grant = rc.assemble_batch(rc.read_batch_lines(self.advisor_requests(now)),
                                  pubkey_id=rc.relay_pubkey_id(),
                                  now_iso=time.strftime(hg._TS_FMT, time.gmtime(now)))
        with mock.patch.object(serve, "_PAGE_TMPL", serve._PAGE_TMPL.replace("Arm ", "Arm  ", 1)), \
                mock.patch.object(rc, "post", wraps=rc.post) as post:
            with self.assertRaisesRegex(rc.RelayError, "needs a deploy"):
                rc.mint_batch(grant, relay=self.relay, secret=SECRET)
        post.assert_not_called()
        self.assertFalse(rc.pending_dir().exists() and any(rc.pending_dir().iterdir()),
                         "a link was created for a page that cannot describe it")

    def test_a_grant_edited_after_the_signature_arms_no_one(self) -> None:
        # The file reaches both advisors with a signed byte changed OUTSIDE the
        # lines, so each seat's line still matches its facts: only the signature
        # stands between the edit and two armed seats. N1, end to end.
        _grant, path = self.mint_signed(time.time())
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["target"] = "advisor-x"
        path.write_text(json.dumps(edited), encoding="utf-8")
        for i, seat in enumerate(self.seats):
            ok, msg = self.arm(seat, path)
            self.assertFalse(ok, msg)
            self.assertIn("grant refused", msg)
            self.assertFalse(hg.is_consumed(hg.line_claim_id(edited["grant_id"], i)))
            self.assertTrue(sa._request_path(f"example-org/{seat['slug']}", "advisor",
                                             seat["sid"]).exists(),
                            "an unverified grant spent the seat's request record")
            self.assertFalse(sa._runtime_path(seat["root"], sa.TOKEN_REL).exists())


if __name__ == "__main__":
    unittest.main()
