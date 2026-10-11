"""Review-workflow helpers (backlog item 6).

Per-PR assembly was heavy both ways (~13 builder pings, ~9 reviewer
worktree-add→suite→remove cycles). These commands cut that:

  review-request  — auto-fill SHA + branch + diff-stat + the risk tier/gate,
                    and prompt only for the ONE part a human must state: the
                    adversarial targets (the best forcing function — both seats).
  review <ref>    — create a clean ephemeral worktree at a PR/branch HEAD, run
                    the suite there, print the diff-stat + risk tier, auto-clean.
                    The reviewer's clean-worktree ritual as one command.
  risk <paths…>   — classify paths (or the working diff) into a review tier.

Stdlib-only; git/gh shelled through an injectable runner so the logic is
testable offline.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from review import freshness
from review.risk import Gate, classify_paths, gate_for_paths, recommend

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ReviewError(Exception):
    pass


def _run(cmd: list[str], cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=cwd)


def _git(args: list[str], runner: Runner, cwd: str | None = None) -> str:
    cp = runner(["git", *args], cwd)
    if cp.returncode != 0:
        raise ReviewError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


def changed_files(base: str, runner: Runner, cwd: str | None = None) -> list[str]:
    out = _git(["diff", "--name-only", f"{base}...HEAD"], runner, cwd)
    return [ln for ln in out.splitlines() if ln.strip()]


def resolve_ref_sha(ref: str, runner: Runner, cwd: str | None = None) -> str:
    """SHA for a PR number (via gh) or a git ref."""
    if ref.isdigit():
        cp = runner(["gh", "pr", "view", ref, "--json", "headRefOid", "-q", ".headRefOid"], cwd)
        if cp.returncode != 0 or not cp.stdout.strip():
            raise ReviewError(f"gh pr view {ref} failed: {cp.stderr.strip()}")
        return cp.stdout.strip()
    return _git(["rev-parse", ref], runner, cwd)


DEFAULT_SUITE = "python3 tests/run_all.py"


def suite_argv(cmd: str) -> list[str]:
    """Run --suite-cmd through bash with pipefail (Lane D row 16). The string was
    `.split()` into argv with no shell: a quoted command was mangled, and a
    pipeline wrapped in `sh -c` reported the LAST stage's exit, so a failing
    suite piped into `tail` read as `exit 0`. Now the command runs exactly as
    typed, and any failing stage fails the review."""
    return ["bash", "-o", "pipefail", "-c", cmd]


# The operator's rule: never mention AI in commit messages. Seats' commits keep arriving
# with harness-injected attribution, so the review path blocks on it. A bare
# "Claude Code" in prose is NOT flagged (maw's own history legitimately names
# the product); only attribution shapes are.
_AI_TOOLS = r"(?:claude|anthropic|chatgpt|openai|copilot|cursor|gemini|codex)"
_AI_TOOL_WORD = re.compile(rf"\b{_AI_TOOLS}\b", re.I)
# A noreply-style address at an AI vendor's own domain (not look-alikes such
# as openai-fans.org: the domain must END at the vendor's registered name).
_AI_VENDOR_ADDR = re.compile(
    r"<[^<>]*@(?:[\w-]+\.)*(?:anthropic\.com|openai\.com|cursor\.(?:com|sh))>", re.I)
_ROBOT = "\U0001F916"
# git accepts whitespace before the trailer separator ("Co-authored-by : x").
_TRAILER = re.compile(r"^\s*[\w-]+-by\s*:\s*(?P<val>.*)$", re.I)
_GENERATED = re.compile(r"^\s*(?:\U0001F916\s*)?generated (?:with|by)\b", re.I)

# The regexes below only ever see this many characters of a line. Two of them
# are quadratic on adversarial input (a 400 KB line took 39 s, in-process and
# outside the suite runner's timeout). Text past the cap is checked with
# linear substring tests instead (_OVERLONG_MARKERS), so a long line cannot be
# used to hide a marker either.
MAX_LINE = 1000
_OVERLONG_MARKERS = (
    "claude.ai/code/session", "claude-session:", "[claude code](",
    "@anthropic.com>", "@openai.com>", "@cursor.com>", "@cursor.sh>",
)


def _is_ai_trailer(line: str) -> bool:
    """Any `<Something>-by:` trailer (Assisted-by, Co-developed-by,
    Signed-off-by, Generated-by, Co-authored-by...) naming an AI tool as a
    word in the NAME part, or carrying an AI vendor's noreply address. Human
    names ("Ai Nguyen") and look-alike domains (openai-fans.org) don't match."""
    m = _TRAILER.match(line)
    if not m:
        return False
    val = m.group("val")
    name = val.split("<", 1)[0]
    return bool(_AI_TOOL_WORD.search(name) or _AI_VENDOR_ADDR.search(val))


