# ADR 0006 — Ship a reference runner in the package

**Status:** Accepted (2026-06-05)

## Context

Through v0.1.x this repo deliberately shipped the *format*, the *scoring
math*, and the *spec* — but not the harness that executes a suite. The
README said "what actually runs the agent? That part is yours to bring."

That split had three problems in practice:

1. **The quickstart was unfulfillable.** `pip install skillevaluation`
   could parse an eval.yaml and aggregate numbers you didn't have. The
   package's headline promise (a measured with/without delta) required
   either writing your own harness or using a hosted platform.
2. **A spec with zero open conforming runners is a weak spec.** The
   conformance suite existed, but no public implementation passed it
   end-to-end. Implementers had prose and golden files, not reference
   *behavior* to compare against.
3. **The execution mechanics are commodity.** Sandboxed shell
   validators, an LLM judge behind a contract, a baseline cache — none
   of this is differentiated; comparable eval tools all ship it openly.

## Decision

v0.2.0 adds `skillevaluation.runner` — a complete reference runner — and
a `skillevaluation` CLI, behind an optional `[runner]` extra:

- `runner/orchestrator.py` — the spec's A/B loop, end to end
- `runner/judge.py` — reference LLM judge (contract per `spec/llm-judge.md`;
  the prompt wording remains non-normative) + the structural fast path
- `runner/validators.py`, `runner/workspace.py` — sandboxed assertion
  execution with `response.txt` / `$RESPONSE_TEXT` staging
- `runner/cache.py` — local file baseline cache over the spec'd key
  derivation, scoped by adapter identity
- `runner/adapters/` — the invocation seam: a supported direct-LLM
  adapter (BYO API key), a deterministic mock, and an experimental
  Claude Code adapter

The core install stays PyYAML-only; the runner's HTTP needs (`httpx`)
live in the extra and are imported lazily.

## What stays out of scope (unchanged from 0.1.x)

Hosted execution, run history, result storage schemas, publish gates,
ranking, quotas, and traffic-split experiments remain product/policy
decisions for consuming platforms — see `runner-contract.md` §"What's
NOT in the runner contract". Shipping a runner changes *who can produce
a result*, not what the spec governs.

## Consequences

- The reference runner must itself pass `compatibility-tests/` — the
  package is now the first conforming implementation of its own spec.
- The single-shot LLM adapter reports honest-but-trivial `turns` /
  `tool_call_count` (1 / 0). Pass-rate, token, and duration deltas are
  real; multi-turn dimensions need an agent-runtime adapter (the
  experimental claude-code one, or your own).
- The judge prompt in `runner/judge.py` is a *reference choice*. Per
  ADR 0002 and `spec/llm-judge.md`, implementations may use their own
  wording; the contract is the I/O shape and rubric.
- `eval.yaml` suites SHOULD use workspace-relative paths. The bundled
  example was updated accordingly (absolute `/workspace/...` paths only
  worked on container runners).
