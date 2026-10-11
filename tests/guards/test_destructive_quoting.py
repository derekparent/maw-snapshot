"""The destructive guard's git-as-DATA hole — #194.

`_segments` split the command string on `;`, `|`, `&` and NEWLINE *before* shlex
ran, so a multi-line quoted body (a commit message, a PR body) was torn into
lines and each line parsed as a command. A PR body that DOCUMENTS
`git reset --hard` was therefore blocked as if it ran it — bitten live while
committing the #137 fix, and the workaround (always use -F/--body-file) taught
seats to route around a security guard.

The other half of these tests matters more: every real compound command must
still block. A quoting fix that lets `&& git reset --hard` through would trade a
false positive for a false negative.
"""
from __future__ import annotations

import unittest

from guards import destructive


class TestQuotedDataIsNotACommand(unittest.TestCase):
    def test_a_multiline_commit_message_documenting_a_reset_is_data(self) -> None:
        cmd = ('git commit -m "fix: restore the tree\n\n'
               'The old recipe told operators to run\n'
               'git reset --hard origin/main\n'
               'which discards uncommitted work."')
        self.assertEqual(destructive.find_dirty_destructive(cmd), [])

    def test_a_multiline_pr_body_documenting_clean_is_data(self) -> None:
        cmd = ('gh pr create --body "What changed\n\n'
               'Do not run git clean -fd here; it removes the fixtures.\n"')
        self.assertEqual(destructive.find_dirty_destructive(cmd), [])

    def test_a_multiline_body_documenting_a_branch_delete_is_data(self) -> None:
        cmd = ('gh pr create --body "Cleanup note:\n'
               'git branch -D main was the old step and is now forbidden.\n"')
        self.assertEqual(destructive.find_branch_deletions(cmd), [])

    def test_single_quotes_protect_a_body_too(self) -> None:
        cmd = ("git commit -m 'line one\ngit reset --hard\nline three'")
        self.assertEqual(destructive.find_dirty_destructive(cmd), [])

    def test_an_escaped_quote_inside_the_body_does_not_end_it(self) -> None:
        cmd = ('git commit -m "he said \\"run\\" and then\ngit reset --hard\n"')
        self.assertEqual(destructive.find_dirty_destructive(cmd), [])

    def test_a_semicolon_inside_a_quoted_body_is_not_a_separator(self) -> None:
        cmd = 'git commit -m "step one; git reset --hard; step two"'
        self.assertEqual(destructive.find_dirty_destructive(cmd), [])

    def test_a_pipe_inside_a_quoted_body_is_not_a_separator(self) -> None:
        cmd = 'git commit -m "run a | git clean -fd | b"'
        self.assertEqual(destructive.find_dirty_destructive(cmd), [])


class TestRealCompoundsStillBlock(unittest.TestCase):
    def test_and_chained_reset_still_found(self) -> None:
        cmd = 'git commit -m "safe message" && git reset --hard'
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git reset --hard"])

    def test_newline_separated_reset_still_found(self) -> None:
        cmd = 'git commit -m "safe message"\ngit reset --hard'
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git reset --hard"])

    def test_semicolon_separated_clean_still_found(self) -> None:
        cmd = 'echo hi; git clean -fd'
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git clean -f"])

    def test_a_reset_AFTER_a_closed_quoted_body_still_found(self) -> None:
        """The adversarial shape: legitimate quoted data, then a real command."""
        cmd = ('git commit -m "documents git reset --hard for the reader"\n'
               'git reset --hard origin/main')
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git reset --hard"])

    def test_branch_delete_after_a_quoted_body_still_found(self) -> None:
        cmd = ('gh pr create --body "mentions git branch -D nothing"\n'
               'git branch -D feature/x')
        self.assertEqual(destructive.find_branch_deletions(cmd), ["feature/x"])

    def test_pipe_chained_clean_still_found(self) -> None:
        self.assertEqual(destructive.find_dirty_destructive('true | git clean -f'),
                         ["git clean -f"])

    def test_or_chained_reset_still_found(self) -> None:
        self.assertEqual(
            destructive.find_dirty_destructive('false || git reset --hard'),
            ["git reset --hard"])

    def test_background_ampersand_still_separates(self) -> None:
        self.assertEqual(
            destructive.find_dirty_destructive('sleep 1 & git reset --hard'),
            ["git reset --hard"])


