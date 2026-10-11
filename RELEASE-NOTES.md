# maw release notes

This repository is an experimental snapshot of maw, exported from the operator's private working repository. The
export is one commit with no private history. It is built by a script from one commit on the private repo's `main`:
the script only leaves files out, plus two edits: one to the marketplace file, and one added file,
`maw/.maw-release-stamp`, which records the source commit and its date so an install can say which commit it is.
Every other file in the export commit is byte-identical to its source.

The export commit's message names the source commit on the private `main`. The commit is authored under a GitHub
no-reply address and dated in UTC, and it is tagged `v0.1-snapshot`.

This snapshot is frozen. It is not updated in place and it is not maintained. A later snapshot, if there is one,
is a new tag or a new repository. What is known to be wrong is listed below and in the repository's issues.

## What is in

- **The plugin** (`maw/`): the seat commands, the substrate installer (`/maw:init`), the bridge and switchboard, the
  review gates, the destructive-command guard, the merge gate (`guards.session_auth`) and the optional phone-grant
  path (`guards.human_grant`, `phone_mint`, `relay/`).
- **The launcher** `maw/bin/maw-seat`: starts an advisor, an executor, or both side by side.
- **The tests** (`tests/`): run with `python3 tests/run_all.py`.
- **The docs**: the README, five docs in `docs/` that explain the tool to a newcomer, and five in `maw/docs/` that
  the seat commands point to (the advisor playbook, the watch recipes, the batch-arm runbook, the oracle-review
  doc and the goal-doc template). Those five are inside the plugin so an installed copy has them.

## What was left behind, and why

- **The private record**: design findings, the decision log, run state and agent instructions. They are a log of
  what people decided and did, not documentation of the tool.
- **The operator's names for his own sessions and pages, in the seat text and in printed messages.** The commands
  and the docs under `maw/docs/` are rewritten at export: his coordinating session becomes "the coordinator", his
  run page "the run page", his launch document "the goal doc", and the steps that call his session-memory tools are
  removed. The messages and `--help` text the Python prints are reworded the same way, 24 lines in 6 modules, and
  so are the 3 test assertions that quote one. Both rewrites are kept in the private repository. They change
  wording only: no branch, value or name in the code differs.
- **The operator's public keys.** The trust store ships empty, so every phone grant refuses until you enroll your
  own key (`maw/guards/keys/README.md`). The Touch ID arm is unaffected.
- **A recorded real passkey assertion** and the tests that read it: its signature covers the operator's own host
  name. The tests that need it skip when it is absent.
- **Three packages that are not part of the tool**: an early channel-plugin experiment (superseded by the
  switchboard), the operator's model-evaluation harness, and an experimental sensor client. The marketplace lists
  only `maw`.
- **Internal design documents**: the PRDs, research notes and runbooks for the operator's own deployment. The
  shipped docs are an allowlist, so a new doc stays private until someone lists it.

## Changes since the previous internal milestone

- The merge gate binds each merge to the SHA the advisor reviewed, enforces the reviewer seat, and binds tokens and
  verdicts to one canonical repo identity. An optional `--prs` set can pin which PRs a session may merge.
- The arm prompt shows the whole scope being granted, cut visibly if it is long. The presence check refuses hosts
  where `sudo` needs no password.
- The destructive-command guard sees git operations inside `sh -c` and `bash -o … -c`, `eval`, `$(...)` and
  backticks, and `find -exec`. It checks force-pushes and branch deletions inside those too, and blocks shell
  nesting too deep to read. Its error path honours the override its own message names.
- `/maw:init` adopts an existing `.claude/`, never writes through a symlink, stops rewriting unchanged
  `.maw-recommended` files, and says when a repo's own pre-push hook means the push gate is not active.
- Operator settings moved out of the code into `~/.maw/operator.toml`, read by `python3 -m opconf` (an env var
  overrides each): the handoff directory and the optional board, the ids of the operator's committed signing keys
  (`grant_pubkey_id`, `phone_pubkey_id`, `relay_pubkey_id`), and the repositories the operator classified no-echo
  (`no_echo_repos`, a record only: every repository is already no-echo by default). The key ids have no default:
  unset, the minting and enrollment tools refuse before anything is signed, shown or posted. Verification never
  reads them; it trusts only the committed keys. The passkey enrollment page names its user `maw-operator`.
