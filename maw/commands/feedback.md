---
description: Capture dogfood feedback on the maw tooling at session end (any maw-equipped repo)
allowed-tools: [Bash, Edit, Read]
---

# /maw:feedback — dogfood capture (run at session end, in ANY maw repo)

You are closing out a session that ran on the maw substrate (a pair seat, or a
solo session using maw tooling). Capture what the tooling felt like THIS
session, while it's fresh. This file is read later by the maw advisor — it is
how friction on the operator's other repos becomes a maw improvement instead
of dying with your context.

1. Create the file (prints the path):

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/maw-run" feedback.cli new \
  --repo <repo-slug> --seat <advisor|executor|solo> \
  --arc "<one line: what this run was>"
```

2. Edit the created file: fill the three sections. Rules:

- **Five bullets max total.** This runs when your context is most degraded —
  record fresh pain, don't synthesize.
- **Concrete beats felt.** "bridge-out failed twice on turn 41 with lock
  timeout" is usable; "bridge felt clunky" is not. Name the command, the
  error, the workaround you used.
- **What worked** matters too — it tells the advisor what NOT to touch.
- List PR numbers touched this run in the `prs:` frontmatter field.
- Nothing notable? Write one bullet under "What worked" saying the run was
  clean — a clean-run signal is data.

Never put secrets, tokens, or key material in a feedback file.