def _is_generated_by_ai(line: str) -> bool:
    return bool(_GENERATED.match(line)) and (_ROBOT in line or bool(_AI_TOOL_WORD.search(line)))


_SESSION = re.compile(r"^\s*claude-session\s*:|claude\.ai/code/session", re.I)
_CC_LINK = re.compile(r"\[claude code\]\(|\bgenerated\b.*\bclaude code\b", re.I)

AI_ATTRIBUTION_PATTERNS: "list[tuple[str, Callable[[str], bool]]]" = [
    ("AI attribution trailer (…-by:)", _is_ai_trailer),
    ("'Generated with' line naming an AI tool", _is_generated_by_ai),
    ("Claude session trailer/link", lambda l: bool(_SESSION.search(l))),
    ("Claude Code attribution link", lambda l: bool(_CC_LINK.search(l))),
]
_OVERLONG_RULE = f"AI attribution marker in a line over {MAX_LINE} chars"


def attribution_rule(line: str) -> "str | None":
    """The rule a (logical) commit-message line trips, or None. Regexes see at
    most MAX_LINE characters; the remainder of an over-long line gets linear
    checks only (and is held to a cruder, stricter standard: any vendor
    address, session link, or a `…-by:` line naming an AI tool anywhere)."""
    line = line.strip()
    head = line[:MAX_LINE]
    for rule, pat in AI_ATTRIBUTION_PATTERNS:
        if pat(head):
            return rule
    if len(line) > MAX_LINE:
        low = line.lower()
        if any(tok in low for tok in _OVERLONG_MARKERS):
            return _OVERLONG_RULE
        if _TRAILER.match(head) and _AI_TOOL_WORD.search(line):
            return _OVERLONG_RULE
    return None


def _logical_lines(body: str) -> "list[str]":
    """Commit-message lines with FOLDED trailers unfolded. git continues a
    trailer value on following lines that start with whitespace, so
    `Co-authored-by:\\n Claude <…>` is one trailer; scanned line by line the
    key and the name never meet. A folded trailer is returned as one line; its
    continuation lines are ALSO kept as lines of their own, so nothing a
    per-line scan would have caught is lost by folding."""
    out: list[str] = []
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if not _TRAILER.match(ln[:MAX_LINE]):
            out.append(ln)
            continue
        parts = [ln.strip()]
        while i < len(lines) and lines[i][:1] in (" ", "\t") and lines[i].strip():
            parts.append(lines[i].strip())
            out.append(lines[i])
            i += 1
        out.append(" ".join(parts))
    return out


