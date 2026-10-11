#!/bin/sh
# validate_plugin.sh — operator-run after setup. Verifies the cache version dir
# is a REAL DIRECTORY COPY (NOT a symlink) of the pinned plugin, whose
# .maw-pinned-sha sentinel matches the pinned HEAD and whose plugin.json is
# readable. Then PRINTS the live durability gate the operator must run by hand.
#
# WHY a real copy (live-validated 2026-06-18): a SYMLINK at the cache version
# dir is REMOVED by Claude Code on the next session start (it "repairs" the
# non-directory plugin entry; the no-op install then leaves nothing). A REAL
# COPY at the same version label SURVIVES session starts. So a symlink here is a
# FAIL — it is a latent empty cache.
#
# WHY a live gate: this script proves the FILESYSTEM is a sound copy. Only a
# fresh live session proves (1) Claude Code resolves CLAUDE_PLUGIN_ROOT to the
# copy for the slash path AND (2) the copy SURVIVES the session start. That
# survival is the durability gate the symlink model failed.
set -u

CACHE_BASE="${HOME}/.claude/plugins/cache/maw/maw"
PINNED="${HOME}/.maw/plugin-main"
PINNED_PLUGIN="${PINNED}/maw"
SENTINEL=".maw-pinned-sha"

fail=0

echo "== filesystem validation (copy model) =="

if [ ! -d "${CACHE_BASE}" ]; then
  echo "FAIL: cache base missing: ${CACHE_BASE}"
  echo "      run: bash maw/repin/setup_pinned_plugin.sh"
  exit 1
fi

PINNED_HEAD="$(git -C "${PINNED}" rev-parse HEAD 2>/dev/null || true)"
if [ -z "${PINNED_HEAD}" ]; then
  echo "WARN: could not read pinned HEAD at ${PINNED} — sentinel match cannot be verified"
fi

found_dir=0
for vd in "${CACHE_BASE}"/*; do
  [ -e "${vd}" ] || [ -L "${vd}" ] || continue
  name="$(basename "${vd}")"
  if [ -L "${vd}" ]; then
    echo "FAIL: ${name} is a SYMLINK — claude removes symlinked plugin entries on"
    echo "      session start (latent empty cache). It MUST be a real directory copy."
    fail=1
    continue
  fi
  if [ ! -d "${vd}" ]; then
    echo "FAIL: ${name} is not a directory"
    fail=1
    continue
  fi
  found_dir=1
  if [ -r "${vd}/.claude-plugin/plugin.json" ]; then
    echo "ok:   ${name} is a real dir with a readable plugin.json"
  else
    echo "FAIL: ${name} has no readable .claude-plugin/plugin.json (empty/corrupt copy)"
    fail=1
  fi
  if [ -r "${vd}/${SENTINEL}" ]; then
    sha="$(cat "${vd}/${SENTINEL}" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "${PINNED_HEAD}" ] && [ "${sha}" = "${PINNED_HEAD}" ]; then
      echo "ok:   ${name} sentinel matches pinned HEAD (${sha})"
    elif [ -n "${PINNED_HEAD}" ]; then
      echo "FAIL: ${name} sentinel ${sha} != pinned HEAD ${PINNED_HEAD} (recopy due)"
      echo "      run: python3 -m repin.check sync"
      fail=1
    else
      echo "note: ${name} sentinel=${sha} (pinned HEAD unknown; could not compare)"
    fi
  else
    echo "FAIL: ${name} missing sentinel ${SENTINEL} (cannot prove it matches main)"
    echo "      run: python3 -m repin.check sync"
    fail=1
  fi
done

if [ "${found_dir}" -eq 0 ] && [ "${fail}" -eq 0 ]; then
  echo "FAIL: no cache version dir found under ${CACHE_BASE}"
  fail=1
fi

# The copy SOURCE must also be a real maw/ dir with a readable plugin.json.
manifest="${PINNED_PLUGIN}/.claude-plugin/plugin.json"
if [ -r "${manifest}" ]; then
  echo "ok:   pinned copy source manifest readable: ${manifest}"
else
  echo "FAIL: pinned copy source manifest not readable: ${manifest}"
  fail=1
fi

echo
if [ "${fail}" -ne 0 ]; then
  echo "RESULT: FAIL — the cache copy is not healthy."
  echo "Re-run setup: bash maw/repin/setup_pinned_plugin.sh"
  echo "Or recopy:    python3 -m repin.check sync"
  exit 1
fi

echo "RESULT: filesystem PASS (real copy, sentinel matches pinned main)."
echo
echo "== LIVE DURABILITY GATE (operator must run this by hand) =="
echo "The filesystem is a sound copy, but only a fresh live session proves the"
echo "slash path resolves AND that the copy SURVIVES the session start (the"
echo "durability gate the symlink model failed)."
echo
echo "  1. Launch a FRESH 'claude' session (new process), e.g.:"
echo "       claude -p \"/maw:status\""
echo "       PASS if it renders substrate/capability/bridge state (not 'plugin not found')."
echo "  2. RE-CHECK that the copy SURVIVED the session start:"
echo "       bash maw/repin/validate_plugin.sh   # must still print RESULT: filesystem PASS"
echo "       (a symlink would be GONE here — that is the whole point of the copy model.)"
echo "  3. Optional: /maw:net who   # PASS if it lists live sessions (not empty/error)."
echo
echo "If ALL pass -> the pinned-worktree COPY model is live + durable. Install the"
echo "post-merge hook (runbook Step 3) and you never repin by hand again."
