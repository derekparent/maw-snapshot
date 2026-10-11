"""Structural destructive-op guard (backlog item 4).

Two-sided evidence from the arcs: BEHAVIORAL rules failed to prevent repeats
(base-branch deletion happened twice after a retarget-first reminder), while a
STRUCTURAL classifier block succeeded (it caught a mass-merge and an over-scoped
flag). Item #1's per-seat worktrees isolate a seat's *own* uncommitted work, but
two hazards survive worktree isolation, and this is the structural guard for them:

  1. Base-branch deletion with open child PRs — a remote/PR-graph fact, not a
     tree fact. Before deleting a branch, refuse if open PRs target it. Worktrees
     do NOT fix this.
  2. Destructive-on-own-uncommitted-work — `git reset --hard`, `git clean -f…`,
     `git checkout -- …` / `.`, `git restore`, `git stash drop/clear` against a
     DIRTY tree still loses the seat's own work even when isolated, as does
     `git worktree remove --force` of a dirty worktree (#349). A discard
     limited to plainly named paths is gated on those paths only, one stash
     drop by a literal ref is not gated, and discards in throwaway scratch
     review worktrees are exempt (#349).
  3. `git push --force` / `-f` / a `+refspec` without a lease, `--mirror`, or
     `--prune` — loses REMOTE commits or refs whatever the local tree looks like
     (#206). `--force-with-lease` passes.

Threat model (KD18 trust boundary): this is an ACCIDENT speed-bump surfaced to a
model that forgot the rule — NOT an adversary-proof block. A session with a shell
can set the documented override env var; the point is to force a conscious act,
the way the base-deletion races were unconscious. Overrides:
  MAW_ALLOW_BASE_DELETE=1   — permit base/PR-targeted branch deletion
  MAW_ALLOW_DESTRUCTIVE=1   — permit a destructive op against a dirty tree,
                              or a lease-less force-push

Robustness: this runs as a PreToolUse hook on EVERY Bash command, so it must
fail OPEN — any parse/runtime error allows the command (the guard adds value
when its checks succeed, never by breaking the shell). Detection is conservative
and shlex-guarded so a quoted command inside a message
(`git commit -m "use git reset --hard"`) is not mistaken for the op itself.
"""
from __future__ import annotations

import json
import os
import functools
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable

ALLOW = 0
BLOCK = 2

# Branches a maw agent should never delete; deletion is blocked regardless of
# the PR graph (override available). dev/develop included as common integration
# bases alongside the default branch.
PROTECTED_BASES = ("main", "master", "dev", "develop")

# git "global" options that take a value — skipped when locating the subcommand.
_VALUE_OPTS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# Command-prefix wrappers that may legitimately precede `git` at the command
# position (so `sudo git clean -fd` / `FOO=bar git reset --hard` still parse as
# git). Anything else before `git` means git is an ARGUMENT, not the command.
_CMD_PREFIXES = {"sudo", "env", "command", "time", "nice", "nohup", "xargs", "builtin", "exec",
                 "timeout", "stdbuf"}
# Wrappers that take one positional argument before the command.
_POSITIONAL_PREFIXES = {"timeout"}
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class Decision:
    exit_code: int
    message: str


