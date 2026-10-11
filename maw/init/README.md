# Layer 1 — `/maw:init`

Installs the maw substrate into a target repo, once per repo. It writes:

- `.maw/config.yaml` (the profile and resolved variables) and `.maw/state.json`
  (what was installed, with checksums);
- `.claude/schemas/handoff.json` and the other schema files the profile lists;
- `.git/hooks/pre-push` (the `MAW_HUMAN` push gate).

It does **not** write `.claude/settings.json` and does not register hooks. The
plugin's own hooks (switchboard registration and heartbeat, capability probe,
supervision watchdog, destructive-op guard) come from `maw/hooks/hooks.json` and
load with the plugin; nothing in the target repo is needed for them.

**Existing files are never overwritten.** A repo that already has a `.claude/`
(settings, commands, anything) is adopted as is. When a file maw would install
already exists and differs, maw keeps yours and writes a `<file>.maw-recommended`
sibling beside it. `--force` backs up and overwrites; `--dry-run` prints the plan;
`--upgrade` moves to a newer template set.

## Templates

Templates live under `init/templates/` and use `{{var}}` placeholders resolved at
install time from `.maw/config.yaml`.

- `templates/profiles/` — stack defaults: `generic.yaml`, `python-flask-railway.yaml`,
  `python-fastapi-railway.yaml`, `node-next-vercel.yaml`. Each lists the files it
  installs.
- `templates/git-hooks/` — `pre-push`, which every profile installs, and
  `post-merge`, which no profile installs: it is the deploy hook of the maw
  source repo itself (`repin.check hook --install` places it there).
- `templates/schemas/` — `handoff.json`.
- `templates/hooks/` — **opt-in** repo-local Claude Code hook scripts (stdlib
  only): `budget-guard.py` (tool-call count bands), `depth-guard.py` (caps nested
  agent spawns via `MAW_DEPTH`), `scan-args.py` (blocks risky Bash flags when
  `MAW_AGENT=1`), `validate-launch.py` (requires a handoff document for agent
  spawns), `check-ext-data.sh`, `check-forbidden-lang.sh`. No profile installs
  them by default, because a hook only runs once registered and these gate every
  tool call or agent spawn. To adopt one, list it under `hooks:` in your profile
  and register it in the repo's `.claude/settings.json` yourself.

## Opting in to a template hook

Two steps: list the template under `hooks:` in the profile (so `/maw:init`
installs it into `.claude/hooks/`), then register it in the repo's
`.claude/settings.json`. Merge the entry into your existing `hooks` object; do
not replace the file. Each entry below is the exact registration.

| Hook | Runs on | Blast radius |
|---|---|---|
| `budget-guard.py` | **every tool call** | Warns in bands as a session's tool-call count grows. It can slow or annoy every session in the repo. |
| `depth-guard.py` | every Agent/Task spawn | Refuses a spawn once `MAW_DEPTH` reaches 3. Inert unless something sets `MAW_DEPTH`. |
| `validate-launch.py` | **every Agent/Task spawn** | Refuses a spawn whose prompt lacks a valid handoff document (`.claude/schemas/handoff.json`). Enabling it blocks ordinary subagent use that does not follow that schema. |
| `scan-args.py` | every Bash call | Blocks risky flag patterns, only when `MAW_AGENT=1` is set in the environment. |
| `check-ext-data.sh` | Read, and Edit/Write | Validates `<ext_data>` blocks in `findings/` documents. Only useful if the repo follows that convention. |
| `check-forbidden-lang.sh` | Edit/Write on `findings/**/*.md` | Blocks listed phrases outside `<ext_data>`. Same convention. |

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "*",          "hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/budget-guard.py"}]},
      {"matcher": "Task|Agent", "hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/depth-guard.py"}]},
      {"matcher": "Task|Agent", "hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/validate-launch.py"}]},
      {"matcher": "Bash",       "hooks": [{"type": "command", "command": "python3 \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/scan-args.py"}]},
      {"matcher": "Read",       "hooks": [{"type": "command", "command": "sh \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/check-ext-data.sh read"}]}
    ],
    "PostToolUse": [
      {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "sh \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/check-ext-data.sh write"}]},
      {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "sh \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/check-forbidden-lang.sh"}]}
    ]
  }
}
```

The `"${CLAUDE_PROJECT_DIR}"` prefix anchors each path at the repo root, so a
hook still resolves after the session `cd`s into a subdirectory. The quotes
keep a project path with spaces intact.

Take only the entries for the hooks you installed. Whether maw should register
any of these by default is an open question; today none are.

