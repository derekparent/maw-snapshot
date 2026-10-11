# Seat watch recipes (the truthful version)

Issues #344 (watch lifetime), #347 (missed turns), #357 (watchers while blocked).
Seat commands (`executor.md` 1d/1e, `advisor.md` 4) point here for the details.

## 1. What a Monitor actually is

- **A Monitor watch lasts at most 30 minutes.** The tool takes a required
  `timeout_ms` (omitting it is an `InputValidationError` that burns the call, #150),
  capped at 1800000. There is **no `persistent` parameter**: older seat docs and the
  SessionStart hook promised a session-long watch, and no such thing exists.
- **Re-arm on every expiry.** When the harness says the Monitor timed out, start the
  same command again, with `timeout_ms: 1800000`. Expect two re-arms an hour for a
  seat with two watches (inbound + pair-sweep). A re-arm does not repeat old
  pair-sweep wakes: `net.watch --wake` keeps its seen-set on disk (section 3), so a
  finding that was already announced and still persists stays quiet in the new watch.
- **Zero-token alternative: a background one-shot.** Run the same loop with
  `Bash` + `run_in_background: true` and make it exit after its first event. (How
  long the harness lets a background command run is UNVERIFIED: an earlier draft of
  this page said its `timeout` goes up to 7200000, two hours, and nobody has
  confirmed that. Do not rely on a figure. If a one-shot ends without an event,
  treat it as an expiry and start it again.) The harness re-invokes you when it exits, you
  handle the event, then start the one-shot again. The inbound watcher has this built
  in: `maw-watch-inbox <name> --once`. For the pair-sweep loop, use the one-shot
  form in section 3, which exits on the first wake or dead-man line. Quiet time
  costs nothing either way.

## 2. Inbound watch (bridge AND wire): `maw-watch-inbox`

```bash
maw-watch-inbox <your-name>            # Monitor, timeout_ms 1800000
maw-watch-inbox <your-name> --once     # background one-shot
```

It polls `net recv <your-name> --check`, which is non-consuming and sees BOTH the
durable bridge and the wire (a coordinator ANSWER or a parked send arrives on the wire
only). It prints one line per event:

- when a turn waits that did not wait at the last poll, so a second turn that lands
  behind the first is announced (the old edge-only loop latched and stayed silent).
  It compares the waiting turn **ids**, not the count: your own consume (the count
  goes down) prints nothing, and a turn that lands in the same poll you consumed
  another in (the count stays level) is still announced, and
- again every ~5 minutes while anything still waits.

Rules that fix the missed-turn classes:

1. **Do not pipe a watcher** through `grep`, `awk`, `head` or `tr`. A pipe buffers,
   and a bridge watch piped through grep delivered its event 22 minutes late.
   `bridge.cli watch` and `maw-watch-inbox` already print one line per event.
2. **Branch on the exit code of `recv --check`, never on its text.** Exit `0` means
   turns wait (`PENDING <n> turn(s) waiting for <name> (bridge:b, wire:w) ids=...`), exit `3`
   means none (`IDLE nothing waiting for <name>`), any other code is an error, not
   "empty". The idle line no longer contains the word `unconsumed`, because a
   `grep unconsumed` filter fired on it.
3. **Do not use `bridge.cli watch` as your only inbound watch.** It reads the bridge
   file alone and never sees a coordinator wire turn (one sat parked 11 minutes). Use
   `maw-watch-inbox`. `bridge.cli watch` stays valid as a reconciling bridge-only
   watcher (`/maw:bridge-watch`).
4. **`/maw:bridge-in` prints one turn at a time and its output can truncate.** After
   a wake, drain until `recv --check` exits 3, and re-run `bridge.cli in` if a body
   looks cut off.

## 3. Pair-sweep watch

The loop lives inline in `executor.md` 1e and `advisor.md` 4:

```bash
while true; do
  sleep 45
  maw-run net.watch --for <your-name> --wake 2>/dev/null
  maw-run net.deadman check --for <your-name> 2>/dev/null
done
```

`net.watch --for <your-name> --wake` is one loop step (#346). One sweep prints a
header plus the detail of every NEW unacked finding for your pair, so a header
never arrives without its detail, and it prints nothing for a finding this seat
was already woken for.

- **The seen-set is persisted on disk** (`~/.claude/bridge/switchboard/watch-seen/<your-name>.json`, or under `$MAW_NET_DIR`,
  tied to your seat's registration). A Monitor re-arm starts a fresh shell but
  reads the same file, so it does not repeat a wake for a finding that persists,
  and acking one finding never re-announces another. A finding that clears and
  recurs wakes you again, and so does an acked finding whose ack lapses.
- **Exit codes:** `1` on a wake (it printed new findings), `0` when there is
  nothing new, `2` on a usage error (`--wake` without `--for`). Branch on the
  code, never on the text.

Background one-shot form (exits on its first event; start it again after
handling it):

```bash
while true; do
  sleep 45
  maw-run net.watch --for <your-name> --wake 2>/dev/null; rc=$?
  esc=$(maw-run net.deadman check --for <your-name> 2>/dev/null)
  [ -n "$esc" ] && echo "$esc"
  if [ "$rc" -ne 0 ] || [ -n "$esc" ]; then exit "$rc"; fi
done
```

## 4. While blocked on the operator

The goal doc's "no watchers while waiting" means **no watcher that waits on the
operator** (no `/loop`, Monitor, ScheduleWakeup or sleep-poll on a Face ID link, an
answer or an arm). It does not touch peer supervision. Keep exactly two watches
armed through a block and nothing more: the inbound watch (section 2) and the
pair-sweep. Both watch the OTHER SEAT. Do not add seat-to-seat bridge watches
beyond those two.
