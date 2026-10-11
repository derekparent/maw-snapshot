#!/bin/sh
# setup_pinned_plugin.sh — one-time, IDEMPOTENT setup of the pinned-worktree
# plugin model (COPY variant). Operator-run. Safe to re-run.
#
# THE MODEL (live-validated 2026-06-18; `repin.check recipe` names the runbook where one exists):
#   ~/.maw/plugin-main           = a git worktree of the maw repo, pinned to main
#   ~/.maw/plugin-main/maw       = the plugin subdir (the COPY SOURCE)
#   ~/.claude/plugins/cache/maw/maw/<version>  = a REAL DIRECTORY COPY of it
#   ~/.claude/plugins/cache/maw/maw/<version>/.maw-pinned-sha = pinned HEAD sha
#
# WHY A COPY, NOT A SYMLINK: Claude Code REMOVES a symlinked plugin entry on the
# next session start ("repairs" the non-directory entry; the no-op install then
# leaves nothing — confirmed twice). A real copy SURVIVES session starts
# (confirmed across 3 fresh `claude -p` runs). So the cache must be a real dir,
# refreshed by file copy, NEVER by `claude plugin install` (it no-ops on the
# unchanged version label or empties the cache).
#
# Run this, then run repin/validate_plugin.sh, then the live durability gate.
set -eu

PINNED="${HOME}/.maw/plugin-main"
PLUGIN_SUBDIR="maw"
CACHE_BASE="${HOME}/.claude/plugins/cache/maw/maw"
SENTINEL=".maw-pinned-sha"
MARKETPLACES="${HOME}/.claude/plugins/known_marketplaces.json"

# --- self-locate the maw repo (this script lives at <repo>/maw/repin/) --------
script_dir="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${script_dir}/../.." && pwd)"
if [ ! -f "${REPO_ROOT}/maw/repin/check.py" ]; then
  echo "error: could not locate the maw repo from ${script_dir}" >&2
  echo "       expected ${REPO_ROOT}/maw/repin/check.py" >&2
  exit 1
fi
echo "maw repo: ${REPO_ROOT}"

# --- (a) pinned worktree: create on plugin-pin if absent, else fetch + ff-only
# (UNCHANGED — this is the half that fixes the contended-editing-tree fault.)
mkdir -p "${HOME}/.maw"
if [ -d "${PINNED}/.git" ] || git -C "${PINNED}" rev-parse --git-dir >/dev/null 2>&1; then
  echo "pinned worktree exists: ${PINNED} — fetch + ff-only"
  git -C "${PINNED}" fetch origin
  git -C "${PINNED}" merge --ff-only refs/remotes/origin/main || {
    echo "warn: ff-only failed (pinned worktree diverged/dirty?) — investigate ${PINNED}" >&2
  }
else
  echo "creating pinned worktree: ${PINNED} (dedicated branch plugin-pin tracking origin/main)"
  git -C "${REPO_ROOT}" fetch origin
  # Pin the mirror on a DEDICATED local branch (plugin-pin) that tracks
  # origin/main. This sidesteps the 'main is already checked out in the primary
  # tree' collision entirely (git refuses a second checkout of the same branch),
  # so the mirror is never born detached. ff-only sync still works against
  # origin/main. If plugin-pin already exists (a prior partial run), attach the
  # worktree to it instead of recreating it.
  if git -C "${REPO_ROOT}" show-ref --verify --quiet refs/heads/plugin-pin; then
    echo "note: branch plugin-pin already exists; attaching the worktree to it"
    git -C "${REPO_ROOT}" worktree add "${PINNED}" plugin-pin
    git -C "${PINNED}" merge --ff-only refs/remotes/origin/main || {
      echo "warn: ff-only of plugin-pin to origin/main failed — investigate ${PINNED}" >&2
    }
  else
    git -C "${REPO_ROOT}" worktree add --track -b plugin-pin "${PINNED}" refs/remotes/origin/main
  fi
fi

if [ ! -d "${PINNED}/${PLUGIN_SUBDIR}" ]; then
  echo "error: ${PINNED}/${PLUGIN_SUBDIR} not found after worktree setup" >&2
  exit 1
fi

PINNED_HEAD="$(git -C "${PINNED}" rev-parse HEAD 2>/dev/null || true)"
if [ -z "${PINNED_HEAD}" ]; then
  echo "error: could not determine pinned HEAD sha at ${PINNED}" >&2
  exit 1
fi
echo "pinned HEAD: ${PINNED_HEAD}"

