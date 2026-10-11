# One Face ID for the morning's pairs (coordinator runbook)

Whose runbook this is: the coordinator's, an optional session an operator may run to arm several
pairs with one Face ID. A single pair on a new install does not need it: its advisor arms with
Touch ID (`/maw:advisor` section 2e, single-seat flow). Everything below needs a coordinator
session and a deployed approval relay.

**Precondition:** the relay Worker serves the batch-capable page. `mint-batch` checks this itself and refuses
when the page is stale. Until the operator deploys it, keep using the single-seat flow at the end of this page.

## The flow

`M=maw-run` (the plugin's `bin/maw-run`: on `PATH` in a session with the plugin enabled, otherwise give its
full path). Every command runs through `$M`, maw's own launcher: a seat
runs these from its target repo, and plain `python3 -m` would let that repo's own `guards/` or `net/` run
instead (#324).

1. **Each advisor, at goal-confirm** (in its own seat, in its own worktree):
   ```
   printf '%s\n' "<bullet 1>" "<bullet 2>" > /tmp/goal.txt        # 1-3 bullets, <= 120 ASCII chars each
   $M guards.session_auth goal-record --file /tmp/goal.txt
   $M guards.session_auth grant-request session.arm --batch --scope "<SCOPE>" > line.json
   ```
   The bullets are the advisor's own words for the goal it accepted. They appear on the operator's card. The scope is
   printable ASCII, at most 200 chars, with `-` for a dash. The advisor sends the coordinator the path of `line.json`.
2. **The coordinator, once it holds every line** (the TTL clock starts here, at most 15 minutes):
   ```
   $M phone_mint.relay_client mint-batch line-maw.json line-layer.json ...
   ```
   It prints the link, the advisor count and a 12-char check code. Send the operator **one** message: the link
   and the check code as the last lines, plus one bullet per repo with the exact scope.
3. **The coordinator polls once, bounded:** `$M phone_mint.relay_client pull-batch --id <id>` waits for the
   signature (every 15s) and stops at the grant's expiry. That stop is why it is allowed when the old "never
   poll" rule forbade polling: it cannot outlive the grant. It writes `~/.maw/grants/batch/<grant_id>.json` (0600).
4. **The coordinator delivers:**
   `$M phone_mint.relay_client deliver-batch --grant ~/.maw/grants/batch/<grant_id>.json --as <coordinator>`
   sends each line's advisor a plain notice over its pair's switchboard (`--net <repo name>`). The notice names
   the file. It carries no attachment and no authority. With under 2 minutes left, deliver-batch refuses:
   re-mint for the seats still unarmed.
5. **Each advisor arms, before anything else:**
   `$M guards.session_auth arm --grant-file <path> --scope "<SCOPE>"`, with the same scope it requested
   (and `--prs` if it requested one). The executor then confirms the start with `peer-status` as usual. Its line
   now shows `goal=<hash>`.

Nobody types "done".

## Rules

- **The grant verifies itself.** A swapped or edited file is refused by the signature or the line match. Never
  "fix" delivery by attaching the grant to a message: recv refuses to act on session grants by design.
- **A line that did not arm stays dead.** A seat restart changes the session id, and an expired line cannot be
  claimed. Recover that seat with ONE single-seat link (below) or Touch ID, never a new batch.
- **A seat that already holds a live token refuses a batch line.** Re-arming is an explicit amend or single-seat
  arm.
- **Keyset alarm:** if any arm reports a CHANGED KEYSET, do NOT run `ack-keyset`. Send the exact error to the
  operator.
- **Delivery finds each advisor on its pair's switchboard `sw-<repo name>`** (the launcher convention), by
  role and session id. A pair on any other switchboard gets "no live advisor", reported and nothing sent:
  fail-closed. Arm that seat by single-seat link.
- **Four junk captures on the link deny the whole batch** (the relay keeps four). If `pull-batch` reports that no
  capture verifies, re-mint.

## Single-seat fallback (unchanged)

```
$M guards.session_auth grant-request session.arm --scope "<SCOPE>" > req.json
python3 -c "import json;json.dump(json.load(open('req.json'))['session'],open('sess.json','w'))"
$M phone_mint.relay_client mint --scope-class session.arm --target advisor --session-file sess.json
$M phone_mint.relay_client pull --id <id> --out grant.json      # once, after the operator signs
$M guards.session_auth arm --scope "<SCOPE>" --grant-file grant.json
```
