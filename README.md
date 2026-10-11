# maw

Governance for Claude Code agents: who may merge, proven how, logged where.

maw is a Claude Code plugin that turns a repository into a place where two agent sessions build and review each
other's work for hours, while the human's approval stays scoped, time-limited and written to a ledger. It is not an
orchestrator that spawns more agents. It is the part that decides what an agent may do with the result.

## Status: an experimental snapshot, frozen

This repository is a snapshot of one operator's working setup, published as is. It is not a packaged product.
It is frozen at one commit, tagged `v0.1-snapshot`, and it is not maintained: no fixes or updates will be pushed
here. The issues on this repository are the honest record of what is known to be wrong with it.

- The seat commands and the playbook describe that operator's workflow. They name a coordinator session, a run
  page and a Face ID relay, none of which a new install has; each step that uses one says what a seat does
  without it. Messages printed by the code use the same three names; a few values stored in ledgers keep the
  operator's own (`RELEASE-NOTES.md` lists them). Read them as a worked example.
- The plugin installs and its test suite passes on a fresh clone, but a new install has known gaps.
  `RELEASE-NOTES.md` lists them under "Known gaps in a new install".
- The checks stop a confused or over-eager agent. They are not a security boundary against a hostile process
  running as the same user. The guard modules say so in their docstrings.
- If a later snapshot is ever published, it will be a new tag or a new repository, never a rewrite of this one.
  A clone of this commit stays valid.

## Why it exists

Coding agents that ask permission for every step get rubber-stamped. Agents that ask for nothing can't be trusted
with a production branch. maw sits in between:

- **One authorization per session, not per keystroke.** The human arms the advisor session once (Touch ID on macOS),
  for one repo and one seat, for at most six hours. The pair works until it is blocked or the arm expires.
- **Reviewers reproduce; they don't just read.** The advisor checks out the pull request's exact head SHA in a clean
  worktree, reruns the suite, and records a verdict bound to that SHA. If the branch moves, the merge refuses.
- **Only the advisor merges, and only through the gate.** `session_auth merge` re-checks the arm, the signed verdict
  and the reviewed SHA immediately before `gh pr merge`. The executor never merges.
- **Builds can prove their origin.** `/maw:provenance` stamps a built artifact with the SHA it was built from, and
  verifying it refuses a stale or dirty build. It is a review tool the advisor runs, not a step the merge enforces.
- **Everything fails closed.** Missing token, wrong session, expired arm, unreviewed PR, drifted SHA, changed key
  set: each is a refusal, not a warning.
- **Branch protection is the backstop.** `main` accepts pull requests only, enforced for admins. maw relies on that;
  it does not replace it.

The threat model, including what maw does *not* stop, is written in the guard modules' docstrings
(`maw/guards/session_auth.py`, `maw/guards/human_grant.py`).

## What's in the box

All commands are namespaced by the plugin, for example `/maw:advisor`.

| Piece | Entry point | What it does |
|---|---|---|
| Launcher | `maw/bin/maw-seat advisor\|executor\|pair` | Starts a seat in its own git worktree with the environment the plugin expects. |
| Substrate | `/maw:init`, `/maw:status` | Installs maw's git hooks, a handoff schema and `.maw/config.yaml` into a repo (idempotent; keeps your edits beside maw's as `.maw-recommended`), and shows its state. The guards themselves load with the plugin. |
| Seats | `/maw:advisor`, `/maw:executor` | Configures a session as the reviewer or the builder, arms its watchers, and grounds it. |
| Goal protocol | `/maw:goal` | The run discipline: confirm the start, PR often, switch lanes on a block, report merges. |
| Bridge | `/maw:bridge-out`, `/maw:bridge-in`, `/maw:bridge-status` | The durable, audited message line between the seats. Files on disk; survives either session dying. |
| Switchboard | `/maw:net who\|send\|recv` | Lets sessions find and wake each other (tmux or herdr). Seats register themselves on start. |
| Session auth | `maw/bin/maw-run guards.session_auth arm\|check\|merge` | The armed-session token: one human authorization, a six-hour cap, bound to the advisor seat and one repo. |
| Review gates | `/maw:review`, `/maw:risk`, `/maw:provenance`, `/maw:review-request` | Clean-worktree reproduction, risk-tier routing, build-from-SHA verification. |
| Signed grants | `guards.human_grant` | Optional: one-off instructions signed by a passkey on the operator's phone and verified offline (text hash, scope class, 15-minute TTL, single-use nonce, key-set alarm). |
| Destructive guard | a Bash hook | Blocks destructive git operations against a dirty tree and lease-less force-pushes, including inside `sh -c`, `$(...)` and `find -exec`. |