# --- (b) ensure maw@maw is REGISTERED (install once only, to create the record)
# We do NOT rely on install to deliver content anymore — the COPY (step c) does.
# This only creates the registry record if missing. Never re-install on an
# already-registered plugin: that no-ops on the unchanged version label or
# empties the cache (the #36 fault).
if command -v claude >/dev/null 2>&1; then
  if claude plugin list 2>/dev/null | grep -q "maw@maw"; then
    echo "maw@maw already registered — skipping install (content comes from the copy)"
  else
    echo "registering maw@maw (one-time install to create the record)"
    claude plugin install maw@maw || \
      echo "warn: 'claude plugin install maw@maw' failed — register the marketplace, then re-run" >&2
  fi
else
  echo "warn: 'claude' not on PATH — skipping registration; ensure maw@maw is registered manually" >&2
fi

# --- (c) recopy the pinned plugin into the cache version dir(s) + sentinel ----
# REAL directory copies, NOT symlinks. cp -R the pinned maw/ in, then write the
# .maw-pinned-sha sentinel so `repin.check` can prove the copy matches main.
mkdir -p "${CACHE_BASE}"
copied=""
recopy_into() {
  vd="$1"
  name="$(basename "${vd}")"
  echo "recopying cache version dir (real copy): ${name} <- ${PINNED}/${PLUGIN_SUBDIR}"
  rm -rf "${vd}"
  mkdir -p "${vd}"
  # copy the CONTENTS of the pinned plugin dir into the version dir
  cp -R "${PINNED}/${PLUGIN_SUBDIR}/." "${vd}/"
  printf '%s\n' "${PINNED_HEAD}" > "${vd}/${SENTINEL}"
  copied="${name}"
}

for vd in "${CACHE_BASE}"/*; do
  [ -e "${vd}" ] || [ -L "${vd}" ] || continue
  recopy_into "${vd}"
done

# If the cache base had NO version dir at all (today's BROKEN/empty state),
# derive a label from plugin.json so CLAUDE_PLUGIN_ROOT has a dir to resolve.
if [ -z "${copied}" ]; then
  version="$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' \
    "${PINNED}/${PLUGIN_SUBDIR}/.claude-plugin/plugin.json" 2>/dev/null \
    | sed 's/.*"\([^"]*\)"$/\1/')"
  [ -n "${version}" ] || version="0.0.0-pinned"
  echo "no cache version dir present — creating real copy ${version} <- ${PINNED}/${PLUGIN_SUBDIR}"
  recopy_into "${CACHE_BASE}/${version}"
fi

# --- (d) repoint the marketplace source to the pinned worktree (defense-in-depth)
# The copy survives regardless, but pointing the 'maw' marketplace source at the
# pinned worktree means any claude-INITIATED auto re-sync copies from clean main,
# not the contended live editing tree. Careful JSON read-modify-write that
# preserves all other keys; back up first. autoUpdate reverting to true on its
# own is harmless under the copy model and is NOT relied on here.
if [ -f "${MARKETPLACES}" ]; then
  echo "repointing marketplace 'maw' source -> ${PINNED} (backup: ${MARKETPLACES}.bak)"
  cp "${MARKETPLACES}" "${MARKETPLACES}.bak"
  MAW_PINNED="${PINNED}" python3 - "${MARKETPLACES}" <<'PY' || \
    echo "warn: marketplace repoint skipped (JSON parse/shape unexpected) — copy still survives" >&2
import json, os, sys
path = sys.argv[1]
pinned = os.environ["MAW_PINNED"]
with open(path, encoding="utf-8") as f:
    data = json.load(f)

def repoint(entry):
    # entry is the marketplace record for 'maw'. Preserve every other key.
    if not isinstance(entry, dict):
        return False
    changed = False
    src = entry.get("source")
    if isinstance(src, dict):
        if src.get("path") != pinned:
            src["path"] = pinned
            changed = True
    if entry.get("installLocation") != pinned:
        entry["installLocation"] = pinned
        changed = True
    return changed

did = False
# Support both dict-of-marketplaces and list-of-marketplaces shapes; only touch 'maw'.
if isinstance(data, dict):
    if "maw" in data and isinstance(data["maw"], dict):
        did = repoint(data["maw"]) or did
    else:
        for v in data.values():
            if isinstance(v, dict) and v.get("name") == "maw":
                did = repoint(v) or did
elif isinstance(data, list):
    for v in data:
        if isinstance(v, dict) and v.get("name") == "maw":
            did = repoint(v) or did

if did:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print("marketplace 'maw' source repointed to the pinned worktree")
else:
    print("marketplace 'maw' source already correct (or no 'maw' entry) — no change")
PY
else
  echo "note: ${MARKETPLACES} not present — skipping marketplace repoint (copy still survives)"
fi

echo
echo "setup complete."
echo "now run: bash ${REPO_ROOT}/maw/repin/validate_plugin.sh"
echo "then launch a FRESH 'claude' session, run /maw:status, and RE-CHECK that the"
echo "cache copy SURVIVES the session start (the durability gate the symlink failed)."
