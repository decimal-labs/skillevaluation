# Runner Contract

> Status: v1 (current). Reference implementation: `skillevaluation.outcomes`, `skillevaluation.aggregation`, `skillevaluation.baseline`.

This document defines what a conforming runner MUST do to execute a `skillevaluation` test suite. Conformance is verified by the golden in/out pairs under [`../compatibility-tests/`](../compatibility-tests/).

## Inputs

A conforming runner takes:

1. A parsed [`eval.yaml`](./eval-yaml.md) suite
2. A way to invoke an agent with a manifest A (with-skill) and a manifest B (without-skill)
3. A workspace (working directory) the agent operates in

The spec does **not** specify how the agent is invoked, what runtime hosts it, what tools it has, or what LLM it uses. Those are implementation choices.

## The A/B execution loop

For each case in the suite, a conforming runner MUST:

0. **Skip trigger-only cases with disclosure** — a case whose only assertion is `should_trigger` (see [`eval-yaml.md`](./eval-yaml.md)) is not an A/B case: exclude it from the loop AND from `total_cases`, and report the count as `cases_skipped_trigger_only` (see "Trigger cases" below). Erroring on it is non-conforming.
1. **Prepare the workspace** — write each `setup.files` entry (mapping form, spec 0.3.0) into the workspace first, rejecting any path that escapes it, then execute each `setup` command sequentially. If any setup command fails (non-zero exit), the case outcome is `error`; do not proceed.
2. **Execute the agent twice**:
   - **with-skill arm** — invoke the agent with manifest A (the skill loaded)
   - **without-skill arm** — invoke the agent with manifest B (the skill not loaded)
3. **Score the with-skill arm**:
   - Run the LLM judge against each `expectations` entry (see [`llm-judge.md`](./llm-judge.md))
   - Run each `validators` entry as a shell command with `cwd=<workspace>`, comparing exit code to `expect_exit_code`. Before validators run, the arm's final output MUST be staged as `<workspace>/response.txt` and exported as `$RESPONSE_TEXT`, so validators can grade the artifact the model emitted
   - The with-skill arm `passed` iff **every** expectation and **every** validator passed
   - A validator that produced **no verdict** — a non-binary undeclared exit code (the grader broke), a wall-clock **timeout**, or a **spawn failure** — MUST be recorded `passed: false` AND `errored: true`, and the runner MUST roll that up into the arm's `errored` so the case classifies as `error` and is excluded from aggregate lift. An ungraded check is not a model failure: validator timeouts are measured per arm, so scoring one as a fail lets a grader that is merely slower on one arm's longer output manufacture a flip. (Mirrors the judge-transport rule in [`llm-judge.md`](./llm-judge.md).)
4. **Score the without-skill arm** — same as step 3, but against the without-skill agent's output
5. **Classify the case outcome** (see below)
6. **Capture metrics** for each arm (also below)

A conforming runner SHOULD parallelize the two arms when possible — they're independent. For the same reason, the arms SHOULD be **isolated** from each other: when the agent can write files, give each arm its own workspace (prepared from the same `setup` steps) so one arm's artifacts are neither readable by the other arm nor graded by its validators. The reference runner prepares one workspace per arm.

## Outcome taxonomy (5 outcomes)

The case outcome is **derived** from three booleans: `with_passed`, `without_passed`, `errored`. The mapping is total and deterministic:

| `errored` | `with_passed` | `without_passed` | Outcome | Meaning |
|---|---|---|---|---|
| true | * | * | `error` | At least one arm failed to execute (transient API failure, setup failure, sandbox crash) |
| false | true | false | `flip_to_pass` | The skill rescued the agent — the "win" outcome |
| false | true | true | `pass_kept` | Both arms passed; the skill didn't help on this case |
| false | false | false | `fail_kept` | Both arms failed; the skill didn't rescue |
| false | false | true | `flip_to_fail` | REGRESSION marker — the skill HURT |

Reference implementation: `skillevaluation.outcomes.classify_outcome`.

The five strings MUST be lowercase with underscores, exactly as above. These strings ship in user-facing UIs and external APIs.

## Per-arm metrics

For each arm, a conforming runner MUST capture:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `passed` | bool | yes | All assertions passed |
| `task_attempted` | bool | yes | Did the agent meaningfully engage with the task? (See apples-to-oranges rule below.) |
| `duration_ms` | int | recommended | Wall-clock duration of the agent invocation |
| `turns` | int | recommended | Number of agent turns / steps |
| `total_tokens` | int | recommended | Total tokens consumed (input + output) |
| `tool_call_count` | int | recommended | Number of tool invocations |
| `errored` | bool | yes | Did this arm fail to execute? |