def _git_run(args: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


def _gh_run(args: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, timeout=10)


class _QuoteWalkUntrusted(Exception):
    """The quote walk cannot be trusted for this command; use the old split.

    Raised for the shapes where "inside a quote" is not what the walk thinks:
    an unterminated quote (an apostrophe in ordinary prose) and a heredoc (whose
    body's quotes are literal to sh). Both would otherwise swallow the rest of
    the command — and with it any real destructive tail, turning a false
    POSITIVE fix into a false NEGATIVE. The rule from the #216 review: a quoting
    fix may remove false positives; it may never add a false negative the old
    regex caught.
    """


def _split_outside_quotes(cmd: str) -> list[str]:
    return [part for part, _before, _after in _split_with_seps(cmd)]


def _split_with_seps(cmd: str) -> "list[tuple[str, str, str]]":
    """(part, separator before it, separator after it); "" at either end."""
    parts = _split_walk(cmd)
    out = []
    for k, (part, after) in enumerate(parts):
        before = parts[k - 1][1] if k else ""
        out.append((part, before, after))
    return out


def _split_walk(cmd: str) -> "list[tuple[str, str]]":
    """Split on shell separators that are NOT inside a quoted span.

    The separator regex used to run over the WHOLE string before shlex, so a
    quoted multi-line body — a commit message, a PR body — was torn into lines
    and each line parsed as its own command. A body that DOCUMENTED
    `git reset --hard` was then blocked as if it ran it (#194), and the
    workaround (always use -F/--body-file) taught seats to route around a
    security guard.

    Raises `_QuoteWalkUntrusted` for an unterminated quote or a heredoc, so the
    caller falls back to the old whole-string split rather than dropping a real
    destructive tail into a swallowed span.
    """
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote is not None:
            # Only double quotes honour backslash escapes in sh; inside single
            # quotes a backslash is a literal, so \' does NOT extend the span.
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                buf.append(ch)
                buf.append(cmd[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        # A heredoc body's quotes are LITERAL to sh, so tracking them here would
        # open a span the body never closes. The old split handled heredocs
        # correctly and they were never the #194 false-positive shape (that was
        # -m/--body), so hand this command back.
        if cmd.startswith("<<", i):
            raise _QuoteWalkUntrusted("heredoc")
        # `#` starts a comment only at a word boundary (sh). Everything to the
        # end of the line is text — including an apostrophe, which would
        # otherwise open a span that swallows the next command.
        if ch == "#" and (not buf or buf[-1].isspace()):
            while i < len(cmd) and cmd[i] != "\n":
                i += 1
            continue
        if ch == "\\" and i + 1 < len(cmd):
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if cmd.startswith("&&", i) or cmd.startswith("||", i):
            parts.append(("".join(buf), cmd[i:i + 2]))
            buf = []
            i += 2
            continue
        if ch in ";\n|&":
            parts.append(("".join(buf), ch))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote is not None:
        # An apostrophe in ordinary prose ("don't") opens a span that never
        # closes. Trusting the walk here would swallow every later command.
        raise _QuoteWalkUntrusted("unterminated quote")
    parts.append(("".join(buf), ""))
    return parts


def _segments(cmd: str, strip: bool = True) -> list[list[str]]:
    """Split a command line into shell segments, each tokenized via shlex.

    A segment whose quoting shlex can't parse is DROPPED (fail-open): better to
    miss a malformed destructive op than to misfire on a quoted string.
    """
    try:
        raw = _split_outside_quotes(cmd)
    except _QuoteWalkUntrusted:
        # Fall back to the pre-#194 whole-string split. Its known cost is the
        # false positive on quoted data; that is strictly better than a false
        # negative on a real destructive command.
        raw = re.split(r"&&|\|\||[;\n|&]", cmd)
    out: list[list[str]] = []
    for part in raw:
        part = part.strip()
        if not part:
            continue
        try:
            toks = shlex.split(part)
        except ValueError:
            continue  # unbalanced quotes → skip (fail-open)
        if strip:
            toks = _strip_grammar(toks)
            toks = [t.lstrip("(") if k and t.startswith("(") and _base(toks[k - 1]) in _CMD_PREFIXES
                    else t for k, t in enumerate(toks)]
            while toks and toks[-1] == ")":
                toks.pop()
        if toks:
            out.append(toks)
    return out


# Shell grammar that can wrap a command without changing what it runs.
_LEAD_WORDS = {"(", "{", "if", "then", "do", "else", "elif", "while", "until", "!"}
_TRAIL_WORDS = {")", "}", "fi", "done", "esac"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
# Long shell options that take the next word as their value.
_SHELL_LONG_VALUE_OPTS = {"--rcfile", "--init-file"}


def _strip_grammar(toks: list[str]) -> list[str]:
    """Drop subshell/group/conditional/loop syntax around a command, so
    `(cd x && git reset --hard)`, `{ ...; }`, `if ...; then ...; fi` and
    `do ...; done` expose the command they run."""
    toks = list(toks)
    while toks:
        t = toks[0]
        if t in _LEAD_WORDS:
            toks.pop(0)
        elif t.startswith("(") and len(t) > 1:
            toks[0] = t.lstrip("(")
        elif t == "case" and len(toks) >= 3 and toks[2] == "in":
            del toks[:3]  # `case WORD in` opens the first arm
        elif t == "function" and len(toks) >= 2:
            del toks[:2]  # `function NAME { ... }`
        elif len(toks) >= 2 and toks[1] == "()":
            del toks[:2]  # `NAME () { ... }`
        elif t.endswith("()") and len(t) > 2:
            toks.pop(0)  # `NAME() { ... }`
        elif len(toks) > 1 and t.endswith(")") and "(" not in t and "$" not in t:
            toks.pop(0)  # a case-arm pattern: `a)`, `*)`, `x|y)`
        else:
            break
    while toks:
        t = toks[-1]
        if t in _TRAIL_WORDS:
            toks.pop()
        elif t.endswith(")") and not t.endswith("$()") and "(" not in t and len(t) > 1:
            toks[-1] = t.rstrip(")")
        else:
            break
    return toks


def _base(tok: str) -> str:
    """A command word by name: `/usr/bin/git` and `git` are the same command."""
    return os.path.basename(tok)


def _is_command_word(tok: str) -> bool:
    return _base(tok) in {"git", "eval", "cd", "pushd"} | _SHELLS | _CMD_PREFIXES


def _locate(toks: list[str]) -> int:
    """Index of the command word: past VAR=val assignments and prefix wrappers
    (sudo/env/nice/time/xargs/command/...), INCLUDING the wrappers' own options
    and option values (`sudo -u root git`, `env -i git`, `nice -n 5 git`).
    len(toks) when there is none."""
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):
        i += 1
    while i < len(toks) and _base(toks[i]) in _CMD_PREFIXES:
        positional = _base(toks[i]) in _POSITIONAL_PREFIXES
        i += 1
        while i < len(toks):
            t = toks[i]
            if _ASSIGN_RE.match(t) or t.startswith("-"):
                i += 1
            elif _is_command_word(t):
                break
            elif toks[i - 1].startswith("-") and not _ASSIGN_RE.match(toks[i - 1]):
                i += 1  # the value of the wrapper option before it
            elif positional:
                positional = False
                i += 1  # timeout's DURATION
            else:
                break
    return i


def _command_head(toks: list[str]) -> "tuple[str, int]":
    """(command name, its index) after assignments and prefix wrappers."""
    i = _locate(toks)
    return (_base(toks[i]), i) if i < len(toks) else ("", i)


def _shell_payload(toks: list[str]) -> "str | None":
    """The script a `sh -c '...'` / `bash -c` / `eval ...` segment runs."""
    head, i = _command_head(toks)
    if head == "eval":
        return " ".join(toks[i + 1:])
    if head in _SHELLS:
        # With -c the script is the first NON-OPTION word, not the word after
        # -c: `bash -o pipefail -c 'x'`, `bash -c -e 'x'` and `bash -euo
        # pipefail -c 'x'` all run x. Each o/O in a flag cluster takes the next
        # word as its value (`-o pipefail`, `+O extglob`).
        saw_c = False
        j = i + 1
        while j < len(toks):
            t = toks[j]
            if t == "--":
                j += 1
                break
            if t in _SHELL_LONG_VALUE_OPTS:
                j += 2
                continue
            if t.startswith("--"):
                j += 1
                continue
            if re.fullmatch(r"[-+][a-zA-Z]+", t):
                saw_c = saw_c or (t[0] == "-" and "c" in t)
                j += 1 + sum(1 for f in t[1:] if f in "oO")
                continue
            break
        if saw_c:
            return toks[j] if j < len(toks) else ""
    return None


# Commands whose heredoc body is a SCRIPT they run, not data they read.
_SCRIPT_READERS = _SHELLS | {"eval", "source", "."}
_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _nested_scripts(cmd: str) -> list[str]:
    """Every script a command substitution in `cmd` runs, at any depth: the
    body of each `$(...)` and `...`, plus the body of a heredoc fed to a
    shell. Single-quoted text is data and is skipped; double quotes do not
    stop a substitution (sh runs it). Each returned script is FLAT: its own
    nested substitutions are blanked (they are returned separately) and its
    heredoc bodies are removed, so segmenting it never re-reads a commit
    message as commands. `-m "$(cat <<'EOF' ... EOF)"` is the everyday shape
    that must stay quiet."""
    out: list[str] = []
    _scan(cmd, 0, None, out)
    out += _stdin_fed_scripts(cmd)
    return out


def _scan(s: str, i: int, close: "str | None", out: list[str],
          literal: bool = False) -> "tuple[str, int]":
    """Walk s from i to the unquoted `close` (")" or "`"; None = to the end).
    Returns (flat text of the span, index just past close).

    `literal` scans an unquoted-tag heredoc body: sh expands its substitutions
    but its quotes, `$'` and `#` are plain text, so treating `don't` as an
    open quote would hide a later `$(git reset --hard)`."""
    flat: list[str] = []
    sq = dq = False
    paren = 0
    brace = 0  # open ${...}: a `)` inside it (${v:-)}) closes nothing
    case = 0   # open case...esac: a pattern's `)` closes nothing
    heredocs: list[tuple[str, bool, bool, bool]] = []  # tag, quoted, strip_tabs, script
    seg_start = line_start = i
    n = len(s)
    while i < n:
        c = s[i]
        if sq:
            flat.append(c)
            sq = c != "'"
            i += 1
            continue
        if c == "\\":
            flat.append(s[i:i + 2])
            i += 2
            continue
        if close == "`" and c == "`":
            return "".join(flat), i + 1
        if s.startswith("$(", i) and not s.startswith("$((", i):
            body, i = _scan(s, i + 2, ")", out)
            out.append(body)
            flat.append(_sub_placeholder(body))
            continue
        if c == "`":
            body, i = _scan(s, i + 1, "`", out)
            out.append(body)
            flat.append(_sub_placeholder(body))
            continue
        if dq:
            dq = c != '"'
            flat.append(c)
            i += 1
            continue
        if (s.startswith("<(", i) or s.startswith(">(", i)) and not literal:
            # Process substitution: the body always runs (`diff <(cmd) x`).
            op = s[i:i + 2]
            body, i = _scan(s, i + 2, ")", out)
            out.append(body)
            # Keep it in the flat text: `bash <(echo ...)` is read as a script
            # source by the stdin check (its own substitutions are blanked).
            flat.append(f"{op}{body})")
            continue
        if literal:
            flat.append(c)
            i += 1
            continue
        if s.startswith("${", i):
            brace += 1
            flat.append("${")
            i += 2
            continue
        if c == "}" and brace:
            brace -= 1
        elif s.startswith("$'", i):
            # ANSI-C quoting: data, and unlike '...' a backslash escapes a
            # quote inside it. Read as a plain single quote, $'it\'s' would
            # end early and leave the rest of the string misquoted.
            j = i + 2
            while j < n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            if j < n:
                flat.append(s[i:j + 1])
                i = j + 1
            else:
                # Unterminated: sh would not run this at all, so it cannot
                # hide a command. Never let it swallow the rest as data.
                flat.append("$'")
                i += 2
            continue
        elif _word_at(s, i, "case") and _at_command_position(s[seg_start:i]):
            case += 1
        elif _word_at(s, i, "esac") and case:
            case -= 1
        if c == "'":
            sq = True
        elif c == '"':
            dq = True
        elif c == "#" and (i == 0 or s[i - 1].isspace()):
            while i < n and s[i] != "\n":
                i += 1
            continue
        elif c == "(" and not brace:
            # Inside ${...} a "(" is expansion syntax (${(f)x}, ${x%%(*}); its
            # ")" is suppressed below, so counting it would never close the $( ).
            paren += 1
        elif c == ")":
            if brace or (case and not paren):
                pass
            elif close == ")" and not paren:
                return "".join(flat), i + 1
            else:
                paren = max(0, paren - 1)
        elif c in ";|&":
            seg_start = i + 1
        elif s.startswith("<<", i) and not s.startswith("<<<", i):
            m = _HEREDOC_RE.match(s, i)
            if m:
                head, _at = _command_head(s[seg_start:i].split())
                if not head:
                    # Redirect first (`<<EOF bash`): the command follows the tag.
                    rest = re.split(r"[;|&\n]", s[m.end():], maxsplit=1)[0]
                    head, _at = _command_head(_HEREDOC_RE.sub(" ", rest).split())
                heredocs.append((m.group(3), bool(m.group(2)), m.group(1) == "-",
                                 head in _SCRIPT_READERS))
                flat.append(m.group(0))
                i = m.end()
                continue
        elif c == "\n":
            if heredocs and (_pipes_into_shell(s[line_start:i])
                             or _pipe_resumes_into_shell(s, i, heredocs)):
                # `cat <<EOF | bash`: the body is the shell's script.
                heredocs = [(t, q, st, True) for t, q, st, _x in heredocs]
            seg_start = line_start = i + 1
            if heredocs:
                flat.append(c)
                i = seg_start = line_start = _skip_heredoc_bodies(s, i + 1, heredocs, out)
                heredocs = []
                continue
        flat.append(c)
        i += 1
    return "".join(flat), n


def _sub_placeholder(body: str) -> str:
    """What a blanked substitution leaves in the flat text. `$(cat)` stays
    itself so `eval "$(cat)" <<< '...'` is still read as a stdin reader."""
    if re.fullmatch(r"\s*cat(\s+(-|/dev/stdin|/dev/fd/0))?\s*", body):
        return " $(cat) "
    return " _SUB_ "


def _word_at(s: str, i: int, word: str) -> bool:
    """`word` as a whole shell word at s[i]."""
    if not s.startswith(word, i):
        return False
    before = s[i - 1] if i else " "
    after = s[i + len(word)] if i + len(word) < len(s) else " "
    return before in " \t\n;|&(" and after in " \t\n;|&)"


def _at_command_position(before: str) -> bool:
    """True if the next word starts a command (only grammar words precede it)."""
    words = before.split()
    return not words or words[-1] in _LEAD_WORDS


def _pipes_into_shell(line: str) -> bool:
    """True if a later pipe stage of this line is a shell reading stdin (no -c),
    or xargs feeding stdin to a shell's -c. A backslash-newline joins lines."""
    line = line.replace("\\\n", " ").replace("|&", "|")
    stages = re.split(r"(?<!\|)\|(?!\|)", line)
    for stage in stages[1:]:
        toks = stage.split()
        head, at = _command_head(toks)
        if head in _SHELLS and "-c" not in toks[at + 1:]:
            return True
        if _stage_runs_stdin(stage):
            return True
    return False


def _pipe_resumes_into_shell(s: str, nl: int,
                             heredocs: "list[tuple[str, bool, bool, bool]]") -> bool:
    """The line at `nl` ends in a pipe, so the pipeline resumes AFTER the
    heredoc bodies (`cat <<EOF |` NL body NL `EOF` NL `bash`). True if that
    next stage is a shell reading stdin."""
    line = s[s.rfind("\n", 0, nl) + 1:nl].rstrip()
    if not line.endswith("|") or line.endswith("||"):
        return False
    end = _skip_heredoc_bodies(s, nl + 1, [(t, True, st, False) for t, _q, st, _x in heredocs], [])
    rest = s[end:].lstrip("\n")
    stage = re.split(r"[;&\n]|\|", rest, maxsplit=1)[0]
    return _pipes_into_shell("x|" + stage)


def _stage_toks(stage: str) -> list[str]:
    return list(_stage_toks_cached(stage))


@functools.lru_cache(maxsize=256)
def _stage_toks_cached(stage: str) -> "tuple[str, ...]":
    # Several checks read the same segment, and shlex is slow on a long
    # quoted body (a 20,000-line commit message is one segment).
    try:
        return tuple(_strip_grammar(shlex.split(stage)))
    except ValueError:
        return tuple(_strip_grammar(stage.split()))


def _stage_runs_stdin(stage: str) -> bool:
    """True if this pipe stage runs its STDIN as shell code: a shell with no
    -c script (or `-s`), or xargs handing each input to a shell's -c (the
    script is empty or carries the replace string, `xargs -I{} sh -c '{}'`)."""
    return _toks_run_stdin(_stage_toks(stage))


_REDIRECT = re.compile(r"(\d*|&)(>>|>\||>&|>|<&|<>|<)(.*)", re.S)


def _split_redirects(toks: list[str]) -> "tuple[list[str], list[str]]":
    """(the words left once redirections are removed, the here-string words).
    A redirect is not an operand: `bash 2>&1`, `bash >/dev/null` and
    `bash <<< '...' > out.log` still run stdin. Handles `bash<<<'...'` (shlex
    keeps it one word) and an fd prefix (`0<<<`)."""
    rest: list[str] = []
    here: list[str] = []
    k = 0
    while k < len(toks):
        t = toks[k]
        if "<<<" in t:
            pre, _sep, word = t.partition("<<<")
            if pre and not pre.isdigit():
                rest.append(pre)
            if word:
                here.append(word)
            elif k + 1 < len(toks):
                here.append(toks[k + 1])
                k += 1
            k += 1
            continue
        m = _REDIRECT.fullmatch(t)
        if m:
            if not m.group(3) and k + 1 < len(toks):
                k += 1  # the target is the next word
            k += 1
            continue
        rest.append(t)
        k += 1
    return rest, here


# Operands that name the shell's own stdin as the script file.
_STDIN_OPERANDS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
_EVAL_STDIN = re.compile(r"\$\(\s*cat(\s+(-|/dev/stdin|/dev/fd/0))?\s*\)|`\s*cat\s*`")


def _toks_run_stdin(toks: list[str]) -> bool:
    toks, _here = _split_redirects(toks)
    head, at = _command_head(toks)
    if head in ("source", "."):
        return at + 1 < len(toks) and toks[at + 1] in _STDIN_OPERANDS
    if head == "eval":
        return bool(_EVAL_STDIN.search(" ".join(toks[at + 1:])))
    if head not in _SHELLS:
        return False
    noexec = reads_stdin = False
    j = at + 1
    while j < len(toks):
        t = toks[j]
        if t == "-":
            reads_stdin = True  # a lone dash: the script is stdin
            break
        if t == "--":
            j += 1
            break
        if t in ("--version", "--help"):
            return False  # prints and exits
        if t in ("-o", "+o"):
            noexec = False  # an option set by name: cannot tell, assume it runs
            j += 2
            continue
        if t in _SHELL_LONG_VALUE_OPTS:
            j += 2  # `--rcfile f`: the value is not the script
            continue
        if re.fullmatch(r"[-+][a-zA-Z]+", t):
            if "n" in t[1:]:
                noexec = t[0] == "-"  # -n parses only; a later +n turns it off
            if "o" in t[1:] or "O" in t[1:]:
                noexec = False
                j += 1  # the -o/-O value
            reads_stdin = reads_stdin or (t[0] == "-" and "s" in t[1:])
            j += 1
            continue
        if t.startswith("--"):
            j += 1
            continue
        break
    if noexec:
        return False
    payload = _shell_payload(toks)
    if any(_base(t) == "xargs" for t in toks[:at]):
        if payload is None:
            return False  # `xargs bash f` runs files named by the input
        repl = "{}"
        for k, t in enumerate(toks[:at]):
            if t in ("-I", "--replace") and k + 1 < at:
                repl = toks[k + 1]
            elif t.startswith("-I") and len(t) > 2:
                repl = t[2:]
            elif t.startswith("--replace="):
                repl = t.split("=", 1)[1] or "{}"
        # Any use of the replace string: it lands in shell code (`$({})`,
        # `bash -c "{}"`, `echo {} | sh`), and a missed run costs more than a
        # false positive.
        return not payload.strip() or repl in payload
    if payload is not None:
        return False
    return reads_stdin or j >= len(toks) or toks[j] in _STDIN_OPERANDS


_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _literal_assignments(parts: "list[tuple[str, str, str]]") -> dict[str, str]:
    """NAME=value words from segments that only assign (`x='...'`, `export x=...`)."""
    found: dict[str, str] = {}
    for part, _b, _a in parts:
        if "=" not in part:
            continue
        toks = _stage_toks(part)
        if toks and toks[0] in ("export", "local", "readonly", "declare"):
            toks = toks[1:]
        if toks and all(_ASSIGN_RE.match(t) for t in toks):
            for t in toks:
                name, _eq, value = t.partition("=")
                found[name] = value
    return found


def _stdin_fed_scripts(cmd: str) -> list[str]:
    """Scripts an `echo ... | bash` / `printf ... | sh` pipeline runs: the
    text echo or printf writes, when the next stage runs stdin as code."""
    try:
        parts = _split_with_seps(cmd)
    except _QuoteWalkUntrusted:
        bits = re.split(r"(&&|\|\||[;\n|&])", cmd)
        parts = [(bits[k], bits[k - 1] if k else "", bits[k + 1] if k + 1 < len(bits) else "")
                 for k in range(0, len(bits), 2)]
    assigned = _literal_assignments(parts)

    def resolve(text: str) -> str:
        # `x='git reset --hard'; bash <<< "$x"`: a literal assigned in this
        # same command is the script; an unknown variable stays as written.
        return _VAR_REF.sub(lambda m: assigned.get(m.group(1), m.group(0)), text)

    out: list[str] = []
    for k, (part, _before, after) in enumerate(parts[:-1]):
        nxt = parts[k + 1]
        if after == "|" and not nxt[0].strip() and nxt[2] == "&" and k + 2 < len(parts):
            nxt = parts[k + 2]  # `|&` pipes stderr too; the split reads `|` then `&`
        if after != "|" or not _stage_runs_stdin(nxt[0]):
            continue
        text = _produced_text(_stage_toks(part))
        if text is not None:
            out.append(resolve(text).replace("\\n", "\n"))
    for part, _before, _after in parts:
        if "<<<" in part:
            out += [resolve(w) for w in _here_string_scripts(_stage_toks(part))]
        if "<(" in part:
            out += [resolve(t) for t in _procsub_scripts(part)]
    return out


def _produced_text(toks: list[str]) -> "str | None":
    """What a stage writes to stdout when that is knowable from its words:
    echo/printf arguments, or the here-string `cat` copies (`cat <<< '...'`)."""
    rest, here = _split_redirects(toks)
    head, at = _command_head(rest)
    args = rest[at + 1:]
    if head == "echo":
        while args and re.fullmatch(r"-[neE]+", args[0]):
            args = args[1:]
        return " ".join(args)
    if head == "printf":
        return "\n".join(args)
    if head == "cat" and here and not [a for a in args if a != "-"]:
        return "\n".join(here)
    return None


def _procsub_bodies(part: str) -> "tuple[str, list[str]]":
    """`part` with each `<(...)` replaced by /dev/stdin, and the bodies.
    Single-quoted text is skipped; parens and double quotes nest."""
    out: list[str] = []
    bodies: list[str] = []
    i, n = 0, len(part)
    while i < n:
        c = part[i]
        if c == "'":
            j = part.find("'", i + 1)
            j = n if j == -1 else j + 1
            out.append(part[i:j])
            i = j
            continue
        if part.startswith("<(", i):
            depth, j, q = 1, i + 2, ""
            while j < n and depth:
                ch = part[j]
                if q:
                    q = "" if ch == q else q
                elif ch in "'\"":
                    q = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                j += 1
            bodies.append(part[i + 2:j - 1])
            out.append(" /dev/stdin ")
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out), bodies


def _procsub_scripts(part: str) -> list[str]:
    """The text a process substitution writes when a script reader takes it as
    its script: `bash <(echo '...')`, `source <(...)`, `bash < <(...)`."""
    if "<(" not in part:
        return []
    head, _at = _command_head(_stage_toks(part))
    if head not in _SHELLS and head not in ("source", ".", "eval"):
        return []
    flat, bodies = _procsub_bodies(part)
    if not bodies or not _toks_run_stdin(_stage_toks(flat)):
        return []
    texts = [_produced_text(_stage_toks(b)) for b in bodies]
    return [t for t in texts if t is not None]


def _here_string_scripts(toks: list[str]) -> list[str]:
    """The word a here-string (`bash <<< '...'`) feeds a command that runs its
    stdin as code. The redirect may sit anywhere in the command."""
    rest, words = _split_redirects(toks)
    if not words or not _toks_run_stdin(rest):
        return []
    return [w.replace("\\n", "\n") for w in words]


def _skip_heredoc_bodies(s: str, i: int, heredocs: "list[tuple[str, bool, bool, bool]]",
                         out: list[str]) -> int:
    """Consume the pending heredoc bodies starting at i; return the index after
    the last terminator. A body fed to a shell is a script (scanned whole); an
    unquoted-tag body is expanded by sh, so its substitutions run; a quoted-tag
    body is literal data."""
    for tag, quoted, strip_tabs, script in heredocs:
        lines: list[str] = []
        while i < len(s):
            j = s.find("\n", i)
            line = s[i:] if j == -1 else s[i:j]
            i = len(s) if j == -1 else j + 1
            if (line.lstrip("\t") if strip_tabs else line) == tag:
                break
            lines.append(line)
        body = "\n".join(lines)
        if script:
            flat, _ = _scan(body, 0, None, out)
            out.append(flat)
        elif not quoted:
            _scan(body, 0, None, out, literal=True)
    return i


_FIND_EXEC = {"-exec": False, "-ok": False, "-execdir": True, "-okdir": True}


def _find_exec_units(toks: list[str]) -> "list[tuple[str, bool]]":
    """The commands a `find` segment runs via -exec/-ok (in find's directory)
    or -execdir/-okdir (in each match's directory, so unknowable here), as
    (command line, runs-in-match-dir) pairs."""
    head, i = _command_head(toks)
    if head != "find":
        return []
    units: list[tuple[str, bool]] = []
    j = i + 1
    while j < len(toks):
        if toks[j] in _FIND_EXEC:
            execdir = _FIND_EXEC[toks[j]]
            k = j + 1
            while k < len(toks) and toks[k] not in (";", "+"):
                k += 1
            if k > j + 1:
                units.append((shlex.join(toks[j + 1:k]), execdir))
            j = k
        j += 1
    return units


def _expanded_segments(cmd: str, _depth: int = 0,
                       overflow: "list[str] | None" = None) -> list[list[str]]:
    """_segments, with every `sh -c` / `eval` payload replaced by ITS segments
    and every command substitution's script added, so a check that reads
    segments sees a git op a shell wrapper or a `$(...)` runs."""
    out: list[list[str]] = []
    for toks in _segments(cmd):
        for unit, _execdir in _find_exec_units(toks):
            if _depth < MAX_PAYLOAD_DEPTH:
                out += _expanded_segments(unit, _depth + 1, overflow)
            elif overflow is not None:
                overflow.append(unit)
        payload = _shell_payload(toks)
        if payload is None:
            out.append(toks)
        elif _depth < MAX_PAYLOAD_DEPTH:
            out += _expanded_segments(payload, _depth + 1, overflow)
        elif overflow is not None:
            overflow.append(payload)
    for script in _nested_scripts(cmd):
        if _depth < MAX_PAYLOAD_DEPTH:
            out += _expanded_segments(script, _depth + 1, overflow)
        elif overflow is not None:
            overflow.append(script)
    return out


def nests_too_deep(cmd: str) -> bool:
    """True if a shell payload or substitution sits deeper than
    MAX_PAYLOAD_DEPTH, where no check reads it."""
    overflow: list[str] = []
    _expanded_segments(cmd, 0, overflow)
    return bool(overflow)


def _git_subcommand(toks: list[str]) -> tuple[str | None, list[str]]:
    """(subcommand, args-after-it) if `git` is the COMMAND here, else (None, []).

    git must be at the command position — after assignments and prefix
    wrappers with their options (see _locate) — and is matched by name, so a
    path-qualified `/usr/bin/git` counts. Matching `git` ANYWHERE would treat
    git-as-DATA as git-as-command: `echo git reset --hard` on a dirty tree
    would false-block (advisor Finding 1).
    """
    i = _locate(toks)
    if i >= len(toks) or _base(toks[i]) != "git":
        return None, []
    i += 1
    while i < len(toks):
        t = toks[i]
        if t in _VALUE_OPTS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t, toks[i + 1:]
    return None, []


def _command_sets(cmd: str, var: str) -> bool:
    """True if the COMMAND STRING itself gives `var` truthy (1/true/yes) to the
    commands the guard would block.

    #86: `MAW_ALLOW_DESTRUCTIVE=1 git checkout -- f` and
    `export MAW_ALLOW_DESTRUCTIVE=1; git checkout ...` both put the override in
    the command, not the session env — so reading only os.environ made the
    documented override unreachable from the very Bash call that trips the guard.

    Scoped the way the shell scopes it: an inline `VAR=1 cmd` reaches only that
    one command, so it counts only when it prefixes EVERY guarded command in
    the string (`VAR=1 true; git reset --hard` does not override). An `export`
    counts only at top level: inside `( … )` or `{ … }` it may never reach the
    next command, so it is ignored there.

    Quote-safe: reuses `_segments` (shlex), so a quoted occurrence as an ARGUMENT
    (`git commit -m "MAW_ALLOW_DESTRUCTIVE=1"`) is never mistaken for an override.
    """
    target = f"{var}="

    def _truthy(tok: str) -> bool:
        val = tok[len(target):].strip().strip('"').strip("'").lower()
        return val in ("1", "true", "yes")

    # An export counts only as its own top-level statement: not inside ( ) or
    # { } (raw first token), not in a pipeline, and not after && / || (it may
    # never run). A command the quote walk cannot trust gets no export credit.
    try:
        statements = _split_with_seps(cmd)
    except _QuoteWalkUntrusted:
        statements = []
    for part, before, after in statements:
        if before not in ("", ";", "\n") or after not in ("", ";", "\n"):
            continue
        try:
            raw = shlex.split(part)
        except ValueError:
            continue
        if raw and raw[0] == "export":
            if any(t.startswith(target) and _truthy(t) for t in raw[1:]):
                return True

    def _prefix_sets(toks: list[str]) -> bool:
        return any(t.startswith(target) and _truthy(t)
                   for t in toks[:_locate(toks)] if _ASSIGN_RE.match(t))

    guarded = [toks for toks in _segments(cmd) if _is_guarded(toks)]
    return bool(guarded) and all(_prefix_sets(toks) for toks in guarded)


def _is_guarded(toks: list[str]) -> bool:
    """Does this one segment run something the guard would block?"""
    if _shell_payload(toks) is not None:
        return True  # a nested script is opaque; an inline override never reaches in
    if _destructive_desc(toks, repeated=True):
        return True
    sub, args = _git_subcommand(toks)
    if sub == "branch" and _has_delete_flag(args):
        return True
    return sub == "push"


def _strip_ref(r: str) -> str:
    r = r.strip().lstrip(":")
    if r.startswith("refs/heads/"):
        r = r[len("refs/heads/"):]
    return r


def _has_delete_flag(args: list[str]) -> bool:
    if "--delete" in args or "-d" in args or "-D" in args:
        return True
    # combined short flags containing d/D, e.g. -Dr
    return any(re.fullmatch(r"-[a-zA-Z]*[dD][a-zA-Z]*", a) for a in args)


def find_branch_deletions(cmd: str) -> list[str]:
    """Branch names a command would delete (via `git branch` or `git push`)."""
    out: list[str] = []
    for toks in _expanded_segments(cmd):
        sub, args = _git_subcommand(toks)
        if sub == "branch" and _has_delete_flag(args):
            out += [a for a in args if not a.startswith("-")]
        elif sub == "push":
            nonflags = [a for a in args if not a.startswith("-") and not a.startswith(":")]
            colon = [a for a in args if a.startswith(":")]
            has_delete = "--delete" in args or "-d" in args
            # nonflags[0] is the remote; the rest are refspecs
            refspecs = nonflags[1:]
            for ref in colon:
                out.append(_strip_ref(ref))
            if has_delete:
                out += [_strip_ref(r) for r in refspecs]
    return [b for b in (_strip_ref(x) for x in out) if b]


def _git_dash_c(toks: list[str]) -> list[str]:
    """The `-C <dir>` values among git's global options, in order."""
    i = _locate(toks)
    if i >= len(toks) or _base(toks[i]) != "git":
        return []
    dirs: list[str] = []
    i += 1
    while i < len(toks):
        t = toks[i]
        if t in _VALUE_OPTS:
            if t == "-C" and i + 1 < len(toks):
                dirs.append(toks[i + 1])
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    return dirs


# A tree the guard cannot know: git was pointed at another repository
# (GIT_DIR / --git-dir) without saying which working tree. Such an op blocks.
UNKNOWABLE_TREE = "\0unknowable-tree"


def _git_redirects(toks: list[str]) -> "tuple[str | None, str | None]":
    """(work_tree, git_dir) named by env assignments before `git` (also after
    `env`) or by git's --work-tree / --git-dir options, else None."""
    work, gdir = None, None
    i = _locate(toks)
    for t in toks[:i]:
        name, _eq, value = t.partition("=")
        if _eq and _ASSIGN_RE.match(t) and name == "GIT_WORK_TREE":
            work = value
        elif _eq and _ASSIGN_RE.match(t) and name == "GIT_DIR":
            gdir = value
    if i >= len(toks) or _base(toks[i]) != "git":
        return work, gdir
    i += 1
    while i < len(toks) and toks[i].startswith("-"):
        t = toks[i]
        if t == "-c" and i + 1 < len(toks) and toks[i + 1].lower().startswith("core.worktree="):
            work = toks[i + 1].split("=", 1)[1]
        for opt in ("--work-tree", "--git-dir"):
            if t == opt and i + 1 < len(toks):
                if opt == "--work-tree":
                    work = toks[i + 1]
                else:
                    gdir = toks[i + 1]
            elif t.startswith(opt + "="):
                if opt == "--work-tree":
                    work = t[len(opt) + 1:]
                else:
                    gdir = t[len(opt) + 1:]
        i += 2 if t in _VALUE_OPTS else 1
    return work, gdir


def _resolve(base: "str | None", path: str) -> "str | None":
    # The guard does not run the shell: a target it would have to EXPAND
    # ($VAR, `cmd`, ~user, `cd -`) is unknowable, and unknowable blocks.
    if base == UNKNOWABLE_TREE or path == "-" or path.startswith("~") \
            or "$" in path or "`" in path:
        return UNKNOWABLE_TREE
    if os.path.isabs(path):
        return os.path.realpath(path)
    if base is None:
        return None  # relative to an unknown directory: fall back to the hook's cwd
    # realpath: a symlink or `..` must land on the tree git will actually touch
    return os.path.realpath(os.path.join(base, path))


def find_dirty_destructive(cmd: str) -> list[str]:
    """Descriptions of destructive ops that would clobber uncommitted work."""
    return [desc for desc, _where in find_dirty_destructive_at(cmd, None)]


# Shell-payload nesting (sh -c, eval) deeper than this is not interpreted: the
# tree it acts on is unknowable, so it blocks rather than recurse without end.
MAX_PAYLOAD_DEPTH = 8


def find_dirty_destructive_at(cmd: str, cwd: "str | None",
                              _depth: int = 0) -> "list[tuple[str, str | None]]":
    """(description, directory) pairs for each destructive op, one per directory
    it could act in. See `_located_ops`, which also carries the paths each op
    names and the segment it came from."""
    out: list[tuple[str, str | None]] = []
    for op in _located_ops(cmd, cwd, _depth):
        if (op.desc, op.where) not in out:
            out.append((op.desc, op.where))
    return out


@dataclass(frozen=True)
class _Op:
    """One destructive op the command may run.

    desc   -- the op family ("git checkout (discard)", ...)
    where  -- the directory it acts in (None = the hook's cwd; UNKNOWABLE_TREE)
    paths  -- the pathspecs it is limited to, or None for the whole tree
    seg    -- the segment's text, for the block message (#349)
    """
    desc: str
    where: "str | None"
    paths: "tuple[str, ...] | None" = None
    seg: str = ""
    # The named paths are restored from a revision, not from the index: that
    # overwrites an UNTRACKED or IGNORED file of the same name too.
    sourced: bool = False


def _located_ops(cmd: str, cwd: "str | None", _depth: int = 0,
                 _repeated: bool = False) -> "list[_Op]":
    """Every destructive op, one per directory it could act in. git's `-C` is deterministic (git fails if the dir is
    missing), but a `cd` is not: it can fail (`cd x || git ...`), or run in a
    pipe stage or subshell and never reach the next command. So a `cd` ADDS a
    candidate directory and never removes one; the caller blocks if ANY
    candidate is dirty. Paths resolve against `cwd` (the hook event's cwd);
    None means the hook's own cwd."""
    hits: list[_Op] = []
    repeated = _repeated or bool(_REPEATS.search(cmd))

    def add(op: _Op) -> None:
        if op not in hits:
            hits.append(op)

    candidates: list[str | None] = [cwd]
    for toks in _segments(cmd):
        # find -exec runs its command in find's directory; -execdir in each
        # match's, which the guard cannot know.
        for unit, execdir in _find_exec_units(toks):
            if _depth >= MAX_PAYLOAD_DEPTH:
                break
            for c in list(candidates):
                for op in _located_ops(unit, c, _depth + 1, repeated):
                    add(_Op(op.desc, UNKNOWABLE_TREE if execdir else op.where,
                            op.paths, shlex.join(toks)))
        payload = _shell_payload(toks)
        if payload is not None:
            if _depth >= MAX_PAYLOAD_DEPTH:
                add(_Op("nested shell payload", UNKNOWABLE_TREE, None, shlex.join(toks)))
                continue
            # A nested script the guard does not interpret: if it contains a
            # destructive git op, where it runs is unknowable, so it blocks.
            for op in _located_ops(payload, None, _depth + 1, repeated):
                add(_Op(op.desc, UNKNOWABLE_TREE, op.paths, shlex.join(toks)))
            continue
        first, at = _command_head(toks)
        toks = toks[at:] if first in ("cd", "pushd") else toks
        if first in ("cd", "pushd") and len(toks) == 1:
            if UNKNOWABLE_TREE not in candidates:  # bare cd: $HOME, unknowable here
                candidates.append(UNKNOWABLE_TREE)
            continue
        if first in ("cd", "pushd") and len(toks) >= 2:
            target = toks[-1].rstrip(")")
            for c in list(candidates):
                moved = _resolve(c, target)
                if moved not in candidates:
                    candidates.append(moved)
            continue
        found = _destructive_desc(toks, repeated)
        if not found:
            continue
        seg = shlex.join(toks)
        paths = _discard_paths(toks) if found in _PATH_SCOPED_OPS else None
        if paths is not None and not _pathspecs_as_written(cmd, toks):
            paths = None
        sourced = paths is not None and _restores_from_source(toks)
        target = _worktree_remove_target(toks) if found == WORKTREE_REMOVE else None
        work, gdir = _git_redirects(toks)
        for c in candidates:
            where = c
            for d in _git_dash_c(toks):
                where = _resolve(where, d)
            if work is not None:
                where = _resolve(where, work)
            elif gdir is not None:
                where = UNKNOWABLE_TREE
            if found == WORKTREE_REMOVE:
                # The op destroys the TARGET worktree's dirt, wherever git runs.
                if target is None:
                    where = UNKNOWABLE_TREE
                elif where is None and not os.path.isabs(target) \
                        and _resolve(None, target) is None:
                    where = _resolve(os.getcwd(), target)
                else:
                    where = _resolve(where, target)
            add(_Op(found, where, paths, seg, sourced))
    # A command substitution runs in the calling shell's directory: check it
    # against every candidate the command could be in by then.
    for script in _nested_scripts(cmd):
        if _depth >= MAX_PAYLOAD_DEPTH:
            add(_Op("nested shell payload", UNKNOWABLE_TREE, None, script.strip()))
            break
        for c in list(candidates):
            for op in _located_ops(script, c, _depth + 1, repeated):
                add(op)
    return hits


WORKTREE_REMOVE = "git worktree remove --force"
STASH_OP = "git stash drop/clear"

# `stash@{2}` carries braces, but they are literal to sh (no comma, no range).
_LITERAL_STASH_REF = re.compile(r"(refs/)?stash@\{\d+\}")

# Ops that may be limited to named paths: only the dirt IN those paths is lost.
_PATH_SCOPED_OPS = frozenset(("git checkout (discard)", "git restore"))

# A word the shell passes to git exactly as written. Anything else may be
# rewritten first ({a,b}, $VAR, `cmd`, ~, =cmd, and zsh globs such as
# `b.py(N)` or `(a|b).py`), so it cannot be handed to `git status` as written:
# judge the whole tree instead. An allowlist, because the seats' shell is zsh
# and its expansion syntax is too wide to enumerate.
_LITERAL_WORD = re.compile(r"[A-Za-z0-9_./@%+,:-]+")

# A stash drop inside one of these may run more than once: a literal ref is
# then no longer one entry (`while git stash drop stash@{0}` is `stash clear`).
_REPEATS = re.compile(r"(?<![A-Za-z0-9_-])(while|until|for|repeat|foreach|xargs|find|"
                      r"parallel|watch)(?![A-Za-z0-9_-])")

_RESTORE_FLAGS = frozenset((
    "-S", "--staged", "-W", "--worktree", "-q", "--quiet", "--progress",
    "--no-progress", "--ours", "--theirs", "-m", "--merge", "--ignore-unmerged",
    "--overlay", "--no-overlay", "--recurse-submodules", "--no-recurse-submodules",
    "--ignore-skip-worktree-bits", "-SW", "-WS",
))


def _discard_paths(toks: list[str]) -> "tuple[str, ...] | None":
    """The pathspecs a `git checkout -- <paths>` / `git restore <paths>` is
    limited to, or None when it may touch the whole tree or the guard cannot
    tell (#349). A path-scoped discard destroys only the uncommitted changes
    in those paths, so a mutation-test restore of a file the compound itself
    changed no longer refuses the whole compound because ANOTHER file is
    dirty. None keeps the whole-tree check, which is the pre-#349 behavior."""
    sub, args = _git_subcommand(toks)
    paths: list[str] = []
    if sub == "checkout":
        if "-f" in args or "--force" in args or any(
                a.startswith("--pathspec-from-file") for a in args):
            return None
        if "--" in args:
            paths = args[args.index("--") + 1:]
        elif [a for a in args if not a.startswith("-")] == ["."]:
            paths = ["."]
        else:
            return None
    elif sub == "restore":
        k = 0
        while k < len(args):
            a = args[k]
            if a == "--":
                paths += args[k + 1:]
                break
            if a in ("-s", "--source"):
                k += 2
                continue
            if a.startswith(("--source=", "--conflict=")) or (
                    a.startswith("-s") and len(a) > 2 and not a.startswith("--")):
                k += 1
                continue
            if a in _RESTORE_FLAGS:
                k += 1
                continue
            if a.startswith("-"):
                return None  # unknown option (or --pathspec-from-file): whole tree
            paths.append(a)
            k += 1
    else:
        return None
    if not paths or not all(_LITERAL_WORD.fullmatch(p) for p in paths):
        return None
    return tuple(paths)


# git options the guard models when it reads a path-scoped discard. Any other
# option before the subcommand may change how git matches the paths
# (`--icase-pathspecs`, `--glob-pathspecs`, `-c core.x=...`), and the guard's
# own `git status -- <paths>` would not carry it.
_SCOPED_GIT_OPTS = {"-C", "--git-dir", "--work-tree"}
_SCOPED_GIT_ENV = {"GIT_DIR", "GIT_WORK_TREE"}


def _pathspecs_as_written(cmd: str, toks: list[str]) -> bool:
    """True if git will match this discard's pathspecs the way the guard's own
    status call does. False (judge the whole tree) when the command sets any
    other GIT_* variable or names a *_PATHSPECS setting anywhere, or gives
    git an option the guard does not model before the subcommand (#349
    review round 4: `git --icase-pathspecs checkout -- B.PY` discards a
    dirty b.py while `git status -- B.PY` reads clean)."""
    if re.search(r"pathspecs", cmd, re.I):
        return False
    for name in re.findall(r"(?<![A-Za-z0-9_])(GIT_[A-Z0-9_]+)", cmd):
        if name not in _SCOPED_GIT_ENV:
            return False
    i = _locate(toks)
    if i >= len(toks) or _base(toks[i]) != "git":
        return False
    i += 1
    while i < len(toks) and toks[i].startswith("-"):
        t = toks[i]
        if t in _SCOPED_GIT_OPTS:
            i += 2
        elif t.startswith(("--git-dir=", "--work-tree=")):
            i += 1
        else:
            return False
    return True


def _restores_from_source(toks: list[str]) -> bool:
    """True if a path-scoped checkout/restore takes the paths from a revision
    (`git checkout HEAD~1 -- f`, `git restore -s REV f`). Any doubt is True:
    the paths are then checked with untracked files included."""
    sub, args = _git_subcommand(toks)
    if sub == "checkout":
        head = args[:args.index("--")] if "--" in args else []
        return any(not a.startswith("-") for a in head)
    if sub == "restore":
        opts = args[:args.index("--")] if "--" in args else args
        return any(a == "--source" or a.startswith("--source=")
                   or (a.startswith("-") and not a.startswith("--") and "s" in a[1:])
                   for a in opts)
    return True


def _worktree_remove_target(toks: list[str]) -> "str | None":
    """The worktree path a `git worktree remove --force <path>` names, or None
    when it is missing or the shell would expand it."""
    _sub, args = _git_subcommand(toks)
    rest = [a for a in args[1:] if not a.startswith("-")]
    if not rest or not _LITERAL_WORD.fullmatch(rest[0]):
        return None
    return rest[0]


def _destructive_desc(toks: list[str], repeated: bool = False) -> "str | None":
    """The destructive git op these tokens run, or None. `repeated`: the
    segment may run more than once (a loop, xargs)."""
    hits: list[str] = []
    sub, args = _git_subcommand(toks)
    if sub == "reset" and "--hard" in args:
        hits.append("git reset --hard")
    elif sub == "clean" and (
        "--force" in args  # long form (advisor Finding 2)
        or any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", a) for a in args)
    ):
        hits.append("git clean -f")
    elif sub == "checkout" and (
        "--" in args or "." in args or "-f" in args or "--force" in args
    ):
        hits.append("git checkout (discard)")
    elif sub == "switch" and (
        "-f" in args or "--force" in args or "--discard-changes" in args
    ):
        hits.append("git switch (discard)")
    elif sub == "restore":
        # `restore --staged X` (only) just unstages — safe. `--worktree`, or
        # no `--staged`, discards worktree changes.
        staged_only = "--staged" in args and "--worktree" not in args
        if not staged_only:
            hits.append("git restore")
    elif sub == "stash" and any(a in ("drop", "clear") for a in args):
        # #349: a drop that names its entry by a literal ref (`stash@{2}`, a
        # SHA) is a deliberate act on one entry, the "push -m tag, apply by
        # SHA, drop by ref" recipe. A bare drop takes whatever is on top, and a
        # ref the shell expands may be empty, so both stay gated, as does clear,
        # and so does a literal ref that a loop would drop again and again.
        at = next(k for k, a in enumerate(args) if a in ("drop", "clear"))
        refs = [a for a in args[at + 1:] if not a.startswith("-")]
        if args[at] == "clear" or not refs or repeated or not all(
                _LITERAL_STASH_REF.fullmatch(r) or _LITERAL_WORD.fullmatch(r)
                for r in refs):
            hits.append("git stash drop/clear")
    elif sub == "worktree" and args[:1] == ["remove"] and (
        "--force" in args[1:]
        or any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", a) for a in args[1:])
    ):
        # Without --force git refuses a dirty worktree itself; with it, the
        # worktree's uncommitted and untracked files are deleted.
        hits.append(WORKTREE_REMOVE)
    return hits[0] if hits else None


def find_force_pushes(cmd: str) -> list[str]:
    """Force-pushes that overwrite remote history without a lease (#206).

    Not gated on a dirty tree: the loss is on the REMOTE, and the local tree's
    state says nothing about it. `--force-with-lease` (what net.worktree
    restack uses) and `--force-if-includes` are the safe forms and pass; a
    bare `--force` still wins over a lease when both are given, so it is caught.
    A dry run (-n / --dry-run) pushes nothing and passes.
    """
    hits: list[str] = []
    for toks in _expanded_segments(cmd):
        sub, args = _git_subcommand(toks)
        if sub != "push":
            continue
        if "--dry-run" in args or any(
            re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", a) for a in args
        ):
            continue
        if "--mirror" in args:
            # Force-overwrites EVERY remote ref and deletes those missing
            # locally (a lease does not bound the deletions).
            hits.append("git push --mirror")
        elif "--force" in args or any(
            re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", a) for a in args
        ):
            hits.append("git push --force")
        elif any(a.startswith("+") for a in args):
            hits.append("git push +refspec")
        if "--prune" in args:
            # Deletes every remote branch with no local counterpart: a
            # base-branch deletion the branch-name check cannot see.
            hits.append("git push --prune")
    return hits


def _open_child_prs(branch: str, gh_runner: Runner) -> list[str] | None:
    """Open PR numbers based on `branch`, or None if gh can't answer."""
    try:
        cp = gh_runner(["gh", "pr", "list", "--base", branch, "--state", "open", "--json", "number"])
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return [str(d.get("number")) for d in data if isinstance(d, dict)]


def _git(where: "str | None", *args: str) -> list[str]:
    return ["git", "-C", where, *args] if where else ["git", *args]


def _derived_dir_dirty(git_runner: Runner, where: str, *status_args: str) -> bool:
    """Dirt check for a directory the guard DERIVED (event cwd, cd, -C). Any
    doubt counts as dirty: a missing directory, or a git error, never reads as
    clean. The one exception: an existing directory that is not a git repo,
    where the git op would itself fail."""
    if where == UNKNOWABLE_TREE or not os.path.isdir(where):
        return True
    try:
        cp = git_runner(_git(where, "status", *status_args))
    except (OSError, subprocess.SubprocessError):
        return True
    if getattr(cp, "returncode", 1) != 0:
        return "not a git repository" not in (cp.stderr or "").lower()
    return cp.stdout.strip() != ""


def _tree_is_clean(git_runner: Runner, where: "str | None" = None) -> bool:
    """True if the worktree is clean. Fail-open (True) if git can't answer."""
    try:
        cp = git_runner(_git(where, "status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return True
    if cp.returncode != 0:
        return True
    return cp.stdout.strip() == ""


def _has_tracked_changes(git_runner: Runner, where: "str | None" = None,
                         pathspec: "tuple[str, ...]" = (),
                         untracked: bool = False) -> bool:
    """True if the worktree has TRACKED modifications (staged or unstaged).

    Untracked and ignored files are excluded: `git reset --hard` and the
    discard-checkout/switch/restore family restore TRACKED files but do NOT touch
    untracked ones — so a reset whose only dirt is untracked (`.maw/`, in every
    real seat) destroys nothing and must not be blocked (#137, the same
    tracked-vs-untracked class as the #124 restack fix). Fail-open (False = no
    tracked changes) if git can't answer, matching _tree_is_clean's stance."""
    try:
        cp = git_runner(_git(where, "status", "--porcelain",
                             *(("--ignored",) if untracked else ("--untracked-files=no",)),
                             *pathspec))
    except (OSError, subprocess.SubprocessError):
        return False
    if cp.returncode != 0:
        return False
    return cp.stdout.strip() != ""


# Ops that discard TRACKED changes only (untracked files are left alone) — gated
# on tracked dirt. `git clean -f` (untracked) and `git stash drop/clear` (the
# stash) are NOT here: they keep the block-on-any-dirt behavior.
_TRACKED_DISCARD_OPS = frozenset((
    "git reset --hard", "git checkout (discard)",
    "git switch (discard)", "git restore",
))


_REVIEW_LEAF_RE = re.compile(r"at-[0-9a-f]{12}")

# A Claude Code session scratchpad: <tmp>/claude-<uid>/<project>/<session>/
# scratchpad. It is deleted with the session, so a review worktree a seat adds
# there is throwaway by construction (#349).
_SCRATCHPAD_RE = re.compile(r"claude-\d+/[^/]+/[0-9a-fA-F-]{8,}/scratchpad/.+")

# Roots whose direct children are builder worktrees: removable once the work
# is pushed, but NOT throwaway while it is being built, so they are exempt from
# the `git worktree remove --force` gate only.
BUILD_WORKTREE_ROOTS = ("~/.claude-builds",)


def _is_scratch_worktree_path(path: str, removable: bool = False) -> bool:
    """True if `path` (already resolved) is under a session scratchpad, or,
    with `removable`, a direct child of a BUILD_WORKTREE_ROOTS entry. A path
    test only: `_in_review_worktree` adds the linked-worktree proof for
    discards, and `git worktree remove` refuses anything that is not a
    linked worktree on its own."""
    real = os.path.realpath(path)
    for root in {os.path.realpath("/tmp"), os.path.realpath(tempfile.gettempdir())}:
        rel = os.path.relpath(real, root)
        if not rel.startswith("..") and _SCRATCHPAD_RE.fullmatch(rel):
            return True
    if removable:
        for root in BUILD_WORKTREE_ROOTS:
            root = os.path.realpath(os.path.expanduser(root))
            if os.path.dirname(real) == root and real != root:
                return True
    return False


def _in_review_worktree(git_runner: Runner, where: "str | None" = None,
                        scratch: bool = True) -> bool:
    """True if the git worktree is an ephemeral /maw:review worktree.

    #86: review.cli creates these as <tmpdir>/maw-review-XXXX/at-<sha12>; a
    `git checkout -- <path>` restore of intended churn there is routine and
    harmless. The shape is PROVEN, not guessed from a substring: the toplevel
    must be exactly that path under the system temp dir, and a linked worktree
    (its common git dir is elsewhere). Fail-CLOSED (False) on any doubt — an
    unrecognized tree keeps the guard.
    """
    try:
        top = git_runner(_git(where, "rev-parse", "--show-toplevel"))
        common = git_runner(_git(where, "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"))
    except (OSError, subprocess.SubprocessError):
        return False
    if getattr(top, "returncode", 1) != 0 or getattr(common, "returncode", 1) != 0:
        return False
    toplevel = os.path.normpath((top.stdout or "").strip())
    if not toplevel:
        return False
    leaf, parent = os.path.basename(toplevel), os.path.dirname(toplevel)
    tmp = os.path.realpath(tempfile.gettempdir())
    if not ((_REVIEW_LEAF_RE.fullmatch(leaf)
             and os.path.basename(parent).startswith("maw-review-")
             and os.path.dirname(parent) == tmp)
            or (scratch and _is_scratch_worktree_path(toplevel))):
        return False
    common_dir = os.path.normpath((common.stdout or "").strip())
    # a linked worktree's common dir is the main repo's .git, not its own
    if not common_dir or common_dir.startswith(toplevel + os.sep):
        return False
    # ...and so is a SUBMODULE's (under the superproject's .git/modules): it
    # must not have a superproject.
    try:
        sup = git_runner(_git(where, "rev-parse", "--show-superproject-working-tree"))
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(sup, "returncode", 1) == 0 and not (sup.stdout or "").strip()


def _op_blocks(op: "_Op", git_runner: Runner) -> bool:
    """Would this one op destroy uncommitted work? Any doubt blocks."""
    where = op.where
    if where == UNKNOWABLE_TREE:
        return True
    if op.desc == WORKTREE_REMOVE:
        if not os.path.isdir(where):
            return False  # nothing on disk to lose (git prunes the entry)
        if _is_scratch_worktree_path(where, removable=True):
            return False
        # remove --force deletes untracked files too: any dirt counts.
        return _derived_dir_dirty(git_runner, where, "--porcelain")
    if where is not None and not os.path.isdir(where):
        return True
    # The stash stack is shared by every worktree of the repo, so a stash op in
    # a scratch worktree is not throwaway: only the /maw:review exemption applies.
    if _in_review_worktree(git_runner, where, scratch=op.desc != STASH_OP):
        return False
    # Gate each op on the dirt it would ACTUALLY destroy: the tracked-discard
    # family (reset --hard, checkout/switch/restore) only on TRACKED changes,
    # so untracked-only dirt (`.maw/`) does not false-block a safe reset
    # (#137), and a path-scoped discard only on the paths it names (#349);
    # clean/stash still block on any dirt.
    pathspec = ("--", *op.paths) if op.paths else ()
    if op.desc in _TRACKED_DISCARD_OPS:
        # A restore of named paths from a revision replaces an untracked or
        # an IGNORED file of that name too (a once-committed .env), so both
        # count in those paths (#349 review rounds 4 and 5).
        untracked = ("--ignored",) if op.sourced else ("--untracked-files=no",)
        if where is None:  # the hook's own cwd: the legacy fail-open check
            return _has_tracked_changes(git_runner, None, pathspec,
                                        untracked=op.sourced)
        return _derived_dir_dirty(git_runner, where, "--porcelain",
                                  *untracked, *pathspec)
    if where is None:
        return not _tree_is_clean(git_runner)
    return _derived_dir_dirty(git_runner, where, "--porcelain")


def _dirty_block_message(blocked: "list[_Op]") -> str:
    """#349: name the segment(s) that matched, and say plainly that nothing in
    the command ran: a compound is refused whole, so its safe parts did not
    run either, and the old message read as if only the op was refused."""
    descs = ", ".join(sorted({op.desc for op in blocked}))
    segs = []
    for op in blocked:
        where = ("an unknowable directory" if op.where == UNKNOWABLE_TREE
                 else op.where or "the current directory")
        seg = op.seg if len(op.seg) <= 160 else op.seg[:157] + "..."
        line = f"  - `{seg}` ({op.desc}, in {where})"
        if line not in segs:
            segs.append(line)
    hints = []
    if any(op.desc == "git stash drop/clear" for op in blocked):
        hints.append("To drop one stash entry, name it: `git stash drop stash@{N}` "
                     "(a literal ref is not gated).")
    if any(op.desc in _PATH_SCOPED_OPS and op.paths is None for op in blocked):
        hints.append("A discard limited to named paths (`git checkout -- <path>`) "
                     "is only gated on dirt in those paths.")
    if any(op.desc == WORKTREE_REMOVE for op in blocked):
        hints.append("Removing a scratch review worktree (session scratchpad, "
                     "~/.claude-builds) is not gated.")
    return "\n".join([
        f"destructive-guard: BLOCKED — {descs} against a DIRTY tree would "
        f"discard uncommitted work. Blocked segment(s):",
        *segs,
        "No part of this command ran, including any safe segments before or after "
        "the blocked one; run those as a separate call.",
        *hints,
        "To proceed anyway, prepend MAW_ALLOW_DESTRUCTIVE=1 to THIS command (read "
        "from the command itself, not just the session env). To recover a single "
        "file without discarding the rest, no override needed: "
        "`git show HEAD:<path> > <path>`. Commit or stash first if the work should "
        "be kept.",
    ])


_VISIBLE_DESTRUCTIVE = re.compile(
    r"\bgit\b.*?\b(reset\s+(\S+\s+)*--hard|clean\b|checkout\b|switch\b|restore\b|"
    r"stash\s+(drop|clear)|branch\s+(\S+\s+)*(-[a-zA-Z]*[dD]|--delete)|push\b|"
    r"worktree\s+remove\b)")


def looks_destructive(cmd: object) -> bool:
    """Crude text check used only when evaluation itself failed: does the
    command visibly mention a git op the guard gates?"""
    return isinstance(cmd, str) and bool(_VISIBLE_DESTRUCTIVE.search(cmd))


def evaluate(
    event: dict,
    *,
    env: dict,
    git_runner: Runner = _git_run,
    gh_runner: Runner = _gh_run,
) -> Decision:
    """Classify a PreToolUse(Bash) event → allow (0) or block (2)."""
    if event.get("tool_name") != "Bash":
        return Decision(ALLOW, "")
    cmd = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd.strip():
        return Decision(ALLOW, "")

    warnings: list[str] = []

    # Overrides are honored from the SESSION ENV or the COMMAND STRING itself
    # (#86 — the inline/exported form is what agents actually type).
    base_override = (
        env.get("MAW_ALLOW_BASE_DELETE") == "1"
        or _command_sets(cmd, "MAW_ALLOW_BASE_DELETE")
    )
    destructive_override = (
        env.get("MAW_ALLOW_DESTRUCTIVE") == "1"
        or _command_sets(cmd, "MAW_ALLOW_DESTRUCTIVE")
    )

    # 0. nesting no check can read: every check below would pass it blind.
    if nests_too_deep(cmd) and not destructive_override:
        return Decision(
            BLOCK,
            f"destructive-guard: BLOCKED — shell payloads or command "
            f"substitutions nested deeper than {MAX_PAYLOAD_DEPTH} levels, so the "
            f"guard cannot see what they run. Flatten the command, or prepend "
            f"MAW_ALLOW_DESTRUCTIVE=1 to THIS command if you mean it.",
        )

    # 1. base-branch deletion
    deletions = find_branch_deletions(cmd)
    for b in deletions:
        if b in PROTECTED_BASES and not base_override:
            return Decision(
                BLOCK,
                f"destructive-guard: BLOCKED — deleting protected base branch "
                f"{b!r}. An agent should never delete an integration base. If you "
                f"truly mean it, re-run with MAW_ALLOW_BASE_DELETE=1.",
            )
    if deletions and not base_override:
        for b in deletions:
            prs = _open_child_prs(b, gh_runner)
            if prs is None:
                warnings.append(
                    f"destructive-guard: could not verify open PRs against {b!r} "
                    f"(gh unavailable) — VERIFY MANUALLY before deleting a base."
                )
                continue
            if prs:
                return Decision(
                    BLOCK,
                    f"destructive-guard: BLOCKED — branch {b!r} is the base of "
                    f"{len(prs)} OPEN PR(s) (#{', #'.join(prs)}). Deleting it "
                    f"orphans them. Retarget those PRs first, or override with "
                    f"MAW_ALLOW_BASE_DELETE=1.",
                )

    # 2. destructive-on-dirty-tree, checked in the repo each op acts on (the
    # event's cwd, a preceding `cd`, and git -C), not the hook's own cwd.
    event_cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    ops = [] if destructive_override else _located_ops(cmd, event_cwd)
    blocked: list[_Op] = [op for op in ops if _op_blocks(op, git_runner)]
    if blocked:
        return Decision(BLOCK, _dirty_block_message(blocked))

    # 3. force-push without a lease — remote loss, independent of the tree.
    forced = find_force_pushes(cmd)
    if forced and not destructive_override:
        return Decision(
            BLOCK,
            f"destructive-guard: BLOCKED — {', '.join(sorted(set(forced)))} "
            f"overwrites or deletes remote refs with no lease, so it can silently "
            f"drop commits or branches someone else pushed. For a force push use "
            f"`git push --force-with-lease` (what net.worktree restack does); for "
            f"--mirror or --prune, push the branches you mean by name. If you truly "
            f"mean it, prepend MAW_ALLOW_DESTRUCTIVE=1 to THIS command.",
        )

    return Decision(ALLOW, "\n".join(warnings))