class TestNoNewFalseNegatives(unittest.TestCase):
    """The #216 review rule: a quoting fix may remove false positives; it may
    never add a false NEGATIVE the old regex caught. These three shapes are
    ordinary seat text — an apostrophe in a comment, an apostrophe in a heredoc
    body, an unterminated quote — and under a naive quote walk each one swallows
    the rest of the command, so the destructive tail is dropped and the guard
    silently stops guarding."""

    def test_an_unterminated_quote_does_not_swallow_a_real_destructive_tail(self) -> None:
        cmd = "echo don't do it\ngit reset --hard"
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git reset --hard"])

    def test_an_apostrophe_in_a_comment_does_not_open_a_span(self) -> None:
        cmd = "# don't do this\ngit reset --hard"
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git reset --hard"])

    def test_a_heredoc_body_does_not_swallow_a_real_destructive_tail(self) -> None:
        cmd = ("git commit -F - <<'EOF'\n"
               "don't do this\n"
               "EOF\n"
               "git reset --hard")
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git reset --hard"])

    def test_a_comment_hash_INSIDE_a_quoted_body_is_still_data(self) -> None:
        """The comment rule must not undo the #194 fix: a # inside a quoted body
        is text, not a comment."""
        cmd = 'git commit -m "see # 194\ngit reset --hard\ndone"'
        self.assertEqual(destructive.find_dirty_destructive(cmd), [])

    def test_a_hash_mid_token_is_not_a_comment(self) -> None:
        """sh starts a comment only at a word boundary; abc#def is one word."""
        cmd = "echo abc#def\ngit reset --hard"
        self.assertEqual(destructive.find_dirty_destructive(cmd), ["git reset --hard"])

    def test_a_branch_delete_after_a_heredoc_is_still_found(self) -> None:
        cmd = ("gh pr create --body-file - <<'EOF'\n"
               "it's a note\n"
               "EOF\n"
               "git branch -D feature/x")
        self.assertEqual(destructive.find_branch_deletions(cmd), ["feature/x"])


class TestSegmentsItself(unittest.TestCase):
    def test_unbalanced_quotes_still_fail_open(self) -> None:
        """Unchanged contract: a segment shlex cannot parse is dropped, because a
        misfire on a quoted string is worse than missing a malformed op."""
        self.assertEqual(destructive._segments('git commit -m "unterminated'), [])

    def test_an_override_assignment_is_still_seen(self) -> None:
        self.assertTrue(
            destructive._command_sets("MAW_ALLOW_DESTRUCTIVE=1 git reset --hard",
                                      "MAW_ALLOW_DESTRUCTIVE"))

    def test_a_quoted_override_is_still_not_an_override(self) -> None:
        self.assertFalse(
            destructive._command_sets('git commit -m "MAW_ALLOW_DESTRUCTIVE=1"',
                                      "MAW_ALLOW_DESTRUCTIVE"))

    def test_a_quoted_newline_keeps_one_segment(self) -> None:
        segs = destructive._segments('git commit -m "a\nb\nc"')
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][:3], ["git", "commit", "-m"])


