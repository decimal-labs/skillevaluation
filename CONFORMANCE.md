# Conformance

How an alternate-language implementation of `skillevaluation` proves it conforms to the spec.

## Why this exists

The spec is a multi-language target. The Python reference implementation lives in this repo, but an implementation in TypeScript, Rust, Go, or any other language is conforming as long as it produces the same outputs for the same inputs. This document defines "same outputs."

## What a conforming implementation must do

A conforming implementation must support, at minimum (the **core** capabilities):

1. **`eval.yaml` parsing** — accept an `eval.yaml` document, validate it against `schemas/eval-yaml.schema.json`, and enforce the semantic rules in `spec/eval-yaml.md` (unique case names within a suite, the **at-least-one-grader rule** — every case carries `expectations` or `validators` — with the spec-0.3.0 **trigger-only exemption**: a case declaring `should_trigger` with no grader is legal and is skipped with disclosure; and rejection of the removed rev-1 keys with a migration message). Both `setup` forms parse: the legacy command list and the 0.3.0 `{files:, commands:}` mapping.

2. **Outcome classification** — given a per-case `{ with_passed: bool, without_passed: bool, errored: bool }` triple, classify as one of the five outcomes in `spec/runner-contract.md`. The mapping is total and deterministic.

3. **Aggregation** — given a list of per-case metric dicts, produce the run-level aggregate following `spec/runner-contract.md`: pass rate (a MEAN over all (case, run) records — repeat runs of a case average in, rev 2), per-dimension raw deltas, the **correctness-gated** efficiency deltas (`*_correctness_gated`, over both-arms-passed cases only), and the **error-dominated floor** (`error_dominated: true` + a nulled headline `pass_rate.delta_pts` when errored / total > 0.25; exactly 25% does not trip) — all under the apples-to-oranges skip rule.

4. **Trajectory rendering** — given a trace's user input, final output, and step list, produce the canonical text format defined in `spec/trajectory-format.md`. Byte-equal output across implementations is required (the format is the canonicalization step that LLM-judge interop depends on).

### Removed capability tier (rev 2)

The rev-1 optional multi-turn tier (`conversation` state-machine grading, `explore` computed gold,
pass^k) is removed with schema rev 2 (ADR-0007). There is one execution contract; a runner that
executed rev-1 modes should migrate suites per `spec/eval-yaml.md` § Removed rev-1 keys.

### Trigger cases (rev 2)

An A/B runner never executes `should_trigger` cases — it skips trigger-only cases with the
`cases_skipped_trigger_only` disclosure (normative). Routing/trigger grading is platform-side
tooling outside this spec.

## The conformance suite

Located under [`compatibility-tests/`](./compatibility-tests/). Each subdirectory is a scenario:

```
compatibility-tests/
  parser/
    happy-path/
      input.yaml           # input
      expected.json        # parsed shape a conforming impl must produce
    rejects-duplicate-names/
      input.yaml
      expected-error.json  # error class + message snippet
  outcomes/
    flip-to-pass/
      input.json           # { with_passed, without_passed, errored }
      expected.json        # { outcome: "flip_to_pass" }
    ...
  aggregation/
    apples-to-oranges-skip/
      input.json           # list of per-case metrics
      expected.json        # aggregate including cases_skipped count
  trajectory/
    minimal-session/
      input.json           # { user_input, final_output, llm_calls, spans }
      expected.txt         # byte-equal trajectory text
```

The Python reference verifies itself via `tests/test_conformance.py`. To verify an alternate implementation, run that implementation against each `input.*` and assert byte-equal match against `expected.*`.

## Versioning

The conformance suite is versioned with the spec. A breaking spec change MUST update or replace the affected golden outputs.
