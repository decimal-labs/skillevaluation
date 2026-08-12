# ADR 0002 — Two assertion kinds: LLM-judged expectations + shell validators

**Status:** Accepted (2026-05-28)

## Context

A test case needs to assert something about the agent's behavior. Options for how:

1. **LLM-judged natural-language assertions** — "The response classifies email as PII"
2. **Shell command assertions** — `jq -e '.email == "PII"' /workspace/output.json`
3. **Structured JSON-schema assertions** — `{"required": ["email"], "properties": {"email": {"const": "PII"}}}`
4. **Code-block Python assertions** — `assert result["email"] == "PII"`

## Decision

`skillevaluation` supports **two** assertion kinds per case: `expectations` (LLM-judged) and `validators` (shell). Both are optional individually; a case MUST have at least one of either.

## Why two kinds

**LLM-judged expectations** are necessary because agent output is often unstructured prose. Asserting "the response classifies email as PII" against a free-text response cannot be done with a regex or jq query — it requires semantic judgment. The cost is some judge variance (see [`spec/llm-judge.md`](../spec/llm-judge.md)).

**Shell validators** are necessary because LLM-judged assertions are expensive, slow, and noisy. When a precise structural check is possible (`test -f` for file existence, `jq -e` for JSON shape, `grep -q` for exact text presence), it is cheaper, deterministic, and immune to judge subjectivity. Skills that produce structured artifacts SHOULD have validator coverage in addition to expectations.

The combination handles the spectrum: structural claims via validators, semantic claims via expectations. Authors pick the right tool per assertion.

## Why not just one of these

**LLM-only.** Rejected — validators are 100× cheaper to run, fully deterministic, and provide regression protection for structural invariants the agent produces. Removing them would push test-suite costs to a level that limits per-PR runs.

**Validators-only.** Rejected — many real expectations are semantic. "The response acknowledges the GDPR risk in the response prose" cannot be asserted via shell.

**Structured JSON-schema only.** Rejected — agent output is often prose, not structured JSON. And when it IS structured, `jq -e` already provides JSON-schema-equivalent assertions with a more familiar UX.

**Code-block Python assertions** (à la pytest). Rejected — introduces a Python runtime requirement for runners and would limit the spec's reach to Python-based implementations. Shell is the universal runtime.

## Why not allow both to be absent

A case with neither `expectations` nor `validators` cannot be scored — it has nothing to grade. The parser rejects this configuration at parse time rather than at run time, so authors catch the mistake at the moment they make it.

## Consequences

- The parser enforces "at least one assertion per case"
- The judge contract specifies a per-expectation output shape (see [`spec/llm-judge.md`](../spec/llm-judge.md))
- The runner contract specifies validator semantics (exit code = `expect_exit_code`)
- An arm `passed` iff **every** expectation AND **every** validator passed (no partial credit at the arm level)