def resolve_base(base: str, runner: Runner, cwd: str | None = None) -> str:
    """The FULL ref path for a branch-name base: refs/remotes/origin/<base>
    when it exists (a stale LOCAL <base> would sweep already-merged commits
    into the range), else refs/heads/<base>.

    Never the short name. git resolves a short name through refs/tags/ BEFORE
    refs/heads/ and refs/remotes/, and a plain fetch auto-follows tags, so
    anyone with push access can push a tag named `origin/main` (or `main`) at
    their PR head: `origin/main..head` is then empty and the scan reads clean.
    A full `refs/…` path is matched exactly and cannot be shadowed. Anything
    that is not a branch name (a SHA, `HEAD~2`) is returned unchanged."""
    if not base or base.startswith("refs/"):
        return base
    short = base[len("origin/"):] if base.startswith("origin/") else base
    candidates = [f"refs/remotes/origin/{short}"]
    if short == base:
        candidates.append(f"refs/heads/{base}")
    for ref in candidates:
        cp = runner(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
        if cp.returncode == 0 and cp.stdout.strip():
            return ref
    return base


def _allowed(sha: str, allow: "list[str] | tuple[str, ...]") -> bool:
    return any(len(a) >= 7 and sha.startswith(a.lower()) for a in allow)


def ai_attribution_findings(
    base: str, head: str, runner: Runner, cwd: str | None = None,
    allow: "list[str] | tuple[str, ...]" = (),
) -> "list[tuple[str, str, str]]":
    """(sha, rule, offending line) for every AI-attribution line in the commit
    messages of base..head. Empty list = clean.

    `allow` is the reviewer's override for a false positive (a human co-author
    actually named Claude, prose quoting the footer): commit SHAs (>= 7 hex
    chars) whose findings are dropped. It is a CLI flag on the reviewer's side,
    never anything the commit itself can carry."""
    base = resolve_base(base, runner, cwd)
    # NUL-delimited (-z, %x00): a commit message cannot contain NUL, so no
    # message content can forge or swallow a record boundary (a 0x1e byte in a
    # message used to hide everything after it).
    out = _git(["log", "-z", "--format=%H%x00%B", f"{base}..{head}"], runner, cwd)
    fields = out.split("\x00")
    found: list[tuple[str, str, str]] = []
    for sha, body in zip(fields[0::2], fields[1::2]):
        sha = sha.strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ReviewError(f"unparseable git log record (expected a commit id, got {sha[:20]!r})")
        if _allowed(sha, allow):
            continue
        seen: set[str] = set()
        for line in _logical_lines(body):
            rule = attribution_rule(line)
            shown = line.strip()[:200]
            if rule and shown not in seen:
                seen.add(shown)
                found.append((sha, rule, shown))
    return found


def format_attribution_block(findings: "list[tuple[str, str, str]]") -> str:
    lines = ["BLOCKING: AI attribution in commit messages (rule: never mention AI in commits)"]
    for sha, rule, line in findings:
        lines.append(f"  {sha[:12]}  {rule}: {line}")
    lines.append("  -> reword the offending commits (strip the trailers) before merge")
    lines.append("  -> a false positive (a person really named Claude, prose quoting the footer)? "
                 "the REVIEWER reruns with --allow-attribution <sha>")
    return "\n".join(lines)


def format_allowed(allow: "list[str] | tuple[str, ...]") -> str:
    return ("commit-message check: findings in " + ", ".join(allow)
            + " ALLOWED by --allow-attribution (reviewer override)")


ATTRIBUTION_EXIT = 3


DEFAULT_LINKS = (".venv", "venv", "node_modules")


class ConfigRefused(ReviewError):
    """The committed review config uses a shape this parser does not read.
    Refused out loud: guessing would run a truncated or different command."""


def _ref_sha(ref: str, runner: Runner, cwd: str | None) -> str:
    cp = runner(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def default_branch_ref(runner: Runner, cwd: str | None = None) -> "tuple[str | None, str]":
    """(FULL ref of the repo's default branch, "") or (None, reason).

    This is the only ref review config is ever read from. It is taken from
    `refs/remotes/origin/HEAD` (set by clone / `git remote set-head`, never by
    a push), and used only as a full `refs/remotes/origin/…` path, which no
    tag or branch name can shadow. With an `origin` remote but no origin/HEAD
    the default is UNKNOWN: guessing `main` would let a pushed branch of that
    name supply the config. Only a repo with no `origin` at all falls back to
    its local main/master, which nobody can push into."""
    cp = runner(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd)
    target = cp.stdout.strip() if cp.returncode == 0 else ""
    if target.startswith("refs/remotes/origin/") and target != "refs/remotes/origin/HEAD":
        if _ref_sha(target, runner, cwd):
            return target, ""
        return None, f"origin/HEAD points at {target}, which does not exist (run `git fetch origin`)"
    if runner(["git", "config", "--get", "remote.origin.url"], cwd).returncode == 0:
        return None, ("origin/HEAD is not set, so the default branch is unknown "
                      "(run `git remote set-head origin -a`)")
    for cand in ("main", "master"):
        if _ref_sha(f"refs/heads/{cand}", runner, cwd):
            return f"refs/heads/{cand}", ""
    return None, "no origin remote and no local main/master branch"


CONFIG_PATH = ".maw/config.yaml"


def load_default_config(repo_root: str | None, runner: Runner = _run) -> "tuple[dict, str]":
    """Review config as committed on the DEFAULT BRANCH only (see
    default_branch_ref), whatever `--base` is. `--base` names what the PR is
    diffed against; on a stacked review that is itself an unreviewed branch,
    so it must not choose the command the reviewer runs. The working tree and
    the PR head are never read.

    Returns (config, source). No config, an unknown default branch, or any
    failure reading it yields ({}, "default (<reason>)") and the caller uses
    the built-in defaults. A config that IS there but is written in a shape
    the parser does not read raises ConfigRefused."""
    if not repo_root:
        return {}, "default (not in a git repository, failing closed)"
    try:
        ref, why = default_branch_ref(runner, repo_root)
        if ref is None:
            return {}, f"default (config not read: {why})"
        cp = runner(["git", "show", f"{ref}:{CONFIG_PATH}"], repo_root)
        if cp.returncode == 0:
            try:
                return parse_review_config(cp.stdout), f"{ref}:{CONFIG_PATH}"
            except ConfigRefused as e:
                raise ConfigRefused(f"{ref}:{CONFIG_PATH} refused: {e}") from None
        # show failed: absent on the ref, or unreadable? ls-tree tells them apart.
        lt = runner(["git", "ls-tree", "--name-only", ref, "--", CONFIG_PATH], repo_root)
        if lt.returncode == 0 and not lt.stdout.strip():
            return {}, f"default (no config on {ref})"
        return {}, f"default (config read failed on {ref}, failing closed)"
    except ConfigRefused:
        raise
    except Exception as e:  # fail closed: never fall back to the working tree
        return {}, f"default (config read failed: {type(e).__name__}, failing closed)"


def base_is_default(base: str, runner: Runner, cwd: str | None = None) -> bool:
    """True when --base is the default branch (same commit as the config ref)."""
    ref, _why = default_branch_ref(runner, cwd)
    if ref is None or not base:
        return False
    resolved = resolve_base(base, runner, cwd)
    return resolved == ref or (_ref_sha(resolved, runner, cwd) or "?") == _ref_sha(ref, runner, cwd)


_CFG_KEY = re.compile(r"^(?P<key>[A-Za-z_][\w.-]*)[ \t]*:(?:[ \t]+(?P<val>.*))?$")
_CFG_QUOTED_KEY = re.compile(
    r"""^['"](?:review|stack|suite_cmd|link|test_command)['"][ \t]*:""")
_CFG_WANTED = {"review": ("suite_cmd", "link"), "stack": ("test_command",)}


def _refuse(n: int, why: str) -> "ConfigRefused":
    return ConfigRefused(f"line {n}: {why}")


def _after_value(rest: str, n: int, name: str) -> None:
    """Only whitespace or a comment may follow a quoted value."""
    if rest.strip() and not re.match(r"^\s+#", rest):
        raise _refuse(n, f"{name}: unexpected text after the closing quote ({rest.strip()[:30]!r})")


def _quoted(v: str, n: int, name: str) -> "tuple[str, str]":
    """(value, remainder after the closing quote) of a quoted scalar at v[0]."""
    q, out, i = v[0], [], 1
    while i < len(v):
        c = v[i]
        if q == '"' and c == "\\":
            nxt = v[i + 1:i + 2]
            if nxt not in ('"', "\\"):
                raise _refuse(n, f'{name}: unsupported escape \\{nxt} in a double-quoted value '
                                 f"(only \\\" and \\\\ are read; use single quotes)")
            out.append(nxt)
            i += 2
        elif c == q:
            if q == "'" and v[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), v[i + 1:]
        else:
            out.append(c)
            i += 1
    raise _refuse(n, f"{name}: unterminated quote (multi-line values are not supported)")


def _scalar(val: "str | None", n: int, name: str) -> "str | None":
    """One-line YAML scalar, or None for an empty value. Refuses every shape
    it would otherwise have to guess at."""
    v = (val or "").strip()
    if not v or v.startswith("#"):
        return None
    if v[0] in "'\"":
        got, rest = _quoted(v, n, name)
        _after_value(rest, n, name)
        return got
    if v[0] in "|>":
        raise _refuse(n, f"{name}: block scalars (| and >) are not supported; "
                         f"write the command on one line, quoted")
    if v[0] in "[{&*!%@`" or v[:2] in ("- ", "? "):
        raise _refuse(n, f"{name}: unsupported value {v[:30]!r}; write a one-line string")
    m = re.search(r"\s#", v)
    if m:
        kept = v[:m.start()].rstrip()
        if kept.count('"') % 2 or kept.count("'") % 2:
            raise _refuse(n, f"{name}: a ' #' inside an unquoted value would cut the command "
                             f"short; wrap the whole value in quotes")
        v = kept
    if ": " in v or v.endswith(":"):
        raise _refuse(n, f"{name}: an unquoted value may not contain ': '; wrap it in quotes")
    return v


def _flow_list(val: "str | None", n: int, name: str) -> "list[str] | None":
    v = (val or "").strip()
    if not v or v.startswith("#"):
        return None
    if v[0] != "[":
        raise _refuse(n, f"{name}: must be a one-line list, e.g. [\".venv\", \"node_modules\"]")
    items: list[str] = []
    rest = v[1:].lstrip()
    if rest.startswith("]"):
        _after_value(" " + rest[1:] if rest[1:].strip() else "", n, name)
        return items
    while True:
        if not rest:
            raise _refuse(n, f"{name}: list is not closed on this line "
                             f"(multi-line lists are not supported)")
        if rest[0] in "'\"":
            item, rest = _quoted(rest, n, name)
        else:
            m = re.match(r"[^,\[\]{}'\"#]*", rest)
            item, rest = m.group(0).strip(), rest[m.end():]  # type: ignore[union-attr]
        if not item:
            raise _refuse(n, f"{name}: empty or unreadable list item near {rest[:20]!r}")
        items.append(item)
        rest = rest.lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
            continue
        if rest.startswith("]"):
            tail = rest[1:]
            if tail.strip() and not re.match(r"^\s+#", tail):
                raise _refuse(n, f"{name}: unexpected text after the list ({tail.strip()[:30]!r})")
            return items
        raise _refuse(n, f"{name}: expected ',' or ']' near {rest[:20]!r}")


def parse_review_config(text: str) -> dict:
    """Parse config text (see load_default_config). The `review:` block:

        review:
          suite_cmd: "npx vitest run"
          link: [".venv", "node_modules"]

    A top-level `suite_cmd:` is accepted as shorthand (`review.suite_cmd` wins).
    Also reads `stack.test_command` as `test_command`, the fallback when
    `review.suite_cmd` is absent.

    This is a deliberately small reader, not a YAML parser. For the three keys
    it reads, every shape it does not understand raises ConfigRefused with the
    line number: block scalars, values continued on a following line, text
    after a closing quote, block-style lists, inline `review: {…}` mappings,
    quoted keys, duplicate keys, tabs. It never returns a shortened command."""
    cfg: dict = {}
    seen: set[str] = set()
    top = ""
    child_indent: "int | None" = None
    pending: "tuple[int, str] | None" = None  # (indent, name) of the last key we read
    for n, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body = raw.lstrip(" ")
        indent = len(raw) - len(body)
        if pending is not None:
            p_indent, p_name = pending
            if indent > p_indent or (indent == p_indent and body.startswith("- ")):
                raise _refuse(n, f"{p_name}: the value continues on this line; block scalars, "
                                 f"multi-line values and block-style lists are not supported "
                                 f"(write it on one line, lists as [a, b])")
            pending = None
        relevant = indent == 0 or top in _CFG_WANTED
        if body.startswith("\t"):
            if relevant:
                raise _refuse(n, "tab in indentation")
            continue
        if relevant and _CFG_QUOTED_KEY.match(body):
            raise _refuse(n, "quoted keys are not supported; write the key bare")
        if indent == 0:
            if body.startswith("{"):
                raise _refuse(n, "a flow-style ({…}) document is not supported")
            m = _CFG_KEY.match(body)
            top, child_indent = (m.group("key") if m else ""), None
            if not m:
                continue
            if top in _CFG_WANTED:
                if top in seen:
                    raise _refuse(n, f"duplicate `{top}:` block")
                seen.add(top)
                if _scalar_is_present(m.group("val")):
                    raise _refuse(n, f"`{top}:` must be a block mapping with its keys indented "
                                     f"on the following lines")
            elif top == "suite_cmd":
                if "suite_cmd" in seen:
                    raise _refuse(n, "duplicate top-level `suite_cmd:`")
                seen.add("suite_cmd")
                v = _scalar(m.group("val"), n, "suite_cmd")
                if v and "review.suite_cmd" not in seen:
                    cfg["suite_cmd"] = v
                pending = (0, "suite_cmd")
            continue
        if top not in _CFG_WANTED:
            continue
        if child_indent is None:
            child_indent = indent
        if indent > child_indent:
            continue  # nested under a key this reader does not own
        if indent < child_indent:
            raise _refuse(n, f"inconsistent indentation inside `{top}:`")
        if body.startswith("<<"):
            raise _refuse(n, f"merge keys (<<) are not supported inside `{top}:`")
        m = _CFG_KEY.match(body)
        if not m or m.group("key") not in _CFG_WANTED[top]:
            continue
        key = m.group("key")
        name = f"{top}.{key}"
        if name in seen:
            raise _refuse(n, f"duplicate `{name}`")
        seen.add(name)
        if key == "link":
            lst = _flow_list(m.group("val"), n, name)
            if lst is not None:
                cfg["link"] = lst
        else:
            v = _scalar(m.group("val"), n, name)
            if v:
                cfg[key] = v
        pending = (indent, name)
    return cfg


def _scalar_is_present(val: "str | None") -> bool:
    v = (val or "").strip()
    return bool(v) and not v.startswith("#")


def link_env_dirs(src_root: str, wt: str, names, out=sys.stdout) -> list[str]:
    """Symlink each existing `<src_root>/<name>` into the review worktree so the
    suite can run there (a fresh worktree lacks untracked .venv/node_modules).
    Skips names that are absolute/escaping, missing in the source, or already
    present in the worktree (tracked). Returns the names linked.

    By design the link target is the MAIN checkout's real directory, not a
    copy: the suite under review runs PR code and can write into it."""
    linked = []
    for name in names:
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            print(f"review: refusing to link {name!r} (must stay inside the repo)", file=out)
            continue
        src, dst = Path(src_root) / rel, Path(wt) / rel
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        anc = dst.parent
        while not anc.exists() and anc != anc.parent:
            anc = anc.parent
        try:
            anc.resolve().relative_to(Path(wt).resolve())
        except ValueError:
            print(f"review: refusing to link {name!r} (parent resolves outside the worktree)", file=out)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"review: refusing to link {name!r} ({type(e).__name__}: {e.strerror or e})", file=out)
            continue
        if not dst.parent.resolve().is_relative_to(Path(wt).resolve()):
            print(f"review: refusing to link {name!r} (parent resolves outside the worktree)", file=out)
            continue
        try:
            dst.symlink_to(src.resolve())
        except OSError as e:
            print(f"review: refusing to link {name!r} ({type(e).__name__}: {e.strerror or e})", file=out)
            continue
        linked.append(name)
    if linked:
        print(f"review: linked into worktree: {', '.join(linked)}", file=out)
    return linked


def _gate_block(gate: Gate, suite_label: str = DEFAULT_SUITE) -> str:
    flags = []
    flags.append(f"suite: {'RUN ' + suite_label if gate.suite else 'skip'}")
    flags.append(f"adversarial: {gate.adversarial}")
    flags.append(f"clean worktree at SHA: {'yes' if gate.worktree else 'no'}")
    if gate.harness:
        flags.append("state-forcing harness + build-from-SHA provenance (items 2/3)")
    return f"RISK TIER: {gate.tier}\n  " + "\n  ".join(flags) + f"\n  → {gate.summary}"


def assemble_request(
    base: str,
    runner: Runner = _run,
    cwd: str | None = None,
    suite_result: str | None = None,
    fresh: "freshness.Freshness | None" = None,
    allowed_reverts: "list[freshness.Revert] | None" = None,
) -> str:
    sha = _git(["rev-parse", "HEAD"], runner, cwd)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], runner, cwd)
    # `base` is printed; git is given the full ref, so a tag named like the
    # base cannot decide the diff-stat or the risk tier.
    git_base = resolve_base(base, runner, cwd)
    files = changed_files(git_base, runner, cwd)
    diffstat = _git(["diff", "--stat", f"{git_base}...HEAD"], runner, cwd)
    gate = gate_for_paths(files)
    suite_line = suite_result or (
        "<RUN `python3 tests/run_all.py` AND PASTE THE RESULT>"
        if gate.suite else "n/a (docs/trivial — suite skipped per risk gear)"
    )
    fresh_line = fresh.line() if fresh is not None else "unchecked"
    reverts = "".join(
        f"REVERTS: {r.describe()} (intentional: --allow-revert)\n" for r in (allowed_reverts or [])
    )
    return (
        f"REVIEW REQUEST\n"
        f"branch: {branch}\n"
        f"SHA: {sha}\n"
        f"base: {base}\n"
        f"freshness: {fresh_line}\n"
        f"{reverts}"
        f"suite: {suite_line}\n"
        f"{_gate_block(gate)}\n"
        f"diff-stat:\n{diffstat}\n"
        f"\nADVERSARIAL TARGETS (REQUIRED — only you can state these; name the\n"
        f"specific risks: injection surface, races, reject paths, cross-package\n"
        f"imports, fail-open holes):\n"
        f"  - <TARGET 1>\n  - <TARGET 2>\n"
    )


