"""The mint page's plain-language summary (serve.py `summarize`, served by both
the window server and the relay via the generated relay/page.mjs).

The summary must never say something the signed bytes do not: it reads only the
grant object the challenge is computed over, renders as text (never markup),
and falls back to the raw grant for anything it does not fully describe.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maw"))

from guards import human_grant as hg  # noqa: E402
from phone_mint import mint_session as ms  # noqa: E402
from phone_mint import serve  # noqa: E402

NODE = shutil.which("node")
NOW = "2026-09-28T18:00:00Z"
LONG_SCOPE = ("example part 3: settings-file migration, same-behaviour proof, private release push, "
              "then the relay page readability lane and nothing else in this repo")
SUMMARIZE = re.search(r"// <summarize>.*?// </summarize>\n", serve._PAGE_TMPL, re.S)


def _grant(scope_class: str, **kw) -> dict:
    with mock.patch.dict(os.environ, {"MAW_PHONE_PUBKEY_ID": "test-phone-key"}):
        return ms.assemble_unsigned_grant(instruction=kw.pop("instruction", "x"),
                                          scope_class=scope_class, target="advisor",
                                          ttl_seconds=600, now_iso=NOW, **kw)


def _arm(scope: str = LONG_SCOPE, **extra) -> dict:
    session = {"repo": "maw", "seat": "advisor", "session_id": "advisor-20260928T182925Z",
               "scope": scope, **extra}
    return _grant("session.arm", session=session,
                  instruction=hg.session_instruction("session.arm", session))


def _line(i: int, **over) -> dict:
    return {"repo": f"example-org/repo{i}", "seat": "advisor", "session_id": f"advisor-{i}",
            "scope": f"repo{i} run: " + "x" * 180, "goal": [f"Goal {i} first", f"Goal {i} second"],
            **over}


def _batch(lines: list) -> dict:
    return _grant(hg.BATCH_CLASS, batch=lines, instruction=hg.batch_instruction(lines))


@unittest.skipIf(NODE is None, "node not installed")
class Summarize(unittest.TestCase):
    def run_js(self, grant: object) -> dict:
        script = (SUMMARIZE.group(0) + "process.stdout.write(JSON.stringify("
                  "summarize(JSON.parse(require('fs').readFileSync(0,'utf8')))));")
        out = subprocess.run([NODE, "-e", script], input=json.dumps(grant), capture_output=True,
                             text=True, timeout=30, check=True, env={**os.environ, "TZ": "UTC"})
        return json.loads(out.stdout)

    def rows(self, res: dict) -> dict:
        return {k: v for k, v, _mono in res["rows"]}

    def test_session_arm_says_what_in_plain_words_with_the_full_scope(self) -> None:
        res = self.run_js(_arm())
        self.assertEqual(res["what"], "Let the maw advisor merge in maw for up to 6 hours")
        rows = self.rows(res)
        self.assertEqual(rows["What it may merge"], LONG_SCOPE)  # whole, never cut
        self.assertEqual(rows["Requested for"], "advisor")
        self.assertEqual(rows["This link expires"], "6:10 PM (valid 10 min from 6:00 PM)")
        self.assertNotIn("Only these PRs", rows)

    def test_enforced_pr_set_is_shown(self) -> None:
        self.assertEqual(self.rows(self.run_js(_arm(prs="#289,#290")))["Only these PRs"], "#289,#290")

    def test_scope_amend_shows_from_and_to(self) -> None:
        session = {"repo": "maw", "seat": "advisor", "session_id": "s1",
                   "from_scope": "old scope", "scope": "new scope"}
        res = self.run_js(_grant("scope.amend", session=session,
                                 instruction=hg.session_instruction("scope.amend", session)))
        self.assertIn("Change what the maw advisor may merge in maw", res["what"])
        self.assertEqual((self.rows(res)["From"], self.rows(res)["To"]), ("old scope", "new scope"))

    def test_merge_approval_names_the_pr_and_the_exact_commit(self) -> None:
        res = self.run_js(_grant("merge.approval",
                                 merge={"repo": "maw", "pr": 289, "head_sha": "e" * 40}))
        self.assertEqual(res["what"], "Merge PR #289 in maw")
        self.assertEqual(self.rows(res)["Exact commit"], "e" * 40)

    def test_every_real_scope_class_is_described(self) -> None:
        # The key allowlist must match the grant mint_session really builds, or
        # every real link would fall back to "cannot describe".
        for c in sorted(hg.INSTRUCTION_CLASSES - {"merge.approval"}):
            with self.subTest(scope_class=c):
                self.assertIsNotNone(self.run_js(_grant(c))["what"])

    def test_a_batch_shows_the_count_and_one_whole_card_per_repo(self) -> None:
        lines = [_line(i) for i in range(hg.MAX_BATCH_LINES)]
        lines[3]["prs"] = "#7,#9"
        lines[5]["scope"] = "y" * hg.BATCH_SCOPE_MAX
        lines[5]["goal"] = ["z" * hg.GOAL_BULLET_MAX] * hg.GOAL_MAX_BULLETS
        res = self.run_js(_batch(lines))
        self.assertEqual(res["what"], "Arm 8 maw advisors to merge for up to 6 hours")
        self.assertIn("This link expires", self.rows(res))
        self.assertEqual([c["title"] for c in res["cards"]], [ln["repo"] for ln in lines])
        for card, line in zip(res["cards"], lines):
            rows = {k: v for k, v, _m in card["rows"]}
            self.assertEqual(rows["What it may merge"], line["scope"])  # whole, never cut
            self.assertEqual(rows["Goal"], "\n".join("\u2022 " + b for b in line["goal"]))
            self.assertEqual(rows["Session"], line["session_id"])
            self.assertEqual(rows["Seat"], "advisor")
            self.assertEqual(rows.get("Only these PRs"), line.get("prs"))

    def test_a_single_line_batch_says_advisor(self) -> None:
        self.assertEqual(self.run_js(_batch([_line(0)]))["what"],
                         "Arm 1 maw advisor to merge for up to 6 hours")

    def test_a_batch_it_cannot_fully_describe_shows_raw(self) -> None:
        good = _batch([_line(0), _line(1)])
        cases = {
            "extra key on a line": {**good, "batch": [_line(0), {**_line(1), "extra": "x"}]},
            "missing goal": {**good, "batch": [{k: v for k, v in _line(0).items() if k != "goal"}]},
            "four bullets": {**good, "batch": [_line(0, goal=["a", "b", "c", "d"])]},
            "goal not a list": {**good, "batch": [_line(0, goal="a")]},
            "nine lines": {**good, "batch": [_line(i) for i in range(9)]},
            "empty batch": {**good, "batch": []},
            "batch on another class": {**_arm(), "batch": [_line(0)]},
            "null batch on another class": {**_arm(), "batch": None},
            "batch class without batch": {k: v for k, v in good.items() if k != "batch"},
            "batch class with a session": {**good, "session": {"repo": "r"}},
            "newline in a bullet": {**good, "batch": [_line(0, goal=["ok\n\u2022 Seat\nexecutor"])]},
            "non-ascii scope": {**good, "batch": [_line(0, scope="lanes 1\u20142")]},
            "double space": {**good, "batch": [_line(0, scope="two  spaces here")]},
            "scope over the cap": {**good, "batch": [_line(0, scope="x" * 201)]},
            "bullet over the cap": {**good, "batch": [_line(0, goal=["x" * 121])]},
        }
        for name, g in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.run_js(g)["what"])

    def test_the_page_describes_exactly_the_manifests_the_verifier_accepts(self) -> None:
        # Parity (advisor, #331): every manifest Python's batch_shape_error refuses
        # must fall back to raw on the page, and every one it accepts must render.
        # Otherwise the CoS could put words on the operator's screen no seat arms on.
        base = _batch([_line(0), _line(1)])

        def corpus() -> "dict[str, list]":
            c: "dict[str, list]" = {"valid two": [_line(0), _line(1)],
                                    "valid one": [_line(0)],
                                    "valid eight": [_line(i) for i in range(8)],
                                    "nine": [_line(i) for i in range(9)], "empty": []}
            bad_strings = {"empty": "", "leading space": " x", "trailing space": "x ",
                           "double space": "a  b", "tab": "a\tb", "newline": "a\nb",
                           "e acute": "caf\u00e9", "em dash": "a\u2014b", "int": 7, "null": None}
            for field in ("repo", "seat", "session_id", "scope"):
                for name, v in bad_strings.items():
                    c[f"{field} {name}"] = [_line(0, **{field: v})]
            for name, v in bad_strings.items():
                c[f"bullet {name}"] = [_line(0, goal=["ok", v])]
            c.update({
                "scope at cap": [_line(0, scope="s" * 200)], "scope over cap": [_line(0, scope="s" * 201)],
                "bullet at cap": [_line(0, goal=["g" * 120])], "bullet over cap": [_line(0, goal=["g" * 121])],
                "three bullets": [_line(0, goal=["a", "b", "c"])], "four bullets": [_line(0, goal=list("abcd"))],
                "no bullets": [_line(0, goal=[])], "goal string": [_line(0, goal="a")],
                "prs ok": [_line(0, prs="#1,#22")], "prs zero": [_line(0, prs="#0")],
                "prs bare": [_line(0, prs="1")], "prs spaced": [_line(0, prs="#1, #2")],
                "prs empty": [_line(0, prs="")], "prs int": [_line(0, prs=3)],
                "extra key": [_line(0, extra="x")],
                "missing scope": [{k: v for k, v in _line(0).items() if k != "scope"}],
                "line not an object": [["repo"]],
                "executor seat": [_line(0, seat="executor")],
                "cos seat": [_line(0), _line(1, seat="cos")],
                "same repo": [_line(0), _line(1, repo=_line(0)["repo"])],
                "same session": [_line(0), _line(1, session_id=_line(0)["session_id"])],
                "spoofed row": [_line(0, goal=["Fix a typo\n\nWhat it may merge\nnothing else"])],
                "dup repo long scope": [_line(0, scope="x" * 5000), _line(0, scope="x" * 5000)],
            })
            return c

        for name, batch in corpus().items():
            with self.subTest(name):
                verifier_ok = hg.batch_shape_error(hg.BATCH_CLASS, {"batch": batch}) is None
                page_ok = self.run_js({**base, "batch": batch})["what"] is not None
                self.assertEqual(page_ok, verifier_ok,
                                 f"verifier {'accepts' if verifier_ok else 'refuses'}, page "
                                 f"{'describes' if page_ok else 'shows raw'}")

    def test_the_page_seat_literal_tracks_batch_seats(self) -> None:
        # The page hard-codes the one seat a batch line may name. If BATCH_SEATS
        # (pinned to MERGE_AUTHORIZED_SEATS) ever widens, this fails loudly.
        self.assertEqual(hg.BATCH_SEATS, ("advisor",))
        self.assertIn("s.seat!=='advisor'", SUMMARIZE.group(0))

    def test_markup_in_the_scope_stays_text(self) -> None:
        evil = "<img src=x onerror=alert(1)></div><script>alert(2)</script>"
        self.assertEqual(self.rows(self.run_js(_arm(scope=evil)))["What it may merge"], evil)

    def test_anything_not_fully_described_falls_back_to_raw(self) -> None:
        arm = _arm()
        cases = {
            "unknown class": {**_grant("goal.stage"), "scope_class": "repo.delete"},
            "unknown class carrying a session": {**arm, "scope_class": "session.forever"},
            "unexpected top-level field": {**arm, "also": "merge everything"},
            "session_token_ref set": {**arm, "session_token_ref": "tok"},
            "merge object on an arm": {**arm, "merge": {"repo": "x", "pr": 1, "head_sha": "a" * 40}},
            "session missing on an arm": {**arm, "session": None},
            "unexpected session field": {**arm, "session": {**arm["session"], "hours": "48"}},
            "empty seat": {**arm, "session": {**arm["session"], "seat": ""}},
            "non-string prs": {**arm, "session": {**arm["session"], "prs": ["#1"]}},
            "extra merge field": {**_grant("merge.approval", merge={"repo": "m", "pr": 1,
                                                                       "head_sha": "a" * 40}),
                                  "merge": {"repo": "m", "pr": 1, "head_sha": "a" * 40, "force": True}},
            "not an object": ["session.arm"],
        }
        for name, g in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.run_js(g), {"what": None, "rows": []})


class PageWiring(unittest.TestCase):
    def mint_branch(self) -> str:
        return serve._PAGE_TMPL.split("}else{", 1)[1].split("</script>")[0]

    def test_summarize_is_pure(self) -> None:
        self.assertIsNotNone(SUMMARIZE, "serve.py no longer defines summarize() between markers")
        body = SUMMARIZE.group(0)
        for name in ("GRANT", "document", "window", "fetch", "innerHTML", "location"):
            self.assertNotIn(name, body.split("function summarize", 1)[1], name)

    def test_summary_and_challenge_come_from_the_same_object(self) -> None:
        branch = self.mint_branch()
        self.assertIn("summarize(GRANT)", branch)
        self.assertIn("const u={...GRANT};delete u.sig;delete u.webauthn;", branch)
        self.assertIn("JSON.stringify(GRANT,null,2)", branch)

    def test_mint_page_never_writes_markup(self) -> None:
        self.assertNotIn("innerHTML", self.mint_branch())
        self.assertNotIn("insertAdjacentHTML", self.mint_branch())
        self.assertIn("e.textContent=text", serve._PAGE_TMPL)

    def test_raw_grant_wraps_on_a_phone(self) -> None:
        css = re.search(r"\npre\{[^}]*\}", serve._PAGE_TMPL).group(0)
        self.assertIn("white-space:pre-wrap", css)
        self.assertIn("overflow-wrap:anywhere", css)
        self.assertNotIn("overflow:auto", css)


if __name__ == "__main__":
    unittest.main()