## Quick start

Prerequisites: Claude Code, a repo on GitHub with branch protection on `main` (PR only, enforced for admins), `gh`,
tmux (or herdr), Python 3.11+, and macOS for the Touch ID gate.

`python3` on your `PATH` must be 3.11 or later. The stock macOS `/usr/bin/python3` is 3.9, and `/maw:init` stops
there with one line that says which Python it needs.

The Touch ID gate needs Touch ID enabled for `sudo` (`pam_tid`), and `pam_reattach` as well when the advisor seat
runs inside tmux. Without them `arm` exits 5 (the prompt could not be shown) and writes no token.

Clone this repository first. `<clone>` below is the path of that clone.

```sh
# 1. install the plugin from the clone's marketplace
claude plugin marketplace add <clone>
claude plugin install maw@maw

# 2. in your repo, from a Claude Code session
/maw:init

# 3. from your repo, start the pair: both seats side by side in tmux
<clone>/maw/bin/maw-seat pair

# 4. in the advisor seat: arm (Touch ID), then set the goal
<clone>/maw/bin/maw-run guards.session_auth arm --scope "<one line: what this run may ship>"
```

`maw-run` starts a maw module with maw's own code first on the path. Use it for every module instead of a bare
`python3 -m`, which would let a `guards/` or `net/` directory in your own repo run in maw's place.

From there the executor opens pull requests, the advisor reviews each at its SHA, records a verdict and merges with
`session_auth merge --pr N`, inside the armed window only. `/maw:status` shows the substrate, bridge and arm state.

## Configuration

A few settings belong to your machine, not to the repo. Each is read from its environment variable, then from
`~/.maw/operator.toml`, then a neutral default (`<clone>/maw/bin/maw-run opconf <key>` prints the resolved value):

| Key | Env | Default |
|---|---|---|
| `handoff_dir` | `MAW_HANDOFF_DIR` | `~/.maw/handoff/pending` (where seats look for a seeding goal doc) |
| `board_sh`, `board_dir` | `MAW_BOARD_SH`, `MAW_BOARD_DIR` | unset: the optional fleet board is off |
| `question_to` | `MAW_QUESTION_TO` | unset: `net.question ask` refuses without `--to`. The session a blocked seat's question goes to |
| `lessons_file` | `MAW_LESSONS_FILE` | unset: seats skip the wrap-retro append. Example in `operator.toml`: `lessons_file = "<path>"` |

Other knobs: `MAW_NET_DIR` (the switchboard directory, set by the launcher), `MAW_FEEDBACK_DIR` (default `~/.maw/feedback`).

## Presence checks

The arm gate borrows `sudo`'s authentication: it drops any cached credential, refuses hosts where sudo needs no
password (a `NOPASSWD` rule proves nothing), then forces a fresh authentication. On macOS with `pam_tid` that is a
Touch ID prompt showing the scope being granted. The optional phone path (a passkey grant through a relay) needs your
own enrolled key: a fresh install ships an empty trust store, and every grant refuses until you enroll one
(`maw/guards/keys/README.md`).

## Development

```sh
python3 tests/run_all.py        # the canonical runner: every tests/*/test_*.py
```

The plugin is stdlib-only. The plugin root is `./maw/` because the marketplace schema requires a plugin `source` to
point at a subdirectory; the repo root doubles as the marketplace root.

## Non-goals

- Spawning fleets of agents. Use whatever orchestrator you like; maw governs the merge, not the fan-out.
- An unattended daemon. maw is two live sessions and a human who arms them.
- Replacing branch protection, code review, or CI.

## License

MIT.
