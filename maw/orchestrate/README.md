# Orchestration helpers

Two small, deterministic helpers for the advisor's supervision loop. Both are
stdlib-only and take an injectable runner, so their tests need no `gh`, git or
live session. Neither consumes a turn and neither merges anything: they gather
state and render text, and the advisor decides what to do with it.

- **`pulse.py`**: one read-only look at the pair. `pulse(home, runner)` gathers
  the advisor's unconsumed bridge inbox (count, senders, and whether a turn
  looks like a blocked summary), the `net.watch` findings, and the pull
  requests opened since a saved cursor. It returns a `Pulse` with
  `page: bool` and `reasons`. `page` is true when a blocked-summary turn is
  waiting, the executor is dark or frozen, an orphaned draft is blocking a
  pane, or a merge is pending. Run it with `maw-run orchestrate.pulse`.
- **`merge_echo.py`**: renders an ordered `#<n> — <title>` list of pull
  requests, closed by a fixed confirmation line. It dates from the time when
  the operator confirmed each merge batch by hand. The merge gate no longer
  asks for that by default (`guards/session_auth.py` arms once per session).
  No command calls this helper now. It is kept for a repository that opts
  back in to the echo.

`commands/goal.md` and `commands/advisor.md` describe the loop these serve.

This package does not dispatch agents. An earlier note here promised a port of
a separate multi-agent workflow into this directory. That port did not happen
and is not planned for this snapshot.