class TestSubstitutionScannerMisses(unittest.TestCase):
    """RELEASE-NOTES known debt (run maw 1001, lane 3). Each shape ran under
    bash (bash -n accepts it) and was invisible to the guard on main: a
    double-quoted "$( )" is one quoted word to the outer split, so only the
    substitution scanner sees inside it."""

    def assert_found(self, cmd: str, op: str) -> None:
        self.assertIn(op, destructive.find_dirty_destructive(cmd), cmd)

    def test_a_paren_inside_a_parameter_default_does_not_close_the_substitution(self) -> None:
        self.assert_found('echo "$(printf %s ${v:-)} && git reset --hard)"', "git reset --hard")
        self.assert_found('echo "$(f ${v:-)}; git reset --hard)" done', "git reset --hard")
        # Advisor CR on #311: a "(" inside ${...} must not open a paren level.
        self.assert_found('echo "$(echo ${(f)x}) it\'s $(git reset --hard)"', "git reset --hard")
        self.assert_found('echo "$(echo ${x%%(*}; git reset --hard)"', "git reset --hard")

    def test_a_case_pattern_paren_does_not_close_the_substitution(self) -> None:
        self.assert_found('echo "$(case $v in a) git reset --hard;; esac)"', "git reset --hard")
        self.assert_found('echo "$(case $v in (a) git clean -fdx;; esac)"', "git clean -f")

    def test_a_heredoc_piped_into_a_shell_is_a_script(self) -> None:
        for shell in ("bash", "sh", "sudo bash -s"):
            with self.subTest(shell=shell):
                self.assert_found(f"echo \"$(cat <<'EOF' | {shell}\ngit reset --hard\nEOF\n)\"",
                                  "git reset --hard")

    def test_quotes_in_an_unquoted_heredoc_body_are_literal(self) -> None:
        # sh expands the body's substitutions; its ' and $' are plain text.
        for line in ("don't", "price $'5", "# a note"):
            with self.subTest(line=line):
                self.assert_found(f"cat <<EOF\n{line}\n$(git reset --hard)\nEOF", "git reset --hard")
        self.assert_found('echo "$(cat <<EOF\nprice $\'5\n$(git reset --hard)\nEOF\n)"',
                          "git reset --hard")

    def test_an_unterminated_ansi_c_quote_does_not_swallow_the_rest(self) -> None:
        self.assert_found("echo $'abc\ngit reset --hard", "git reset --hard")


class TestSubstitutionScannerStaysQuiet(unittest.TestCase):
    """The fixes above may not add false positives on the everyday shapes."""

    def assert_quiet(self, cmd: str) -> None:
        self.assertEqual(destructive.find_dirty_destructive(cmd), [], cmd)

    def test_commit_message_heredocs_documenting_resets(self) -> None:
        self.assert_quiet("git commit -m \"$(cat <<'EOF'\nfix: do not git reset --hard here\nEOF\n)\"")
        self.assert_quiet('git commit -m "$(cat <<EOF\ndocs: never git clean -fdx\nEOF\n)"')

    def test_a_heredoc_piped_to_a_non_shell_or_to_sh_c_is_data(self) -> None:
        self.assert_quiet("echo \"$(cat <<'EOF' | grep reset\ngit reset --hard\nEOF\n)\"")
        self.assert_quiet("echo \"$(cat <<'EOF' | bash -c 'echo hi'\ngit reset --hard\nEOF\n)\"")

    def test_braces_case_words_and_ansi_quotes_in_ordinary_use(self) -> None:
        self.assert_quiet('echo "$(printf %s ${v:-x})" ; echo ok')
        self.assert_quiet('echo "$(echo case; echo done)"')
        self.assert_quiet("echo $'it\\'s'; echo fine")


if __name__ == "__main__":
    unittest.main()


X = "git reset --hard"


def _caught(cmd: str) -> bool:
    return bool(destructive.find_dirty_destructive(cmd))


class TestStdinFedScripts(unittest.TestCase):
    """RELEASE-NOTES guard debt (run 1002): text piped into a shell, and a
    heredoc whose pipe into a shell the scanner did not follow, failed open.
    Each shape below runs X under bash (checked live); `$( )` is where the
    substitution scanner, not the line-split fallback, has to read it."""

    SHAPES = {
        "echo | bash": f"echo '{X}' | bash",
        "echo -e | sh": f"echo -e '{X}' | sh",
        "printf | bash": f"printf '%s\\n' '{X}' | bash",
        "echo | sudo bash": f"echo '{X}' | sudo bash",
        "echo |& bash": f"echo '{X}' |& bash",
        "xargs -I{} sh -c '{}'": f"echo '{X}' | xargs -I{{}} sh -c '{{}}'",
        "xargs -I % bash -c '%'": f"echo '{X}' | xargs -I % bash -c '%'",
        "xargs -0 bash -c": f"printf '%s' '{X}' | xargs -0 bash -c",
        "heredoc, pipe after terminator": f"cat <<EOF |\n{X}\nEOF\nbash",
        "heredoc, backslash-continued pipe": f"cat <<EOF | \\\nbash\n{X}\nEOF",
        "redirect-first <<EOF bash": f"<<EOF bash\n{X}\nEOF",
        "redirect-first, quoted tag, sudo": f"<<'EOF' sudo sh\n{X}\nEOF",
        "heredoc into xargs sh -c": f"cat <<EOF | xargs -I{{}} sh -c '{{}}'\n{X}\nEOF",
    }

    def test_each_shape_is_caught(self) -> None:
        for name, cmd in self.SHAPES.items():
            with self.subTest(name):
                self.assertTrue(_caught(cmd), cmd)

    def test_each_shape_is_caught_inside_a_substitution(self) -> None:
        for name, cmd in self.SHAPES.items():
            with self.subTest(name):
                self.assertTrue(_caught(f'y="$({cmd}\n)"'), cmd)

    def test_each_shape_is_caught_inside_bash_c(self) -> None:
        for name, cmd in self.SHAPES.items():
            if '"' in cmd:
                continue
            with self.subTest(name):
                self.assertTrue(_caught(f'bash -c "{cmd}"'), cmd)

    def test_force_push_and_base_delete_reach_their_checks(self) -> None:
        self.assertTrue(destructive.find_force_pushes("echo 'git push --force origin main' | bash"))
        self.assertIn("main", destructive.find_branch_deletions("printf 'git branch -D main' | sh"))