`task_attempted` defaults to `true`. Set it to `false` only when the without-skill agent gave a "punt" response that doesn't engage with the task — e.g., a 73-token "I don't know how to do that" reply for a task that with-skill produces a 4000-token structured analysis. This boolean drives the apples-to-oranges skip rule below.

A runner MAY capture additional fields beyond these. Implementations MUST tolerate unknown fields when consuming results from another implementation.

## Baseline caching

The without-skill arm is expensive and **independent of the skill being tested**. A conforming runner SHOULD cache the without-skill trajectory per `(skill, case)` pair and reuse it across repeat benchmark runs on the same agent manifest.

The cache key is derived as a 16-character hex digest of:

```json
{"skill": "<skill_id>", "case": "<case_id>", "prompt": "<case.prompt>"}
```

JSON serialized with `sort_keys=True`, SHA-256 hashed, first 16 hex chars.

For a run executed with `--runs N > 1` (rev 2, runner-level repetition), each run caches independently: run 0 uses the key above unchanged (byte-identical to the single-run key — no cache churn), and runs `1..N-1` add a `"trial": <n>` field (the historical wire name for the index). Runs are i.i.d. rollouts, so sharing one key across them would freeze the without-arm to run 0's output and understate its variance.

Reference implementation: `skillevaluation.baseline.baseline_cache_key`.

