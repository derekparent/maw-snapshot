"""maw orchestration — the advisor-as-orchestrator loop body and its helpers.

This package holds the DETERMINISTIC core of the Phase-3 advisor loop (the operator's
2026-06-17 vision). The advisor LLM reasons on top of these primitives; the
primitives themselves consume nothing and merge nothing — they GATHER state and
echo plans so the loop's decisions are reproducible and the merge gate stays an
explicit, literal artifact.

  * pulse.py — the advisor PULSE: one no-consume read of inbox / watch / new PRs,
    returning a structured page-or-quiet recommendation.
  * merge_echo.py — the literal ordered merge-plan echo the operator's "Y" is taken against.

Stdlib-only, type-hinted, runner-injectable (gh / git / watch are driven through
an injectable Runner so tests are hermetic).
"""
