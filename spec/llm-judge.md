# LLM Judge Contract

> Status: v1 (current). Schema: [`schemas/judge-result.schema.json`](../schemas/judge-result.schema.json).

This document specifies the **contract** an LLM-based expectation judge must satisfy — input shape, output JSON schema, scoring rubric. It does **not** specify a canonical prompt. Implementations are free to use different prompts as long as the output matches this schema and the rubric.

This is a deliberate design choice. See [`../adrs/0002-llm-judge-vs-script-validator.md`](../adrs/0002-llm-judge-vs-script-validator.md) for why.

## Why a contract, not a prompt

A canonical prompt would lock the spec to a moment-in-time wording that:

- Doesn't survive model upgrades cleanly (a prompt tuned for one model often underperforms on another)
- Forecloses prompt-engineering improvements that implementers might discover
- Makes the spec versioning dependent on prompt iteration

A contract over input/output shape lets implementations iterate the prompt freely, while still producing comparable scores at the per-expectation level. The cost is some inter-implementation variance in marginal cases — a known tradeoff. See the **calibration suite** section below for how this is bounded.

## Inputs to the judge

A conforming judge MUST be given, for each expectation:

| Input | Type | Required | Source |
|---|---|---|---|
| `expectation` | string | yes | A single entry from `eval.yaml`'s case `expectations` list |
| `transcript` | string | yes | The full canonical [trajectory text](./trajectory-format.md) of the agent's session |
| `prompt` | string | yes | The user's original prompt from the case |
| `final_output` | string | yes | The agent's final response text — convenience field; also present in `transcript` |

A judge MAY be given additional context (the SKILL.md body, the case description, tags) but MUST NOT depend on it for correctness — implementations differ in what they pass.

## Output schema

The judge MUST produce, for each expectation, a JSON object matching [`schemas/judge-result.schema.json`](../schemas/judge-result.schema.json):

```json
{
  "expectation": "The response classifies email as PII",
  "passed": true,
  "reason": "The response explicitly labels 'email' as PII in its output JSON.",
  "score": 0.95
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `expectation` | string | yes | Echo of the expectation text being graded |
| `passed` | bool | yes | Did the agent's response satisfy this expectation? |
| `reason` | string | yes | 1–3 sentence prose explanation grounded in observable agent behavior |
| `score` | float in [0, 1] | optional | Confidence in `passed`. Useful for downstream filtering; not used by aggregation |

The reference Python implementation does not require `score` — `passed` alone drives outcomes.

## Scoring rubric

A conforming judge MUST follow these rules:

### What counts as `passed: true`

- The agent's response demonstrably satisfies the claim in the expectation
- The judgment is grounded in observable behavior (text the agent produced, tool calls it made), not speculation about intent
- "The response classifies email as PII" passes if the response output text or tool calls clearly classify it as PII; fails if email is omitted, miscategorized, or only mentioned in passing

### What counts as `passed: false`

- The response contradicts the expectation
- The response does not address what the expectation is about
- The response is empty, errored, or refused

### Edge cases the rubric MUST handle consistently

| Case | Verdict | Reason |
|---|---|---|
| Expectation phrased in the negative ("response does NOT mention X") and X is absent | `passed: true` | Absence is the assertion |
| Agent gave a correct answer with extra unrelated content | `passed: true` | Extra is not wrong |
| Agent gave a partial answer that satisfies the literal claim | `passed: true` if the claim is technically met | Don't penalize narrowness |
| Agent refused for safety reasons | `passed: false` for outcome accounting, but `reason` SHOULD note the refusal | Refusals are a real signal but not a pass |
| Agent errored (tool failure, sandbox crash) | `passed: false`, AND the outer per-arm `errored=True` SHOULD be set | Distinguish "skill failed" from "infra failed" |
| JUDGE transport errored (judge API down, timeout) | `passed: false` AND `errored: true` on the result dict; the runner MUST roll it up into the arm's `errored` so the case outcome is `error` | An ungraded expectation is not a model fail — a judge outage must be excluded from lift, or it manufactures flips in whichever direction the outage lands |
| Ambiguous case where humans could reasonably disagree | Pick one and explain | Don't return null; consistency beats correctness here |

### What `reason` MUST contain

A 1–3 sentence explanation that:

- References specific evidence from the transcript (a tool call, a phrase from the response, an absence)
- Avoids speculation about agent intent
- Is short enough to fit in a list UI

Anti-patterns: "The agent did a good job" (no evidence); "I think the agent understood the task" (intent speculation); a full essay (too long).

## Calibration suite (recommended, not required)

To bound inter-implementation variance, conformant implementations SHOULD test their judge against the calibration set under [`../compatibility-tests/judge/`](../compatibility-tests/judge/) (when populated). Each fixture is:

- `input.json` — `{expectation, transcript, prompt, final_output}`
- `expected.json` — `{passed, reason_keywords: [...]}` (substring matches; not strict text match)

A judge that scores ≥95% agreement with the calibration set on `passed` is considered well-calibrated. This is a quality signal, not a strict conformance gate — the strict conformance bar is the schema and the rubric rules above.

## Determinism (recommended)

A conforming judge SHOULD use:

- Temperature 0 (or the implementation's nearest equivalent)
- A fixed seed if the model exposes one
- A pinned model version (don't accept `claude-latest`)

Determinism makes repeat runs comparable. It is RECOMMENDED, not REQUIRED — for very large-scale benchmarking, some implementations may sample multiple times and majority-vote.

## Implementation notes (informative)

The DecimalAI platform's reference implementation:

- Uses the configured judge model (`llm.judge.model`) for its expectation judge, recorded on each run
- Wraps the rubric above into a single-shot prompt
- Sets temperature 0
- Logs the prompt hash with each result for auditability

> **Two-category model (2026-06-13):** `expectations` are **always** LLM-judged — there is no
> structural short-circuit. Deterministic/exact-match checks belong in `validators` (graded by
> code; see `eval-yaml.md`). This keeps "how was this graded" unambiguous per check and makes the
> grading method publicly displayable (code vs judged-by-model-X). `try_structural_assertion`
> remains a public helper for *authoring* structural validators, not a grading path.

None of these are required by the spec. They are example implementation choices.

## What's NOT in this contract

- **Which LLM to use** — implementations choose
- **The literal prompt** — implementations choose
- **Pricing / token budget** — implementations choose
- **Caching strategy** — implementations choose
- **Retry / fallback behavior** — implementations choose

## See also

- [`eval-yaml.md`](./eval-yaml.md) — where `expectations` are authored
- [`runner-contract.md`](./runner-contract.md) — how judge results feed into per-arm `passed` and case outcomes
- [`trajectory-format.md`](./trajectory-format.md) — the canonical transcript format the judge consumes