_TARGETS_HEAD = re.compile(r"adversarial\s+targets?\b[^:\n]*:?(.*)$", re.I)
_ITEM = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.*\S)\s*$")
_EMPTY_WORDS = {"none", "n/a", "na", "tbd", "todo", "nothing", "-", "?"}


def _substantive(item: str) -> bool:
    item = item.strip()
    if not item or re.fullmatch(r"<[^>]*>", item) or item.lower().startswith("<target"):
        return False
    if item.lower().rstrip(".") in _EMPTY_WORDS:
        return False
    return len(re.findall(r"[A-Za-z0-9_./#:-]+", item)) >= 3


def lint_request(text: str) -> "tuple[int, list[str]]":
    """(number of substantive adversarial targets, problems).

    C3 (2026-09-22): a review request that names nothing to attack is refused
    in code. It hides the author's risk model and gives the reviewer no floor
    to exceed. A target is a list item (or the text after the heading's colon)
    of at least three words that is not a placeholder, "none", or "TBD".
    """
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines) if _TARGETS_HEAD.search(ln)), None)
    if head is None:
        return 0, ["no ADVERSARIAL TARGETS section: name what the reviewer should attack"]
    items: list[str] = []
    inline = _TARGETS_HEAD.search(lines[head]).group(1).strip()  # type: ignore[union-attr]
    if inline:
        items.append(inline.lstrip(":").strip())
    for ln in lines[head + 1:]:
        m = _ITEM.match(ln)
        if m:
            items.append(m.group(1))
        elif ln.strip() and not ln[:1].isspace():
            break
    n = sum(1 for it in items if _substantive(it))
    if n == 0:
        return 0, ["the ADVERSARIAL TARGETS section names no target (empty, placeholders, "
                   "or 'none'): name the specific risks: injection surface, races, reject "
                   "paths, fail-open holes"]
    return n, []


