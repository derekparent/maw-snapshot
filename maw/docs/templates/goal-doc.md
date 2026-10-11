# <repo> — <run name> goal (seeded by <who>, <YYYY-MM-DD>)

<!-- Template for a run's goal doc: the contract the next pair's advisor grounds
on. It lives in the handoff dir (`python3 -m opconf handoff_dir`) as
<date>-<repo>-advisor-goal.md.
Delete these comments when you fill it in. -->

## State you inherit
- main `<sha>`; suite `<N>` OK via `python3 tests/run_all.py`.
- Open PRs and who owns each (for example, "the operator merges it; never on an arm").
- Anything parked for the operator, with the finding that explains it.

## Operator rulings this run implements
<!-- State each ruling plainly and cite where the operator gave it (trusted path,
memory ids). Leave the
section out if the run implements none. -->

## Run order (by impact; each lane is one small PR)
1. **<lane>**: what, why, and how it is verified.

## Questions the coordinator may answer

<!-- No coordinator on this install (`maw-run opconf question_to` prints nothing): leave this
section empty. Every question is then the operator's. -->
<!-- REQUIRED (refit R1, DECISIONS.md 2026-09-23 rulings 1 and 2). The seat files
a question as `--class within-goal` ONLY when this list covers it. Everything
else is `operator-only`. The coordinator answers a within-goal question itself and logs it. A
coordinator answer never arms, merges, widens scope, or overrides a safety refusal. -->
- <e.g. naming, file layout, test fixture choices inside a listed lane>
- <e.g. which of two equivalent libraries or flags, when neither changes a boundary>

Always `operator-only`, whatever this list says: money, legal, product or kill
decisions, design taste, scope growth, and anything touching key or trust
material or the merge posture.

## Open questions carried in
<!-- Paste `net.question stage --as <seat>` output from the last run, if any:
each open question with the seat's default. The operator's answers go here before the
pair relaunches. -->

## Traps the last run hit

## Wrap
Target about 30% context left at wrap. File executor field notes and the next
goal doc from this template.
