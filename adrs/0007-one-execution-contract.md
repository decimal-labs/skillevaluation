# ADR 0007 — One execution contract (return to the original design)

**Status:** Accepted (2026-07-15) · schema rev 2 · package 0.6.0

## Context

The original design of this spec (the owner's hand-written brainstorm, 2026-05-24, and its
formalization in the initial commit) had ONE execution shape: run the agent once on the prompt in a
prepared environment; the agent may take many tool steps; record the entire trajectory — tool
calls, token usage, file changes, final response; grade with `expectations` (LLM judge over that
trajectory) plus `script validators` (deterministic code).

Between 2026-06-20 and 2026-07-14 the spec accreted a four-way `mode` type system
(`single_shot`/`agentic`/`explore`/`conversation`) with per-mode grader rules, a user-simulator +
event-labeler + state machine for `conversation`, a runner-computed gold for `explore`, and a
per-case `trials` field aggregated by pass^k. An adversarial per-feature assessment (2026-07-15)
found:

- `agentic` was a **no-op label** — the orchestrator routed it to the byte-identical branch as
  `single_shot`.
- `explore` **violated** the one-contract design rather than extending it: it never ran the real
  agent (a bespoke text protocol with a hardcoded system prompt and four fixed SQLite tools), its
  computed-gold integrity story was illusory (the same author writes the seed AND the gold query),
  and zero real suites used it.
- `conversation` served multi-USER-turn dialogue — a meaning of "multi-turn" the original design
  never had (the design's multi-turn = multi-STEP single invoke) — and reintroduced an LLM at the
  decisive layer (the event labeler) while demoting the judge to display-only.
- per-case `trials` + pass^k made the published number depend on `k` and made `k` an author-chosen
  lever over the headline (raise trials only where the baseline is flaky → inflate the delta).
- No ADR was ever written for the mode system.

## Decision

1. **One execution contract.** Every case: invoke the agent once in a workspace prepared from
   `setup`; the agent may take many tool steps (capped by `max_turns`); record the canonical
   trajectory; grade with `expectations` (judge evidence = the full transcript) + `validators`
   (code, in the workspace, reply staged as `response.txt`/`$RESPONSE_TEXT`). A one-step
   trajectory is a natural outcome, not a type.
2. **Remove** `mode`, `user_goal`, `environment` (computed gold), `simulator`, `policy_check`, and
   per-case `trials` from the schema; the parser rejects them with a targeted migration error.
3. **Repetition is runner-level** (`--runs N`, uniform, capped), aggregated by **MEAN** over all
   (case, run) records: the expected value is independent of N; more runs only narrow error bars.
   pass^k is retired. Flakiness stays visible in per-run records as a secondary signal, never the
   headline.
4. **Policy/conversation skills** are tested as **seeded-transcript cases**: the dialogue-so-far is
   data in the prompt; the agent replies once; validators + expectations grade it. Guardrail:
   seeded histories carry the counterparty's lines and neutral stage directions only — never
   scripted weak agent lines.
5. **`should_trigger` stays** (file format + trigger-only skip disclosure). The menu-selection execution
   contract moves out of this spec to the tooling that owns the router, which consumes the labels
   as ground truth.

## Consequences

- The schema shrinks to the original field set + `max_turns` + `should_trigger`.
- `conversation.py`, `explore.py`, and `runner/trigger.py` are deleted from the reference runner;
  their conformance goldens are retired. `aggregation/runs-mean-over-repeats` pins the MEAN
  semantics.
- What is consciously given up: whole-episode multi-user-turn robustness claims (episode-conjunction
  scoring, adaptive user phrasing). The marketplace does not make that claim; if it ever must, it
  returns as a separate, explicitly-scoped extension — not as a mode inside this spec.
- Suites carrying removed keys fail loudly at parse time with migration guidance; rewriting a suite
  changes its content hash (new `skill-eval://` identity), and historical gate records remain valid
  as history.