class TestStdinFedScriptsStayQuiet(unittest.TestCase):
    def test_text_piped_to_a_non_shell_is_data(self) -> None:
        for cmd in (f"echo '{X}' | cat", f"printf '{X}' | tee log", f"echo '{X}' > notes.txt"):
            with self.subTest(cmd):
                self.assertFalse(_caught(cmd))

    def test_a_shell_running_a_file_or_its_own_c_is_not_fed(self) -> None:
        for cmd in (f"echo '{X}' | bash script.sh", f"echo '{X}' | xargs bash",
                    "echo hi | bash -c 'echo ok'"):
            with self.subTest(cmd):
                self.assertFalse(_caught(cmd))

    def test_heredoc_into_a_non_shell_after_the_terminator_is_data(self) -> None:
        self.assertFalse(_caught(f'y="$(cat <<EOF |\n{X}\nEOF\nwc -l\n)"'))

    def test_a_pr_body_documenting_the_shape_is_data(self) -> None:
        cmd = ("gh pr create --body \"$(cat <<'EOF'\n"
               f"echo '{X}' | bash was the bug\nEOF\n)\"")
        self.assertFalse(_caught(cmd))


class TestHereStrings(unittest.TestCase):
    """`bash <<< '...'` runs the word as a script (the #321 follow-up)."""

    SHAPES = {
        "bash <<<": f"bash <<< '{X}'",
        "sh <<< dq": f'sh <<< "{X}"',
        "attached word": f"bash -s <<<'{X}'",
        "redirect first, sudo": f"<<< '{X}' sudo bash",
        "xargs sh -c": f"xargs -I{{}} sh -c '{{}}' <<< '{X}'",
    }

    def test_each_shape_is_caught_plain_and_in_a_substitution(self) -> None:
        for name, cmd in self.SHAPES.items():
            with self.subTest(name):
                self.assertTrue(_caught(cmd), cmd)
                self.assertTrue(_caught(f'y="$({cmd})"'), cmd)

    def test_force_push_reaches_its_check(self) -> None:
        self.assertTrue(destructive.find_force_pushes("bash <<< 'git push --force origin main'"))

    def test_a_here_string_to_a_non_script_reader_is_data(self) -> None:
        for cmd in (f"cat <<< '{X}'", f"grep -q y <<< '{X}'", f"bash f.sh <<< '{X}'",
                    f"bash -c 'echo ok' <<< '{X}'"):
            with self.subTest(cmd):
                self.assertFalse(_caught(cmd))


