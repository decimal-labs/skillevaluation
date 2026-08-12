# ADR 0001 — A/B benchmarking, not absolute scoring

**Status:** Accepted (2026-05-28)

## Context

A skill eval suite needs to answer: "does this skill help an agent?" There are two ways to interpret that question:

1. **Absolute:** "Does the agent pass the test when the skill is loaded?" — a single pass-rate number per skill
2. **A/B:** "Does the agent do better with the skill loaded than without?" — a delta between two runs

These produce very different signals for the same suite.

## Decision

`skillevaluation` is an **A/B benchmark** spec, not an absolute scoring spec. Every test case is executed twice — once with the target skill in the agent's manifest, once without — and the run-level metrics are deltas.

## Why

**Absolute scoring conflates skill quality with model capability.** When Claude Sonnet 4.6 can already pass 90% of a suite without the skill loaded, an absolute pass rate of 95% tells you almost nothing about the skill — the agent would do nearly as well without it. The same suite run against an older model might show 30% absolute, which looks worse but actually reveals a more useful skill (it's doing real work).

The number that matters for a catalog claim is "the skill helped the agent on X cases it would have otherwise failed" — that's `flip_to_pass`, which only exists in an A/B framing.

**Without an A/B baseline, the spec cannot detect regressions.** A skill that hurts the agent (`flip_to_fail`) looks identical to a skill that helps it (`flip_to_pass`) under absolute scoring — both just produce a `passed` row. The A/B framing makes regressions a first-class outcome.

**The cost is real but bounded.** Running each case twice doubles wall-clock and compute. The spec mitigates this with **baseline caching** (see [`0003-baseline-cache-key.md`](./0003-baseline-cache-key.md)) — the without-skill arm is independent of the skill and can be reused across runs on the same manifest.

## Alternatives considered

**Absolute only.** Rejected — would not detect regressions, conflates skill quality with model capability, no useful catalog headline number.

**Optional A/B (default absolute).** Rejected — every spec consumer would have to special-case the two modes. Pinning to A/B keeps the runner contract and outcome taxonomy simple.

**A/B + absolute side-by-side.** Rejected for v1 — adds spec surface without clear demand. May revisit in v2 if there's a use case for absolute scoring (e.g., "the agent should pass this regression suite regardless of whether the skill is loaded").

## Consequences

- The outcome taxonomy has exactly 5 states, all framed as A/B comparisons ([ADR 0004](./0004-outcome-taxonomy.md))
- The aggregate metric is a delta, not a level (`delta_pts`, `delta_pct`)
- Conforming runners MUST execute each case twice
- The `task_attempted` field exists specifically to handle apples-to-oranges A/B cases (when one arm gives up immediately)
