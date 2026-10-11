# Tools awareness — computer-use eyes + Xcode MCP

*Doctrine + enablement for the two on-demand tools maw surfaces at seat
bring-up.*

## The problem this solves

MCP tools are frequently **deferred / loaded-on-demand**. A session that holds
`computer-use` or an Xcode MCP server often does not *know* it has them — the
tool schema isn't loaded until something forces it, and an agent that doesn't
know a capability exists will not reach for it. The result is a reviewer who
signs a UI verdict off the builder's screenshots because it never occurred to
them they could look themselves, or a builder who hand-waves "it builds" without
ever running the app.

maw closes the gap with a **SessionStart hook** (`maw/hooks/tools-awareness.sh`)
that injects a short, actionable awareness line on every session — and a
stack-aware second line in iOS repos. The hook is fail-open (POSIX sh, every
path exits 0): it can never block or fail a session start.

## computer-use — the visual eyes

`computer-use` is the agent's eyes: screenshot the screen, drive mouse/keyboard,
and **visually verify RENDERED UI**. For the advisor it is the load-bearing one:
it is the reviewer's **INDEPENDENT** check for the non-reproducible-UI gap
(**the ADVISOR-PLAYBOOK non-reproducible-oracle gate**). Instead of signing a UI verdict on the builder's
screenshots — which can be stale, cropped, or from a different build — the
advisor can `request_access` for the app (e.g. the iOS Simulator) and
`screenshot` it directly. Pair it with the **build-from-SHA provenance gate** so
the app under your eyes was built from the exact commit under review.

**Timing (refit R2, the operator 2026-09-23): headless first; computer-use only in a seat's
FIRST turn.** Computer-use app approval is per session. It cannot be
pre-approved, Remote Control does not forward it, and bypass mode does not skip
it. A seat that asks for it after the operator has left stops on a prompt nobody can
answer. So:
- **Default to headless**: `xcrun simctl launch <udid> <bundle> <launch args>`
  to force the state, `xcrun simctl io booted screenshot <file>` to capture
  it, and XCUITest for taps. No prompt, and it works at night.
- **If the run needs computer-use**, the seat calls `request_access` in its FIRST turn,
  while the operator is at the Mac arming, so he approves both together.
- **After the operator has left**, a visual check you cannot do headlessly is a PARKED
  lane (file it with `net.question ask`, `/maw:goal` §3b). Never sit on a prompt.

**Enable steps:**
1. **Grants.** computer-use needs macOS **Accessibility** + **Screen-Recording**
   grants for the host app. If it is unresponsive, that is almost always a
   missing grant — fix it in System Settings → Privacy & Security.
2. **request_access flow.** Before any computer-use action, call
   `request_access` with the list of applications you need; the user approves
   each explicitly. You may call it again mid-task when you discover you need
   another app.
3. **Tier limits.** Apps are granted at a tier by category:
   - **Browsers** → `read` tier: visible in screenshots, but clicks/typing are
     blocked. Use the claude-in-chrome MCP for web navigation.
     A black or stale Chrome screenshot usually means a backgrounded tab; see
     gate 11(f) in the ADVISOR-PLAYBOOK.
   - **Terminals / IDEs** → `click` tier: visible + left-clickable, but typing /
     key-presses / right-click are blocked. Use the Bash tool for shell work.
   - **Everything else** (native apps, the Simulator) → `full` tier.

When grants are **unavailable**, the **state-forcing harness** is the grant-free
fallback (force the app into the state under test deterministically rather than
driving the UI by eye). computer-use is the *direct* eyes when grants land; the
harness is the substitute when they don't. Both exist; neither replaces the
other.

## Xcode MCP — the iOS builder's tool

In an iOS/Xcode repo, the **Xcode MCP** server is the builder's tool: build and
run the app in an iOS/Xcode environment (an iOS app repo). The tools-awareness
hook only surfaces this line when the repo is iOS/Xcode-shaped — detected via
`init.stack_detection.is_xcode_env`, which is True for an `.xcodeproj` /
`.xcworkspace`, `Package.swift`, a `Podfile`, or any `.swift` sources.

**Enable steps:** if the Xcode MCP server is not connected, connect it via
`/mcp`. Then self-check the rendered build headlessly (`xcrun simctl io booted
screenshot`), or with computer-use when it was approved in the first turn,
before requesting review.

**Which build is actually installed? — pin `-derivedDataPath`, verify by
provenance, never spelunk the binary.** An executor on an iOS repo burned a cycle testing a
months-old stale build: `strings` on the binary does NOT surface SwiftUI string
literals (so a source change appears "missing" in a binary that in fact never had
it), and `find DerivedData -name '*.app' | head -1` grabs whatever old artifact
sorts first. The binary is not a reliable witness to which SHA it was built from.
The canonical check:

1. **Build with an explicit `-derivedDataPath`** (e.g.
   `xcodebuild ... -derivedDataPath ./build`) so the artifact path is
   deterministic and yours, not a shared DerivedData lottery.
2. **Install the app from THAT path** — not from a `find`-the-newest guess.
3. **Verify via the build-from-SHA provenance stamp (`/maw:provenance`)**, not by
   `strings`/binary inspection. Provenance answers "was this artifact built from
   the SHA under review?" directly; binary spelunking cannot.

This is the iOS instance of the ADVISOR-PLAYBOOK build-from-SHA provenance gate:
a UI verdict is only as good as the guarantee that the app under your eyes was
built from the commit you are reviewing.

## The supervision surface the operator relies on

The side-by-side **tmux pair** (`maw-seat pair`) is the supervision surface: the
panes where the operator watches the advisor/executor pair work, each seat in its
own worktree. These tools change *what the seats can see and do*; they do **not**
change the pair's layout or how it is launched. Preserve them.

## Running maw helpers — mind the tree that shadows `net`

Run `python3 -m net.*` / `guards.*` helpers from the **repo root or a fresh
tree**, not a stale sub-checkout. A stale checked-out tree on `PYTHONPATH` (e.g.
a seat sitting in a `maw/` subdir with an old copy of the package) **shadows**
the deployed `net` package, so the helper runs old code and reads runtime state
from the wrong root (this bit the live `net.board` gate-10 check, 2026-07-21).
Run it through the plugin's own launcher, `maw-run <module>` (`bin/maw-run`,
on `PATH` in a session with the plugin enabled), from the repo root.

## Small tool notes

- **`maw-run`**: `maw/bin/maw-run <module> [args]` runs `python3 -m <module>` with the
  plugin root on `PYTHONPATH` (e.g. `maw-run net.cli who`), so seats stop hand-setting it.
- **claude-in-chrome cannot open `file://` URLs.** To view a local HTML render, serve
  its directory (`python3 -m http.server --bind 127.0.0.1 8765` from that dir), open
  `http://127.0.0.1:8765/<file>`, then stop the server.

## Where it all wires together

- **Hook:** `maw/hooks/tools-awareness.sh`, registered under SessionStart in
  `maw/hooks/hooks.json` (separate from `probe-capabilities.sh` on purpose — a
  stateless always-on reminder, not a TTL'd probe gate).
- **Detection:** `init.stack_detection.is_xcode_env(root)`.
- **Seat doctrine:** the advisor's "Your eyes" note (`maw/commands/advisor.md`)
  and the executor's "iOS tooling" note (`maw/commands/executor.md`).
- **Review gate:** the ADVISOR-PLAYBOOK non-reproducible-oracle gate is the *why*;
  computer-use is the *how* when grants land, the state-forcing harness when they
  don't.