class TestLiteralVariablesAndQuietShells(unittest.TestCase):
    def test_a_literal_assigned_in_the_same_command_is_the_script(self) -> None:
        for cmd in (f"x='{X}'; bash <<< \"$x\"", f"x='{X}'\nbash <<< \"${{x}}\"",
                    f"export C='{X}' && echo \"$C\" | sh"):
            with self.subTest(cmd):
                self.assertTrue(_caught(cmd))

    def test_any_use_of_the_xargs_replace_string_runs(self) -> None:
        # #325 review: the string lands in shell code wherever it sits, so it is
        # never relaxed to "argument only" (each of these executed live).
        for body in ("cd /tmp && {}", "eval {}", "$({})", "`{}`", 'bash -c "{}"',
                     "f(){ {}; }; f"):
            with self.subTest(body):
                self.assertTrue(_caught(f"echo '{X}' | xargs -I{{}} sh -c '{body}'"))
        self.assertTrue(_caught(f"echo '{X}' | xargs -I{{}} sh -c 'echo {{}}' | sh"))

    def test_noexec_is_turned_back_off_by_a_later_option(self) -> None:
        # `bash -n +n` and `-n +o noexec` RUN (checked live in bash, zsh, dash).
        for fl in ("-n +n", "-n +o noexec", "-n -o pipefail"):
            with self.subTest(fl):
                self.assertTrue(_caught(f"echo '{X}' | bash {fl}"))

    def test_a_redirect_is_not_an_operand(self) -> None:
        for cmd in (f"bash <<< '{X}' >/dev/null", f"bash <<< '{X}' 2>&1",
                    f"bash <<< '{X}' > out.log", f"bash<<<'{X}'", f"bash 0<<< '{X}'",
                    f"echo '{X}' | bash 2>&1", f"echo '{X}' | bash >/dev/null",
                    f"echo '{X}' | bash -o pipefail", f"echo '{X}' | bash -",
                    f"echo '{X}' | bash -x -", f"echo '{X}' | bash -- -",
                    f"bash - <<< '{X}'"):
            with self.subTest(cmd):
                self.assertTrue(_caught(cmd))

    def test_shells_that_do_not_run_their_input_stay_quiet(self) -> None:
        # Each checked live with a benign payload: none executes.
        for cmd in (f"echo '{X}' | bash -n", f"echo '{X}' | bash -nx",
                    f"echo '{X}' | bash --version", f"echo '{X}' | bash --help",
                    f"bash -n <<< '{X}'", 'bash <<< "$UNKNOWN"',
                    f"echo '{X}' | bash f.sh 2>&1"):
            with self.subTest(cmd):
                self.assertFalse(_caught(cmd))


class TestStdinAliasesAndProcessSubstitution(unittest.TestCase):
    """Both-trees misses after #325: a script read from /dev/stdin, a script
    reader other than a shell, a here-string piped through cat, and process
    substitution (whose body always runs)."""

    SHAPES = {
        "source /dev/stdin": f"source /dev/stdin <<< '{X}'",
        ". /dev/stdin": f". /dev/stdin <<< '{X}'",
        "eval $(cat)": f"eval \"$(cat)\" <<< '{X}'",
        "eval `cat`": f"eval \"`cat`\" <<< '{X}'",
        "bash /dev/stdin": f"bash /dev/stdin <<< '{X}'",
        "| bash /dev/stdin": f"echo '{X}' | bash /dev/stdin",
        "| sh /dev/fd/0": f"echo '{X}' | sh /dev/fd/0",
        "| source /dev/stdin": f"echo '{X}' | source /dev/stdin",
        "cat <<< | bash": f"cat <<< '{X}' | bash",
        "bash < <(echo)": f"bash < <(echo '{X}')",
        "bash <(echo)": f"bash <(echo '{X}')",
        "source <(echo)": f"source <(echo '{X}')",
        "--rcfile value": f"echo '{X}' | bash --rcfile f",
        "--init-file value": f"echo '{X}' | bash --init-file f -x",
        "procsub body runs": f"diff <({X}) other",
        "output procsub runs": f"tee >({X}) < f",
    }

    def test_each_shape_is_caught_plain_and_in_a_substitution(self) -> None:
        for name, cmd in self.SHAPES.items():
            with self.subTest(name):
                self.assertTrue(_caught(cmd), cmd)
                self.assertTrue(_caught(f'y="$({cmd})"'), cmd)

    def test_readers_of_a_file_or_of_data_stay_quiet(self) -> None:
        for cmd in (f"cat notes <<< '{X}' | bash", f"diff <(echo '{X}') x",
                    f"source ./env.sh <<< '{X}'", f"eval \"$(foo)\" <<< '{X}'",
                    f"bash --rcfile f script.sh <<< '{X}'", f"grep -f <(echo '{X}') log",
                    f"cat <<< '{X}' | grep y", f"echo '<({X})'"):
            with self.subTest(cmd):
                self.assertFalse(_caught(cmd))
