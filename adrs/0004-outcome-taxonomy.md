# ADR 0004 — Five outcome strings, frozen

**Status:** Accepted (2026-05-28)

## Context

Every benchmark case produces a classification of how the A/B turned out. The classification surfaces in UIs, APIs, regression reports, and catalog displays.

Options considered:

1. **A single `passed` bool** — too coarse, loses regression vs. non-discriminating distinction
2. **A 4-state enum** — `flip_to_pass`, `pass_kept`, `fail_kept`, `flip_to_fail` — covers the A/B math
3. **A 5-state enum** — adds `error` for "couldn't evaluate"
4. **An open-ended status string** — unbounded, hard to filter in UIs
5. **A numeric score in [0,1]** — loses the discrete-state semantics ("did the skill rescue?")

## Decision

`skillevaluation` uses a 5-state outcome enum, with these exact strings:

| String | Meaning |
|---|---|
| `flip_to_pass` | The skill rescued the agent (the "win") |
| `pass_kept` | Both arms passed (skill didn't help on this case) |
| `fail_kept` | Both arms failed (skill didn't rescue) |
| `flip_to_fail` | The skill HURT (regression) |
| `error` | Couldn't evaluate (transient failure) |

The strings MUST be lowercase with underscores, exactly as above. They are **frozen** — they ship in user-facing UIs and external APIs, and renaming would break every consumer.

## Why these five

**`flip_to_pass`** is the headline. Catalog cards show "the skill rescued 17 of 50 cases." Marketing copy depends on this distinction.

**`flip_to_fail`** is the regression marker. Without it, a skill that hurts the agent is indistinguishable from a skill that doesn't help. This is the first-class regression signal — a skill with any `flip_to_fail` in its run is a candidate for rollback.

**`pass_kept`** and **`fail_kept`** look similar but report different facts. Many `pass_kept` means the suite isn't discriminating (the model is good enough without the skill — author should add harder cases). Many `fail_kept` means the suite IS discriminating but the skill isn't strong enough.

**`error`** distinguishes "the skill failed" from "the infrastructure failed." Without it, a transient LLM API outage looks like a skill regression. The `error` outcome excludes the case from aggregate lift (see [`spec/runner-contract.md`](../spec/runner-contract.md)), preserving headline integrity.

## Why frozen

The strings ship in:

- DecimalAI's catalog leaderboards
- The DecimalAI SDK's CLI output (`decimalai skills benchmark`)
- Webhook payloads to external eval-tracking platforms
- JSON wire formats stored in customer databases

Renaming `flip_to_pass` to `regression_rescued` (or whatever) would silently break every consumer. The cost of locking these names at v1 is much smaller than the cost of breaking compatibility later.

## Why not numeric scores

A numeric score in [0,1] loses the categorical semantics. "0.7" doesn't tell you whether the skill rescued the agent or just both arms partially passed. The discrete states preserve the questions consumers actually want to ask:

- "How many `flip_to_pass`?" (the win count)
- "Any `flip_to_fail`?" (any regressions?)
- "Many `pass_kept`?" (suite not discriminating)

## Why not open-ended status strings

Bounded enums enable:

- UI filtering ("show me only `flip_to_fail` cases across all my agents")
- Pre-publish gates ("must have zero `flip_to_fail`")
- Cross-implementation comparison

An open-ended status would force consumers to handle unknown values defensively, which leaks complexity everywhere.

## Consequences

- The 5 strings are frozen at v1.0 (one of the few backward-incompatibility commitments the spec makes)
- A 6th outcome would require a v2 schema migration
- Runners MUST classify every case as exactly one of the 5 — there is no "unknown" or "skipped"
- The `error` outcome is the ONLY way to handle infrastructure-level failure; runners should not fabricate `fail_kept` or similar when the eval was never actually run