When the manifest changes (the agent's tools, model, system prompt, etc.) the cache MUST be invalidated. The simplest correct strategy is to scope the cache by `agent_manifest_id` in addition to the key above.

## Run-level aggregation

After all cases complete, a conforming runner MUST produce a run-level aggregate matching `schemas/test-run-result.schema.json`. The aggregate has two parts:

### Pass rate

Pass-rate delta is in **percentage points**, not percent:

```
with_skill   = aggregated_with_pass / aggregated_n
without_skill = aggregated_without_pass / aggregated_n
delta_pts    = (with_skill - without_skill) * 100
```

The aggregate divisor `aggregated_n` uses **only** cases that contributed to aggregation — see the skip rule below.

**Degenerate runs (`aggregated_n == 0`).** When every case errored or was skipped by the apples-to-oranges rule, the run measured nothing — there is no honest pass rate. A conforming runner MUST emit `with_skill`, `without_skill`, AND `delta_pts` as **`null`** in that case, NOT a fabricated `0`. (`cases_aggregated == 0` is the signal; consumers render "N/A", not "0% / +0 pts".) This mirrors the `delta_pct: null` rule for the per-dimension deltas below, and keeps the headline from contradicting `tokens`, which already goes null on the same run.

**Error-dominated runs (the >25% floor, spec 0.3.0).** When **more than 25%** of a run's cases have outcome `error` (`errors / total_cases > 0.25` — e.g. a provider 503 storm), the run is not a valid measurement even if the surviving minority scored cleanly. A conforming runner MUST:

- emit `error_dominated: true` on the aggregate (`false` otherwise; a zero-case run is `false` — the degenerate-run rule already covers it),
- emit the headline `pass_rate.delta_pts` as **`null`** (the lift claim is withheld; the per-arm `with_skill`/`without_skill` rates and every disclosure count stay populated),
- report the run verdict as `error` (see the verdict table below), so no CI gate or consumer reads a pass off an invalid run.

Exactly 25% errored does NOT trip the floor — the comparison is strictly greater. Reference constant: `skillevaluation.aggregation.ERROR_DOMINATED_FLOOR`.

**Rounding (normative).** Every emitted numeric (`with_skill`/`without_skill` to 4 places, `delta_pts` to 2, the per-dimension `delta_pct` to 1, `with_skill_avg`/`without_skill_avg` to 2) MUST be **rounded half away from zero on the decimal value**, not via IEEE-754 binary round-half-even. A `.5` tie (e.g. `delta_pct = 2.45`) must round up to `2.5` regardless of the implementation language; compute over the decimal value (e.g. `Decimal(str(x)).quantize(...)`) so a binary-float `printf` can't drift the last digit.

### Per-dimension deltas

For each of `duration_ms`, `turns`, `tokens`, `tool_calls`, compute a signed percentage delta:

```
with_avg     = sum(with_skill_<dim>) / aggregated_n
without_avg  = sum(without_skill_<dim>) / aggregated_n
delta_pct    = ((with_avg - without_avg) / without_avg) * 100
```

- **Negative delta** = with-skill is lower (good for `turns`/`tokens`/`duration` where lower is better)
- **Positive delta** = with-skill is higher
- **When `without_avg == 0`** → `delta_pct` is `null`. Do **not** fabricate a value. Consumers MUST surface honestly.

The raw deltas above are computed over all aggregated cases. They are kept for backward compatibility,
but a "fast but wrong" arm can inflate them — so they are **not** the number an efficiency leaderboard
should rank on. Use the correctness-gated deltas below instead.

### Correctness-gated efficiency deltas (the honest efficiency number)

For each dimension a conforming runner MUST **also** emit a `<dim>_correctness_gated` delta computed
over **only the cases where BOTH arms passed** (Cost-of-Pass): `duration_ms_correctness_gated`,
`turns_correctness_gated`, `tokens_correctness_gated`, `tool_calls_correctness_gated`, plus
`cases_correctness_gated` (the both-passed divisor). Comparing turns/tokens when the arms disagree on
correctness lets a wrong-but-fast answer fake a saving; gating on both-correct is the only honest basis
for an efficiency claim. Same shape as the raw deltas (`with_skill_avg` / `without_skill_avg` /
`delta_pct`, with `delta_pct: null` when the gated baseline average is 0, i.e. no both-correct case).

### Repetition: runner-level `runs` (MEAN-aggregated) — rev 2

A conforming runner MAY execute every graded case `N` times per arm (`--runs N` in the reference
CLI; a run-level parameter on hosted runners — **never** a per-case eval.yaml field). Each run is an
ordinary A/B record sharing the case's `name`; the aggregate rates are **means over all
(case, run) records**, so the expected value of every reported number is independent of `N` — more
runs only narrow the error bars. When `N > 1` the runner MUST disclose it (the optional top-level
`runs` integer in the results document) so a reader knows the rates are averaged.

Rationale (ADR-0007): the rev-1 pass^k AND-fold made the published number depend on `k`, and the
per-case `trials` field made `k` an author-chosen lever over the headline (raising trials only on
cases where the baseline is flaky inflates the delta). Uniform runner-chosen repetition with MEAN
aggregation removes the lever entirely; flakiness remains visible in the per-run records and MAY be
surfaced as a secondary consistency statistic, never the headline.

Reference implementation: `skillevaluation.aggregation.compute_run_aggregates`.

## Trigger cases (spec 0.3.0 file format; rev-2 posture: disclosure only)

Trigger evaluation grades a different question than lift — *does the skill surface on the right
prompts?* — and belongs to platform-side routing tooling, not this spec's runner. The FILE format
(`should_trigger`, see [`eval-yaml.md`](./eval-yaml.md)) is normative; the rev-1 menu-selection
execution contract and its `trigger_metrics`/`trigger_cases` wire shape are
**removed from this spec** (ADR-0007) — platforms that grade routing keep doing so with their own
tooling, consuming the boolean labels as ground truth.

What remains normative for a conforming A/B runner:

- A **trigger-only** case (`should_trigger` present, no grader) is NEVER an A/B case: the runner
  MUST NOT error on it; it excludes it from the loop and from `total_cases`, and emits the count
  as `cases_skipped_trigger_only` in the run document. Burying the count is a conformance
  violation.
- A **composed** case (`should_trigger` + graders) runs in the A/B loop as usual; the boolean is
  carried as data for platform tooling.

## The apples-to-oranges skip rule

A case is **excluded from aggregation** (but still persisted as an outcome) if either:

1. The case outcome is `error`, OR
2. Either arm's `task_attempted` is `false`

The reason: an empty-output "I don't know" response on the without-skill arm doesn't represent a fair baseline. A 20-token punt vs a 4000-token structured analysis would inflate token deltas to nonsense values like +2461%. Excluding from the aggregate divisor keeps the headline number honest.

The aggregate result MUST include disclosure of what was filtered:

```json
{
  "errors": 2,
  "cases_aggregated": 46,
  "cases_skipped_apples_oranges": 2,
  "total_cases": 50
}
```

Conforming runners MUST surface these numbers — burying the skipped count is a conformance violation. Authors and consumers depend on knowing how the headline was computed.

## Run verdict

The outcome classifier's verdict is a pure function of the per-case outcomes
(this table, and only this table, is what the `verdict/` conformance goldens pin):

| Condition | Verdict |
|---|---|
| Every case errored | `error` |
| Every non-errored case passed (`flip_to_pass` or `pass_kept`) | `pass` |
| Every non-errored case failed (`flip_to_fail` or `fail_kept`) | `fail` |
| Anything else | `mixed` |

**Run-level error-dominated override (spec 0.3.0).** The >25% floor is computed from the
AGGREGATE (see "Error-dominated runs" above), not inside the outcome→verdict classifier. When
`error_dominated` is true, a conforming runner MUST override the classified verdict to `error`
on the run it reports: an invalid measurement must never surface as `pass`/`mixed`, and CI
gates (e.g. the reference CLI's `--fail-on-verdict`) MUST treat an error-dominated run as
failing. The two layers are deliberately separate so the classifier stays a pure,
golden-pinned function; a third-party implementation that folds the floor into its classifier
will fail the `verdict/pass-with-error-excluded` golden.

Note on the floor's divisor: the aggregate counts aggregation units — `(case, run)` records —
so with `--runs N > 1` an erroring flaky case weighs once per run. An implementation that
counts CASES instead agrees exactly at `runs: 1` (the default) and only approximately above it.

The verdict is a coarse signal. The aggregate metrics are the substantive output.

## What's NOT in the runner contract

These are product/policy decisions for the consuming platform, not spec material:

- **Publish gates** — whether a skill is eligible for some catalog based on its run results (e.g., "≥80% pass + positive lift") is a product policy. Implementations MAY enforce such gates; the spec does not require one.
- **Pricing / quotas** — how many runs a user gets per month is a product decision.
- **Persistence schema** — how the result is stored in a DB is implementation choice.
- **UI surfaces** — how the result is displayed is implementation choice.
- **Recording to `evaluation.gates[]`** — composing with [agentversion](https://github.com/decimal-labs/agentversion) is recommended but optional.

## Conformance suite pointers

The golden in/out fixtures under [`../compatibility-tests/`](../compatibility-tests/) cover:

| Directory | What it tests |
|---|---|
| `outcomes/{flip-to-pass,pass-kept,fail-kept,flip-to-fail,error,error-overrides}/` | Outcome classifier — exhaustive, incl. `errored` overriding the pass flags |
| `verdict/{all-pass,all-fail,mixed,all-error,pass-with-error-excluded}/` | Outcome-classifier verdict — incl. errors excluded from the pass/fail decision. `pass-with-error-excluded` (1/3 errored) pins the CLASSIFIER's `pass`; at run level the 0.3.0 error-dominated override then reports `error` — the override lives outside the classifier |
| `aggregation/apples-to-oranges-skip/` | Skip rule application |
| `aggregation/error-excluded/` | Errors excluded from the aggregate averages — and, at 1 error / 2 cases, the **error-dominated floor** tripping (`error_dominated: true`, `delta_pts: null`) |
| `aggregation/error-dominated-floor/` | **Error-dominated floor** — 2/4 errored: headline `delta_pts` nulled while per-arm rates + disclosure counts stay |
| `aggregation/error-floor-at-boundary/` | Exactly 25% errored does NOT trip the floor (strictly-greater comparison) |
| `aggregation/zero-baseline/` | `delta_pct` is null when baseline is zero |
| `aggregation/negative-delta-good/` | Negative (good) efficiency delta sign convention |
| `aggregation/{all-skipped,all-errored,empty}/` | **N/A contract** — `pass_rate.delta_pts` is null when `cases_aggregated == 0` |
| `aggregation/runs-mean-over-repeats/` | **Repetition = MEAN** — repeat (case, run) records average into the rates; no AND-fold (rev 2) |
| `parser/{happy-path,rejects-*}/` | Parser strictness — unknown keys at every level, duplicate names, empty cases, missing assertions, removed rev-1 keys (`rejects-removed-rev1-keys` — the migration error) |
| `parser/accepts-trigger-only-case/` | **Trigger cases (spec 0.3.0)** — a trigger-only case (`should_trigger`, no grader) parses; `should_trigger` composes with graded fields |
| `parser/accepts-setup-files-mapping/` | **`setup` mapping form (spec 0.3.0)** — `{files:, commands:}` parses; list form unchanged |
| `trajectory/{minimal-session,repeated-tool-calls,tool-call-with-output,tool-output-truncation}/` | Canonical rendering, incl. repeated-tool output queueing + truncation |
| `trajectory/{duration-rounding,tool-args-portability,truncation-boundary}/` | **Cross-language byte-equality** — half-up duration rounding, normative arg JSON (list/object/float/non-ASCII), codepoint truncation |

A conforming runner reproduces the `expected.json` for each `input.json`.
