# skillevaluation

*Part of [DecimalAI](https://decimal.ai). Most users want the Python SDK → [decimal-labs/decimalai-python](https://github.com/decimal-labs/decimalai-python).*

**Does your skill actually make the agent better? Prove it — with measured before/after numbers.**

[![PyPI](https://img.shields.io/pypi/v/skillevaluation)](https://pypi.org/project/skillevaluation/)
[![Downloads](https://static.pepy.tech/badge/skillevaluation/month)](https://pepy.tech/project/skillevaluation)
[![CI](https://img.shields.io/github/actions/workflow/status/decimal-labs/skillevaluation/ci.yml?branch=main)](https://github.com/decimal-labs/skillevaluation/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/skillevaluation/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://pypi.org/project/skillevaluation/)

A skill is just a folder — a `SKILL.md` plus some attachments. It's easy to write one and *assume* it helps. `skillevaluation` lets you **measure** the help: write a small `eval.yaml` next to your skill, and a runner executes each test case twice — once with the skill loaded, once without — then hands you a clear A/B delta on pass rate, speed, tokens, turns, and tool calls.

No more "I think this skill is good." Now you can say *"this skill lifts pass rate 40 points and cuts tokens 43%"* — and back it with reproducible cases.

---

## The payoff

Here's the bundled [`commit-conventions`](https://github.com/decimal-labs/skillevaluation/tree/main/examples/commit-conventions) example — five cases, run with the skill and without it. The skill teaches one thing the model can't guess: your repo's house commit format (`[TICKET] AREA: summary`, with an AREA code the base has no way to know). Without it the model falls back to Conventional Commits (`feat:`/`fix:`) and fails four of the five cases.

| Dimension | Without skill | With skill | Delta |
|---|---:|---:|:---:|
| **Pass rate** | 20% | 100% | **+80 pts** |
| Avg tokens | 118 | 95 | **−19%** |
| Avg turns / tool calls | — | — | *n/a (single-shot)* |

The skill takes the agent from failing the convention to nailing it. That's exactly the kind of claim `skillevaluation` is built to produce — a measured before/after, not a vibe.

> Numbers above are illustrative of the example's shape. Don't take our word for it — that's the whole point: clone the repo and run `skillevaluation run ./examples/commit-conventions` to get your own. (The examples also ship inside the wheel, but the paths below are relative to a checkout.) (Turn / tool-call deltas need an agent-runtime adapter — see the honest-metrics note below.)

---

## How it works

1. **Write `eval.yaml`** next to your `SKILL.md` — a handful of declarative cases (a prompt, plain-English expectations, and optional shell validators).
2. **A runner executes each case twice** — once with the skill loaded (the *with* arm), once without (the *without* arm).
3. **You get measured deltas** — each case is classified (`flip_to_pass`, `pass_kept`, …) and aggregated into per-dimension lift.

```
                    ┌─ with skill ────▶ pass? + metrics ─┐
   each case ──────▶┤                                    ├──▶ outcome ──▶ aggregate deltas
                    └─ without skill ─▶ pass? + metrics ─┘
```

---

## Quickstart

```bash
pip install "skillevaluation[runner]"
```

**1. Describe what "better" means.** Drop an `eval.yaml` beside your `SKILL.md`:

```yaml
# eval.yaml
cases:
  - name: retry-http
    prompt: |
      Ticket: PROJ-101
      Change: add retry to the HTTP client
      Write the git commit subject line. Return only the subject line.
    expectations:
      - "The subject line is in the form [TICKET] AREA: summary, not Conventional Commits"
    validators:
      - cmd: "python3 scripts/grade.py scripts/grading_spec.json retry-http"
        label: "bracketed ticket + NET: area code"
```

See the full five-case suite in [`examples/commit-conventions/eval.yaml`](https://github.com/decimal-labs/skillevaluation/blob/main/examples/commit-conventions/eval.yaml) (ships inside the wheel too, though the relative path below assumes a checkout — `git clone https://github.com/decimal-labs/skillevaluation && cd skillevaluation`).

**2. Run the A/B benchmark.** The bundled examples are referenced by relative path, so start from a
checkout. First a free, networkless dry-run (no API key, ~10s) to confirm the harness runs end-to-end:

```bash
git clone https://github.com/decimal-labs/skillevaluation && cd skillevaluation
skillevaluation run ./examples/commit-conventions --adapter mock
```

Then run it for real — one command, your own API key. Cases go straight to your model provider; nothing is uploaded to DecimalAI unless you pass `--export-url`:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY / GEMINI_API_KEY
skillevaluation run ./examples/commit-conventions --model claude-haiku-4-5
```

Each case executes twice — once with the skill loaded, once without — then **both arms are graded with the same assertions**: expectations via an LLM judge, validators in an isolated per-case workspace (scrubbed env, resource limits, `HOME`/`TMPDIR` confined to the workspace; see the security note below). You get the delta table on stdout plus a `results.json` that validates against the packaged [wire schema](https://github.com/decimal-labs/skillevaluation/blob/main/schemas/test-run-result.schema.json) (ships in the wheel — `from skillevaluation.resources import load_schema`). The without-skill arm is cached locally (it doesn't depend on the skill), so re-runs while you iterate on `SKILL.md` cost half.

**3. Gate it in CI.**

```yaml
- run: skillevaluation run ./examples/commit-conventions --fail-on-verdict fail --min-delta-pts 10
```

Useful flags: `--adapter mock` (free, networkless plumbing dry-run) · `--adapter claude-code` (experimental: drives your installed Claude Code, so turn/tool-call deltas come from a real agent loop) · `--judge-model` (use a cheap judge, e.g. `gemini-3.5-flash`) · `--trajectories DIR` (write per-arm canonical transcripts) · `--json` · `--export-url` (POST the results document to any collector).

> Security note: a `validators` command and a `setup` step are author-controlled shell. The reference runner reduces the blast radius — it scrubs secrets from the environment, applies CPU/file-size resource limits, confines `HOME`/`TMPDIR` to the per-case workspace, and (where the host supports a user+network namespace) runs validators with no network by default. It is **not** a full OS sandbox (no filesystem jail). Don't point it at `eval.yaml` suites you don't trust without real OS-level isolation.

> Honest-metrics note: the default `llm` adapter is a single-shot completion — pass-rate, token, and duration deltas are real; `turns`/`tool_calls` are trivially 1/0. Use an agent-runtime adapter (or implement [`AgentAdapter`](https://github.com/decimal-labs/skillevaluation/blob/main/skillevaluation/runner/adapters/base.py) (ships in the wheel — `from skillevaluation.runner.adapters.base import AgentAdapter`) for your own stack) when those dimensions matter.

**Scoring as a library.** Bringing your own harness? The same delta math is importable directly:

```python
from skillevaluation.outcomes import classify_outcome
from skillevaluation.aggregation import CaseResult, CaseMetrics, compute_run_aggregates

results = [
    CaseResult(
        case_name="retry-http",
        outcome=classify_outcome(with_passed=True, without_passed=False),
        with_skill=CaseMetrics(passed=True,  duration_ms=1300, turns=1, total_tokens=95, tool_call_count=0),
        without_skill=CaseMetrics(passed=False, duration_ms=1450, turns=1, total_tokens=118, tool_call_count=0),
    ),
    # ... one CaseResult per case
]

agg = compute_run_aggregates(results)
print(agg.pass_rate)   # {'with_skill': 1.0, 'without_skill': 0.0, 'delta_pts': 100.0}
print(agg.to_dict())   # full per-dimension JSON, matching the wire schema
```

**What actually runs the agent?** The bundled reference runner — `skillevaluation run` executes every case A/B through a pluggable [adapter](https://github.com/decimal-labs/skillevaluation/tree/main/skillevaluation/runner/adapters). Building a runner in another language (or a hosted one)? It's an open spec: start at the [runner contract](https://github.com/decimal-labs/skillevaluation/blob/main/spec/runner-contract.md) (ships in the wheel — `from skillevaluation.resources import read_spec`) and verify against [`compatibility-tests/`](https://github.com/decimal-labs/skillevaluation/tree/main/compatibility-tests) (ships in the wheel). Hosted execution, run history, and rankings are platform concerns — [DecimalAI](https://decimal.ai) runs this same spec server-side.

> **Status:** pre-1.0 (see [`CHANGELOG.md`](https://github.com/decimal-labs/skillevaluation/blob/main/CHANGELOG.md) for the current version). The format is stable enough to build on, but APIs may shift before v1 — every change is logged in the changelog.

---

## What's in the box

A typed Python reference implementation. The core is dependency-light (only needs PyYAML); the runner's HTTP pieces live behind the `[runner]` extra:

| Module | What it does |
|---|---|
| `skillevaluation.parser` | Parse + strictly validate `eval.yaml` |
| `skillevaluation.outcomes` | Classify each case: `flip_to_pass` / `pass_kept` / `fail_kept` / `flip_to_fail` / `error` |
| `skillevaluation.aggregation` | Per-dimension delta math, with an honest apples-to-oranges skip rule |
| `skillevaluation.baseline` | Baseline-cache key derivation (skip re-running an unchanged *without* arm) |
| `skillevaluation.trajectory.format_v1` | Canonical agent-session rendering, so different runners' LLM judges agree |
| `skillevaluation.resources` | The packaged `spec/` + `schemas/` — validate results offline, no GitHub fetch |
| `skillevaluation.runner` | **The reference runner**: A/B orchestrator, reference LLM judge, workspace-isolated validators (env-scrubbed + resource-limited, not a full OS sandbox), local baseline cache |
| `skillevaluation.runner.adapters` | The invocation seam: direct-LLM (supported), mock (deterministic), Claude Code (experimental) — or implement `AgentAdapter` for your runtime |
| `skillevaluation` CLI | `run` (delta table + `results.json` + CI gates) and `validate` |

---

## Use it as a spec, not just a library

`skillevaluation` is an **open spec**, so any tool — in any language — can produce interoperable results. If you're building your own runner, start here:

- [`spec/eval-yaml.md`](https://github.com/decimal-labs/skillevaluation/blob/main/spec/eval-yaml.md) — the file format
- [`spec/runner-contract.md`](https://github.com/decimal-labs/skillevaluation/blob/main/spec/runner-contract.md) — how to execute cases A/B and aggregate
- [`spec/llm-judge.md`](https://github.com/decimal-labs/skillevaluation/blob/main/spec/llm-judge.md) — the judge input/output contract
- [`spec/trajectory-format.md`](https://github.com/decimal-labs/skillevaluation/blob/main/spec/trajectory-format.md) — canonical session rendering
- [`schemas/`](https://github.com/decimal-labs/skillevaluation/tree/main/schemas) — JSON Schemas for every input and output (ships in the wheel — `from skillevaluation.resources import load_schema`)
- [`CONFORMANCE.md`](https://github.com/decimal-labs/skillevaluation/blob/main/CONFORMANCE.md) + [`compatibility-tests/`](https://github.com/decimal-labs/skillevaluation/tree/main/compatibility-tests) — golden in/out pairs your implementation must reproduce (both ship in the wheel)

Deliberately **out of scope:** live traffic-split experiments, external eval-score webhooks (DeepEval/LangSmith), catalog ranking or publish-gate policy, and the exact LLM-judge prompt wording (the contract is specified; the prompt is your choice).

Also out of scope — but covered by the sibling spec: [`agentversion`](https://github.com/decimal-labs/agentversion) versions the agent runtime itself (a manifest of tools/prompts/models, diffed across versions), while `skillevaluation` A/B-benchmarks a single skill; an agent manifest can carry `skillevaluation` results in its `evaluation.gates[]`.

---

## Contributing

Contributions are genuinely welcome — especially new conformance cases that catch an edge the golden suite misses. See [`CONTRIBUTING.md`](https://github.com/decimal-labs/skillevaluation/blob/main/CONTRIBUTING.md) (ships in the wheel). Dev setup is the usual:

```bash
pip install "skillevaluation[dev]"
pytest
```

## License

[Apache 2.0](https://github.com/decimal-labs/skillevaluation/blob/main/LICENSE).

---

[Docs](https://docs.decimal.ai) · [Registry](https://app.decimal.ai/skills) · [SDK](https://github.com/decimal-labs/decimalai-python) · [agentversion](https://github.com/decimal-labs/agentversion) · [regression-check](https://github.com/decimal-labs/regression-check)