def review_pr(
    ref: str,
    base: str,
    *,
    suite_cmd: list[str],
    runner: Runner = _run,
    cwd: str | None = None,
    out=None,
    allow_attribution: "list[str] | tuple[str, ...]" = (),
    link: "list[str] | tuple[str, ...]" = (),
    link_src: str | None = None,
    suite_source: str | None = None,
) -> int:
    """Clean ephemeral worktree at a PR/branch HEAD; run the suite; auto-clean.

    Returns the suite's exit code (or 1 on setup failure). The worktree is
    removed in a finally, so a failed suite never leaves a stale tree (the
    reviewer's clean-worktree ritual, automated).
    """
    out = out or sys.stdout
    sha = resolve_ref_sha(ref, runner, cwd)
    attribution: list[tuple[str, str, str]] = []
    if base:
        try:
            attribution = ai_attribution_findings(base, sha, runner, cwd, allow_attribution)
            if allow_attribution:
                print(format_allowed(allow_attribution), file=out)
        except ReviewError as e:
            print(f"review: WARNING: commit-message check skipped ({e})", file=out)
    # TemporaryDirectory (not bare mkdtemp): the PARENT dir is removed on EVERY
    # exit — success, suite failure, AND a worktree-add failure (which happens
    # inside the `with`, so no stranded temp dir). git worktree remove (finally)
    # runs first to clear git's metadata, then the parent dir is rmtree'd
    # (advisor Note 1: mkdtemp leaked the parent on success and setup-fail).
    with tempfile.TemporaryDirectory(prefix="maw-review-") as tmp:
        wt = str(Path(tmp) / f"at-{sha[:12]}")
        print(f"review: worktree at {sha[:12]} → {wt}", file=out)
        _git(["worktree", "add", "--detach", wt, sha], runner, cwd)
        try:
            if link and link_src:
                link_env_dirs(link_src, wt, link, out)
            files = changed_files(resolve_base(base, runner, cwd), runner, cwd=wt) if base else []
            gate = gate_for_paths(files) if files else recommend("unknown")
            label = suite_cmd[-1] if suite_cmd[:3] == ["bash", "-o", "pipefail"] else " ".join(suite_cmd)
            if suite_source:
                label = f"{label}  ({suite_source})"
            print(_gate_block(gate, label), file=out)
            cp = runner(suite_cmd, wt)
            print(cp.stdout, file=out)
            if cp.stderr.strip():
                print(cp.stderr, file=out)
            rc = cp.returncode
            print(f"review: suite exit {rc} (worktree auto-removed)", file=out)
            if attribution:
                print(format_attribution_block(attribution), file=out)
                return rc or ATTRIBUTION_EXIT
            return rc
        finally:
            runner(["git", "worktree", "remove", "--force", wt], cwd)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def prepare_request(
    base: str | None,
    runner: Runner = _run,
    cwd: str | None = None,
    *,
    allow_revert: bool = False,
    depth: int = freshness.DEFAULT_DEPTH,
    err=sys.stderr,
) -> "tuple[int, str]":
    """(exit code, request text) — the pre-file freshness gate, then assembly (#197).

    Exit 3 (distinct from error=1) when the branch is stale against
    origin/<default>, or when it silently reverts recent main work and
    `allow_revert` is off. No request is assembled on a refusal. A missing
    origin ref (offline/local-only) warns and proceeds.
    """
    fr = freshness.check(runner, cwd, depth)
    if fr.warning:
        print(f"review-request: WARNING: {fr.warning}", file=err)
    if fr.stale:
        print(f"review-request REFUSED: {freshness.stale_message(fr)}", file=err)
        return freshness.REFUSE_EXIT, ""
    if fr.reverts and not allow_revert:
        print(f"review-request REFUSED: {freshness.revert_message(fr)}", file=err)
        return freshness.REFUSE_EXIT, ""
    # Default base: the FRESH remote default the check just measured against
    # (falls back to the local default / HEAD offline). An explicit --base wins.
    if base is None:
        base = fr.ref if fr.ref_sha else _local_base(runner, cwd)
    return 0, assemble_request(
        base, runner, cwd, fresh=fr, allowed_reverts=fr.reverts if allow_revert else None,
    )


