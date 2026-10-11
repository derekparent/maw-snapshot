#!/bin/bash
# check-ext-data.sh — Three-state ext_data validator. Per PRD §4.2 + D27.
#
# Modes:
#   $1 = "read"   → PreToolUse(Read), force re-validation before content
#                   reaches the agent if file isn't validated this session
#   $1 = "write"  → PostToolUse(Edit|Write), validate after write
#
# Path filter: findings/**/*.md only. Other paths exit 0 immediately.
#
# Three states:
#   0 = safe (clean)
#   1 = warn-allow (file has gaps; mark needs_human_review; NOT blocking)
#   2 = block (malicious patterns inside <ext_data>; quarantine)
#
# Hook input on stdin: PreToolUse/PostToolUse event JSON.

set -u

MODE="${1:-write}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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

# 2. Path filter: findings/**/*.md only.
case "$FILE_PATH" in
  *findings/*.md|*findings/*/*.md|*findings/*/*/*.md) ;;
  *) exit 0 ;;
esac

# 3. File must exist (write hook fires after write; read hook fires before read).
[ -f "$FILE_PATH" ] || exit 0

# 4. Three-state evaluation via Python (regex over file content with ext_data skip).
python3 - "$FILE_PATH" "$REPO" "$MODE" <<'PY'
import os, re, shutil, sys, datetime

path, repo, mode = sys.argv[1], sys.argv[2], sys.argv[3]

with open(path, encoding="utf-8", errors="replace") as f:
    content = f.read()

# Extract ext_data regions; record contents and the surrounding (outside) text.
ext_pattern = re.compile(r"<ext_data\s+source=[^>]*>(.*?)</ext_data>", re.DOTALL)
inside = "\n".join(m.group(1) for m in ext_pattern.finditer(content))
outside = ext_pattern.sub("", content)

# State 2: prompt-injection canaries inside ext_data.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"ignore\s+(all\s+)?prior\s+instructions",
    r"system\s*:\s*you\s+are\s+now",
    r"\bsudo\s+rm\s+-rf\b",
    r"</ext_data>",  # nested-escape attempt within an ext_data block
]
for pat in INJECTION_PATTERNS:
    if re.search(pat, inside, re.IGNORECASE):
        # Quarantine to findings/.tmp/quarantine/
        qdir = os.path.join(repo, "findings", ".tmp", "quarantine")
        os.makedirs(qdir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        qpath = os.path.join(qdir, f"{ts}-{os.path.basename(path)}")
        try:
            shutil.move(path, qpath)
        except Exception as e:
            sys.stderr.write(f"check-ext-data: quarantine move failed: {e}\n")
        sys.stderr.write(
            f"check-ext-data: BLOCK — injection canary matched inside <ext_data>: pattern={pat!r}\n"
            f"  Quarantined (removed from findings/): {qpath}\n"
        )
        sys.exit(2)

# State 1: captured-content heuristic outside ext_data wrapper.
# Triggers if the file LOOKS like it ingested external content (long quoted
# blocks, "captured:" labels, etc.) without wrapping it.
HEURISTICS = [
    r'(?m)^\s*captured\s*:\s*\S',
    r'(?m)^\s*content\s*:\s*"',
    r'(?m)^>\s.{200,}',  # blockquote with 200+ chars (probably pasted content)
]
gaps = []
for pat in HEURISTICS:
    if re.search(pat, outside, re.IGNORECASE):
        gaps.append(pat)
if gaps:
    sys.stderr.write(
        f"check-ext-data: WARN-ALLOW (mode={mode}) — captured-content heuristics outside <ext_data>: {gaps}\n"
        f"  Mark unit needs_human_review; do not block ship.\n"
    )
    sys.exit(1)

# State 0: clean.
sys.exit(0)
PY