- **Renamed trust banners**: messages the switchboard marks as operator-authorized now read `OPERATOR-AUTHORIZED` and
  `OPERATOR-TYPED` (formerly with the operator's initials).

Later changes, through 2026-10-08:

- **Batch arm**: one passkey approval on the phone can arm several advisor seats at once (`session.arm.batch`,
  `phone_mint.relay_client mint-batch`, `pull-batch`, `deliver-batch`). The relay page shows one card per seat and
  serves a fingerprint of the page it shows. The runbook is `maw/docs/cos-batch-arm.md`.
- **Seat commands run maw's own code**: every command and hook starts modules through `maw/bin/maw-run`, so a
  `guards/` or `net/` directory in the target repo cannot run in maw's place.
- **The destructive-command guard** reads text and heredocs piped into a shell, here-strings, scripts fed by stdin
  and process substitutions. It catches `--mirror` and `--prune` pushes, case arms and shell functions, and it passes
  some safe forms it used to refuse.
- **The merge gate** has one canonical runtime root, prints the merge scope before the merge, reports a failed fetch,
  and gives git the full remote ref. `arm` exits with a distinct code for declined, timed out, unreachable and
  unprovable.
- **Supervision**: the pair sweep stops paging seats that are idle by design. `maw-watch-inbox` is the inbound
  watcher. `maw/docs/watch-recipes.md` says what a watch can and cannot do.
- **Review**: a review request refuses a stale or reverting branch, the reviewer blocks on attribution trailers in
  commit messages, and a repo can set its own suite command.
- **Bridge and switchboard**: `send --allow-literal` with an audited read for quarantined messages, safe send
  bodies from stdin, `net.question resolve`, and `recv` never consumes a session-class grant.
- **`/maw:init` and `/maw:status`**: init writes without following a swapped-in symlink and says where hooks land
  under `core.hooksPath`. Status shows the merge posture and the arm state.
- **Trust store**: an empty one says that no key is installed, and its README gives enrollment steps.

## Known gaps in a new install

Found by installing this snapshot into an empty home directory and following the README. None of them leaks
anything or opens a gate. Each one confuses a newcomer.

- **The Touch ID arm needs setup the tool does not do**: Touch ID enabled for `sudo`, and `pam_reattach` when the
  seat runs inside tmux. Without them `arm` exits 5.
- **The seat commands name three optional surfaces a new install does not have**: a coordinator session
  (`question_to`), a run page (`deck_url`) and a Face ID relay (`relay_pubkey_id`). Each step that uses one says how
  to tell it is absent and what the seat does instead, and the session-memory steps are gone. No live seat on a
  fresh install has followed that text yet. A blocked seat's question has no addressee until you set
  `MAW_QUESTION_TO` (or `question_to` in `operator.toml`) or pass `--to`; until then `net.question ask` refuses and
  says so.
- **Seven values in the code keep the operator's name for the coordinator**, because they travel on the wire or
  sit in a ledger and the code compares them: the role and session name `cos` (`guards/typed_approval.py`,
  `net/watch.py`, `net/deadman.py`), the approval channel `cos-terminal` (`guards/typed_approval.py`), the answer
  source `cos-within-goal` (`net/question.py`, also in the reply format line it prints) and the escalation method
  `stderr+net-send-cos` (`net/deadman.py`). Read `cos` as "coordinator". Printed messages and `--help` text do not
  use the operator's names otherwise.
- **Comments, identifiers and docstrings still use the operator's own names** ("the CoS", "the Deck", "launch
  brief"): about 25 comments and 55 docstring mentions in the Python, and comments in `maw/bin/maw-watch-inbox` and
  `maw/hooks/watchdog-arm.sh`. None of them is printed. The tests keep them as well, in inputs, fixtures and
  test names (158 string literals and 58 test names); you meet those only by reading a test or its name in a
  test report. The module, the config key and the runbook keep their names too: `net.deck`, `deck_url` /
  `MAW_DECK_URL`, `maw/docs/cos-batch-arm.md`.
- **Several files cite documents that were left behind** (`DECISIONS.md`, `findings/...`, `docs/design/...`, the PRDs
  and runbooks). Those citations are dead ends here, and so is `[[merge-posture-session-auth]]` in a docstring and a
  comment of `maw/guards/session_auth.py`: it is the name of a private note of the operator's. So are the issue and
  pull request numbers (`#149`, `PR #42` and
  the like) in comments, tests and command text: they belong to the private repository this was exported from, not to
  this one.

## Known debt

These fail safe (they refuse, or they only affect wording) unless noted.

- **The Touch ID dialog at full length** (up to 360 characters of scope) has not been seen rendered on a real arm.
  The text reaches the prompt; whether macOS shows all of it is unverified.
- **The author name** in `LICENSE` and the plugin manifests is the operator's own name, by his decision. The export
  test lists every file that carries it and fails on any new one.
- **The destructive-command guard** reads text piped into a shell (`echo`/`printf … | bash`, `|&`, and
  `xargs -I{} sh -c '{}'`) and a heredoc piped into a shell after its terminator, across a backslash-newline, or
  redirect-first (`<<EOF bash`). It reads a here-string fed to a shell (`bash <<< '…'`) and a variable assigned a literal in the same
  command. It reads a script named by `/dev/stdin`, `/dev/fd/0` or `-`, `source`/`.`/`eval "$(cat)"`
  fed by stdin, `cat <<< '…' | bash`, `bash <(echo …)` and `bash < <(…)`, and it scans the body of
  every process substitution. It does not read `bash -c $'…'` (ANSI-C payloads), a variable set
  outside the `$( )` that uses it, or a reassigned variable (resolution is order-blind). Any use of an xargs replace string in a shell's `-c` counts as code. It blocks `find -execdir` even on a clean tree, and it reads `-exec` used as another
  option's value as an exec. A 20,000-line heredoc takes a few seconds to parse. The override accepts only `1` from
  the environment but `1`/`true`/`yes` inline.
- **The bridge content scan** (the forbidden-pattern registry, a regex pass over message text) does not catch a
  script downloaded to a file and then run, a merge through the raw REST API instead of `gh pr merge`, or a `chmod`
  with an option between the mode and the path. The destructive-command guard is the execution-time gate for
  those. A message that merely quotes a caught string is quarantined; read it with `--allow-literal` (#316).
- **`/maw:init`** refuses a symlink at the write itself, but a link swapped over an existing file in the instant
  between its last check and the rename is replaced (never written through). It does not check symlinks under
  `.maw/.runtime` (written by the capability probe), and a `.git/hooks` that is itself a symlink out of the repo
  still receives the hooks. Rewriting an existing `.gitignore`, or a `--force` target, leaves it mode 0600 even if
  it was 0644. When a repo uses `core.hooksPath`, hooks are installed into that path (not `.git/hooks`), and the
  banner now prints a NOTE saying where. The per-file action lines still read `.git/hooks/...`, and a shared
  hooksPath runs maw's hooks for every repo that uses it.
- **`/maw:provenance`** is a review tool the advisor runs. It is not wired into the merge gate.
- **The phone-grant relay** keeps no sweep of expired requests.
- **The plugin cache**: commands resolve `CLAUDE_PLUGIN_ROOT` to the pinned checkout, not the cached copy, so the
  cache copy may be unnecessary. Hooks were not checked.
- **The native `SendMessage` wake** was confirmed only for a busy receiving session.
- **Tests**: one bridge test writes a lock file under `$HOME/.claude/bridge`. The suite needs Python 3.11 or later.

## Verification of this export

Measured on 2026-10-11 (UTC) on the export of source commit `c67e4ea1921a` (export commit `a301382ed4c0`). This snapshot
is cut from the merge of the change that last edited this section, so it differs from the measured export in two
files only: this one and `maw/.maw-release-stamp`. Every number below was measured in this check.

- **Suite**, in a fresh clone of the export with an empty `HOME`, once on each Python: 3.11.14, 3.12.12, 3.13.12
  and 3.14.2. Each run: 2623 tests, 0 failures, 0 errors, 18 skipped. The 18 are the same on every version: 9 need
  a recorded passkey assertion that is left out of the export, 8 need the relay's `node_modules` (`cd relay &&
  npm ci`; they boot the Worker in `wrangler dev`), and 1 needs an optional package that is left out. Relay:
  `node --test relay/test/worker.test.mjs` passes, 11 of 11.
- **Install**, in an empty home directory, following the README, with Claude Code 2.1.296: both manifests validate,
  and `claude plugin marketplace add` and `claude plugin install maw@maw` succeed. The body of `/maw:init`, run by
  hand, installs into a new repo; `/maw:status` reads it and does not call it stale; the session hooks all exit 0.
  An install whose stamp is old or missing does read stale, and the drift hook says so. With the stock macOS
  Python 3.9 first on `PATH`, `/maw:init` stops with one line that names the Python it needs.
- **Not run in this check**: no logged-in session typed the commands (the empty home has no login, so each command's
  body was run directly). No seat was started on this cut. `maw-seat pair` was started for real once, on the
  2026-10-10 cut (no launcher code has changed since): both seats came up in their own worktrees with the seat environment set, and stopped at
  Claude Code's first-run screen. Seat bring-up past that screen, the wire between two seats, and the Touch ID arm
  were not run. The target repo had no GitHub remote.
- **Guards**: the defects an outside review reproduced in September are pinned by tests in the suite above (they were
  not counted again in this check). Three were reproduced again through the installed hook (a `git -C` reset against a
  dirty repo from a clean directory, a dirty repo whose path contains `maw-review-`, and a force push without a
  lease). All three block.
- **Privacy scan** (secrets, workplace details, emails, personal paths and hosts, quoted speech, personal topics,
  transcript-like files) over the tree and its one commit: no real hits. The secret-pattern hits are variable names
  and a synthetic test constant; the emails and hosts are synthetic; the quoted-speech hits are inputs a detector's
  tests must refuse; the personal-topic hits are ordinary technical words. `gitleaks` 8.30.1 with its default rules
  reports one finding, `relay/handler.mjs` line 203: a list of three field names, not a value. The operator facts
  left are the author name above and his initials: in the older spellings three commands still accept (`dp-only`,
  `source: dp`, `settled-by-dp`, `--dp-text-file`), in a stored field name (`dp_text`), in a detector's pattern and
  its test inputs, and in synthetic test key ids. The export test lists every file that carries them and fails on
  a new one. One recorded test input, `tests/review/fixtures/rrB.txt`, names one of his own shell launchers; a
  reader uses `maw-seat`. The export test fails on any other file that names one.