def _local_base(runner: Runner, cwd: str | None) -> str:
    from net.worktree import base_ref
    return base_ref(cwd, runner)


def cmd_request(args: argparse.Namespace) -> int:
    try:
        rc, text = prepare_request(args.base, allow_revert=args.allow_revert, depth=args.depth)
    except (ReviewError, freshness.WorktreeError) as e:
        print(f"review-request failed: {e}", file=sys.stderr)
        return 1
    if rc == 0:
        print(text)
    return rc


def cmd_lint(args: argparse.Namespace) -> int:
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"review lint failed: {e}", file=sys.stderr)
        return 1
    n, problems = lint_request(text)
    if problems:
        for p in problems:
            print(f"REFUSED: {p}", file=sys.stderr)
        return 2
    print(f"review request OK: {n} adversarial target(s) named")
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    if args.paths:
        files = args.paths
    else:
        try:
            files = changed_files(args.base, _run)
        except ReviewError as e:
            print(f"risk failed: {e}", file=sys.stderr)
            return 1
    print(_gate_block(gate_for_paths(files)))
    return 0


def cmd_commits(args: argparse.Namespace) -> int:
    try:
        findings = ai_attribution_findings(args.base, args.head, _run, allow=args.allow_attribution)
    except ReviewError as e:
        print(f"commits failed: {e}", file=sys.stderr)
        return 1
    if args.allow_attribution:
        print(format_allowed(args.allow_attribution))
    if findings:
        print(format_attribution_block(findings))
        return ATTRIBUTION_EXIT
    print("commit messages OK: no AI attribution")
    return 0


