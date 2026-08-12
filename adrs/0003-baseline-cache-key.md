# ADR 0003 — Baseline caching keyed by (skill, case, prompt)

**Status:** Accepted (2026-05-28)

## Context

A/B benchmarking ([ADR 0001](./0001-ab-not-absolute.md)) requires running each case twice. The without-skill arm is independent of the skill being tested — the same case run against the same manifest without skill X produces the same trajectory regardless of what X is.

Without caching, every benchmark run wastes the without-skill compute. With a 50-case suite, that's 50 unnecessary agent invocations per repeat benchmark — significant cost and latency.

## Decision

Conforming runners SHOULD cache the without-skill trajectory per `(skill_id, case_id, prompt)` tuple. The cache key is derived as:

```
sha256(json.dumps({"skill": skill_id, "case": case_id, "prompt": prompt}, sort_keys=True))[:16]
```

Reference implementation: `skillevaluation.baseline.baseline_cache_key`.

When the agent's manifest changes (different tools, model, system prompt), the cache MUST be invalidated. The recommended way to scope this is to extend the cache key with `agent_manifest_id` at the runner level — the spec defines the deterministic-derivation half; runners add the scope.

## Why include `skill_id` if the arm is skill-independent

The cache key includes `skill_id` even though the baseline is skill-independent, because:

- It avoids cross-skill cache collisions for the same prompt text. Two different skills might use identical-prompt regression-check cases ("classify this schema") and the without-skill baseline for each is computed against a different manifest (one with skill A loaded, one with skill B loaded — and "without-skill" means "without the target", not "without any skill").
- It keeps the cache namespace per-skill, which makes invalidation surgical when a skill's eval suite changes.

## Why hash truncated to 16 hex chars

- 16 hex chars = 64 bits of entropy = collision-resistant for any plausible catalog size (a catalog with 1M skills × 100 cases each = 100M entries; birthday probability of collision ≈ 0 at 64 bits)
- Fits in URLs, log lines, and DB indexes without bloat
- Easier to copy-paste in debugging

## Why `sort_keys=True`

Deterministic hash output across implementations. Without `sort_keys`, two implementations that serialize the same logical record in different JSON-key-order would compute different cache keys and miss the cache. This is the same canonicalization principle as JCS-SHA256 in agentversion.

## Why this is SHOULD, not MUST

The spec doesn't force runners to cache. A research runner that only runs a suite once per skill version has no cache hits — paying the implementation cost of caching would be wasted. The cache key is provided so runners that DO cache produce comparable results.

## Consequences

- Repeat benchmark runs on the same manifest amortize to ~50% the cost of the first
- Cache MUST be invalidated when the agent manifest changes (tool list, model, system prompt)
- The baseline cache is implementation-defined in scope (per-org, per-tenant, per-installation)
- The deterministic-derivation half lives in `skillevaluation.baseline` and is shared across implementations
