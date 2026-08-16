# Versioning Policy

> Status: pre-stable (v0.x). This policy describes how the spec evolves once we cut v1.0.

`skillevaluation` covers several artifacts that version independently:

| Artifact | Versioning identifier | How it evolves |
|---|---|---|
| `eval.yaml` format | implicit (v1 today) | Additive fields are non-breaking; renames/removals require v2 |
| Runner contract | implicit (v1 today) | New per-arm metric fields are additive; outcome strings are frozen |
| LLM judge contract | implicit (v1 today) | Output schema additive; rubric clarifications are non-breaking |
| Trajectory format | explicit `FORMAT_VERSION = "v1"` | Any canonicalization change bumps version |

## Pre-v1.0 (current)

We are at **v0.x**. Breaking changes are allowed in 0.x with a CHANGELOG entry. Use this period to:

- Discover what implementers actually need
- Find rough edges in the rubric
- Tighten the conformance suite

There is no migration path between 0.x releases — pin a specific version.

## v1.0 cutover criteria

We will cut v1.0 when at least one of the following holds:

- An external implementation (TypeScript, Rust, etc.) is conforming
- DecimalAI's public catalog has ≥100 public `eval.yaml` files

Either signal indicates the format is being used in earnest and the cost of changing it is real.

## v1.0+ stability promise

Once at v1.0, **eval.yaml** and the **runner contract** are stable. Breaking changes go through a deprecation cycle:

1. Deprecation: the change is announced and supported alongside the old behavior for one minor version
2. Removal: at the next major version

Specifically, between v1.0 and v2.0:

- New optional fields MAY be added
- Existing fields MUST NOT change type
- Existing fields MUST NOT be renamed or removed
- The five outcome strings (`flip_to_pass`, `pass_kept`, `fail_kept`, `flip_to_fail`, `error`) are frozen — these ship in user-facing UIs and external APIs
- The aggregate metric shape is frozen
- The apples-to-oranges skip rule is frozen

## Trajectory format versioning

The trajectory format has an explicit `FORMAT_VERSION` because different format versions can coexist in storage. A stored trajectory row written with `FORMAT_VERSION = "v1"` is distinguishable from one written with `"v2"`.

When v2 ships:

- v1 implementations continue working
- Historical reports tagged `v1` remain unchanged
- New runs MAY use v2; the choice is per-runner

## JSON Schema URLs

JSON Schemas are served directly from the GitHub raw content URL on the `main` branch:

```
https://raw.githubusercontent.com/decimal-labs/skillevaluation/main/schemas/eval-yaml.schema.json
```

The URL tracks `main` — it always serves the latest schema content. This is the simplest hosting story: zero infrastructure, no per-release URL updates, works with IDE/CI tooling out of the box.

**The tradeoff:** the URL is not immutable. A consumer that fetches today and again next week may see different content if the schema evolved. Since this spec follows additive evolution within a major version (see above), the practical risk is small — older consumers ignore new optional fields. For consumers that need exact per-release content, the URL `https://raw.githubusercontent.com/decimal-labs/skillevaluation/v<X.Y.Z>/schemas/<name>.schema.json` is available for any released tag (constructed by the consumer, not surfaced in `$id`).

If a brandable custom domain becomes worthwhile later, it can be CNAMEd to GitHub Pages and the `$id`s updated — non-breaking for consumers that vendor the schema.

## Source of truth

The version a `skillevaluation` release implements is recorded in `CHANGELOG.md` and `skillevaluation/__init__.py:__version__`. Implementers SHOULD pin a specific package version in production.

## What does NOT count as breaking

- Adding new optional fields to the eval.yaml schema
- Adding new optional fields to the run/case-result schemas
- Clarifying ambiguous prose in the rubric
- Adding new conformance test fixtures
- Internal refactors of the reference implementation that preserve outputs

## See also

- [`CHANGELOG.md`](../CHANGELOG.md) — the actual log of what changed when
- [`CONFORMANCE.md`](../CONFORMANCE.md) — how conformance is verified