def main_checkout(cwd: str | None = None) -> str | None:
    """The MAIN checkout (parent of the git common dir), even from a linked
    worktree; None outside a repo."""
    cp = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    cd = cp.stdout.strip()
    if cp.returncode != 0 or not cd:
        return None
    return str(Path(cd).resolve().parent)


def cmd_pr(args: argparse.Namespace) -> int:
    try:
        root = _run(["git", "rev-parse", "--show-toplevel"]).stdout.strip() or None
        try:
            cfg, src = load_default_config(root)
        except ConfigRefused as e:
            if not args.suite_cmd:
                print(f"review REFUSED: {e}\n  fix the config on the default branch, or pass "
                      f"--suite-cmd to name the suite yourself", file=sys.stderr)
                return 1
            print(f"review: config ignored ({e}); using --suite-cmd and the default links")
            cfg, src = {}, "default (config refused)"
        if root and args.base and src.endswith(CONFIG_PATH) and not base_is_default(args.base, _run, root):
            print(f"review: --base {args.base} is not the default branch; review config is "
                  f"read from {src} only (a non-default base is unreviewed code and never "
                  f"chooses the suite)")
        if args.suite_cmd:
            cmd, label = args.suite_cmd, "--suite-cmd"
        elif cfg.get("suite_cmd"):
            cmd, label = cfg["suite_cmd"], f"suite_cmd from {src}"
        elif cfg.get("test_command"):
            cmd, label = cfg["test_command"], f"stack.test_command from {src}"
        else:
            cmd, label = DEFAULT_SUITE, "default"
            if src.startswith("default"):
                label = f"suite_cmd from {src}"
        link = cfg.get("link", list(DEFAULT_LINKS))
        return review_pr(args.ref, args.base, suite_cmd=suite_argv(cmd),
                         link=link, link_src=main_checkout(), suite_source=label,
                         allow_attribution=args.allow_attribution)
    except ReviewError as e:
        print(f"review failed: {e}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    req = sub.add_parser("request", help="assemble a review request (auto-fill + prompt targets)")
    req.add_argument("--base", default=None,
                     help="diff base (default: FRESH origin/<default>, fetched first)")
    req.add_argument("--allow-revert", action="store_true",
                     help="file anyway when the branch restores a recent main change's "
                          "pre-image (an INTENTIONAL revert); the request carries REVERTS: lines")
    req.add_argument("--depth", type=int, default=freshness.DEFAULT_DEPTH,
                     help="first-parent main commits per path the revert sniff inspects")
    req.set_defaults(fn=cmd_request)

    lint = sub.add_parser("lint", help="refuse a filled request that names no adversarial target")
    lint.add_argument("file", help="the filled review request ('-' for stdin)")
    lint.set_defaults(fn=cmd_lint)

    risk = sub.add_parser("risk", help="classify paths (or the working diff) into a review tier")
    risk.add_argument("paths", nargs="*")
    risk.add_argument("--base", default="main")
    risk.set_defaults(fn=cmd_risk)

    cm = sub.add_parser("commits", help="block on AI attribution in commit messages of base..head")
    cm.add_argument("--base", default="main")
    cm.add_argument("--head", default="HEAD")
    cm.add_argument("--allow-attribution", action="append", default=[], metavar="SHA",
                    help="reviewer override for a FALSE POSITIVE in the commit-message check: "
                         "drop findings in this commit (SHA, >= 7 hex chars; repeatable)")
    cm.set_defaults(fn=cmd_commits)

    pr = sub.add_parser("pr", help="clean ephemeral worktree at a PR/branch HEAD, run suite, auto-clean")
    pr.add_argument("ref", help="PR number or git ref")
    pr.add_argument("--base", default="main")
    pr.add_argument("--suite-cmd", default=None,
                    help="run via `bash -o pipefail -c`, exactly as typed (pipes allowed). "
                         "Default: review.suite_cmd from .maw/config.yaml on the default branch, else "
                         f"`{DEFAULT_SUITE}`")
    pr.add_argument("--allow-attribution", action="append", default=[], metavar="SHA",
                    help="reviewer override for a FALSE POSITIVE in the commit-message check: "
                         "drop findings in this commit (SHA, >= 7 hex chars; repeatable)")
    pr.set_defaults(fn=cmd_pr)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
