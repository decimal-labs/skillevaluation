# Contributing to skillevaluation

Thanks for your interest. `skillevaluation` is a small, focused spec — contributions are most welcome in three forms:

## 1. Conformance tests

If you find a case the existing `compatibility-tests/` golden suite doesn't cover (an edge case in outcome classification, an ambiguous YAML shape, an aggregation corner), add it as a new directory under `compatibility-tests/` with:

- `input.yaml` or `input.json` — the input the runner sees
- `expected.json` — what a conforming implementation must produce

Then update `tests/test_conformance.py` to pick up the new case.

## 2. Reference implementation

The Python reference in `skillevaluation/` is the canonical implementation. Bugs here are spec bugs — they should be fixed in code AND have a conformance test added so the bug can't regress.

## 3. Spec clarifications

If the prose in `spec/` is ambiguous, a PR that tightens the wording (with a regression-protection conformance test) is high-value.

## What is NOT in scope

This spec deliberately does not cover:

- Production traffic-split experiments
- External eval-score push (DeepEval/LangSmith webhooks)
- Catalog ranking, publish-gate thresholds, or other product policy
- LLM judge prompt wording (the contract is specified; the prompt is implementation choice)

For changes outside these boundaries, see the sibling spec [`agentversion`](https://github.com/decimal-labs/agentversion) or open an issue to discuss.

## Versioning

We're pre-v1. Breaking changes are allowed in 0.x with a CHANGELOG entry. Once we cut v1.0, breaking changes go through a deprecation cycle.

## Releases

Releases are published to PyPI by publishing a GitHub Release, which uploads via PyPI Trusted Publishing; `./scripts/release.sh` is the local fallback. See [`RELEASING.md`](./RELEASING.md) for the runbook.

## Development setup

```bash
git clone https://github.com/decimal-labs/skillevaluation
cd skillevaluation
pip install -e ".[dev]"
pytest                              # unit + conformance suite
ruff check skillevaluation tests
mypy skillevaluation                # strict
```

Run all four yourself before opening a pull request. CI runs the same checks from `.github/workflows/ci.yml`, plus a JSON Schema validation of `schemas/eval-yaml.schema.json`.

## License

By contributing, you agree your changes are licensed under Apache 2.0.
