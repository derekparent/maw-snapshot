#!/bin/bash
# check-forbidden-lang.sh — PostToolUse(Edit|Write) on findings/**/*.md.
# Per PRD §4.1 + CLAUDE.md "Forbidden Language" list. Round-2 Hunter trap
# avoidance: skip content inside <ext_data>...</ext_data> blocks.
#
# Exit:
#   0 = clean (no forbidden phrase outside ext_data)
#   2 = found a forbidden phrase

set -u

# 1. Parse event from stdin; extract file_path.
EVENT_JSON="$(cat)"
FILE_PATH="$(printf '%s' "$EVENT_JSON" | python3 -c "
import json, sys
try:
    e = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
ti = e.get('tool_input') or {}
print(ti.get('file_path') or ti.get('path') or '', end='')
" 2>/dev/null)"

# 2. Path filter: findings/**/*.md.
case "$FILE_PATH" in
  *findings/*.md|*findings/*/*.md|*findings/*/*/*.md) ;;
  *) exit 0 ;;
esac

[ -f "$FILE_PATH" ] || exit 0

# 3. Strip ext_data blocks, then regex-match forbidden phrases.
python3 - "$FILE_PATH" <<'PY'
import re, sys

path = sys.argv[1]
with open(path, encoding="utf-8", errors="replace") as f:
    content = f.read()

# Strip ext_data regions (they are DATA, not assertions).
content = re.sub(r"<ext_data\s+source=[^>]*>.*?</ext_data>", "", content, flags=re.DOTALL)

# 10 phrases per CLAUDE.md "Forbidden Language" list. Fuzzy where it helps.
FORBIDDEN = [
    (r"\bsome(?:one|body)\s+(?:should|must|needs?\s+to)\s+(?:investigate|look|check|review)\b",
     "vague-someone-should"),
    (r"\bthis\s+needs?\s+(?:further|more)\s+review\b",
     "needs-further-review-no-owner"),
    (r"\bTODO\s*:\s*figure\s+out\b",
     "todo-figure-out-no-fixer"),
    (r"\bprobably\s+fine\b",
     "probably-fine"),
    (r"\bshould\s+work\b",
     "should-work-unverified"),
    (r"\bclean\s+(?:this|that|it)?\s*up\s+later\b",
     "clean-up-later"),
    (r"\bout\s+of\s+scope\s+but\s+flagging\b",
     "out-of-scope-but-flagging"),
    (r"\bpending\s+verification\b",
     "pending-verification-no-owner"),
    (r"\blikely\s+safe\s+to\s+ignore\b",
     "likely-safe-to-ignore"),
    (r"\b(?:will|i'?ll)\s+revisit\b",
     "will-revisit-vague"),
    (r"\blooks\s+good\s+to\s+me\b",
     "lgtm-no-evidence"),
]

hits = []
for pat, label in FORBIDDEN:
    if re.search(pat, content, re.IGNORECASE):
        hits.append(label)

if hits:
    sys.stderr.write(
        f"check-forbidden-lang: BLOCK — forbidden phrase(s) outside <ext_data>: {hits}\n"
        f"  File: {path}\n"
        f"  Refusal applies per PRD §4.1 forbidden-language list (CLAUDE.md).\n"
    )
    sys.exit(2)

sys.exit(0)
PY
