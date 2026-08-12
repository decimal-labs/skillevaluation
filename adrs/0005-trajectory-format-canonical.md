# ADR 0005 — Trajectory format is canonical and byte-equal across implementations

**Status:** Accepted (2026-05-28)

## Context

An LLM judge ([`spec/llm-judge.md`](../spec/llm-judge.md)) grades agent behavior by reading a transcript of the session. If two implementations render the same session into two different transcripts, their judges will see different inputs and disagree on `passed`. Cross-implementation conformance becomes impossible to assert.

The same problem appears in [agentversion](https://github.com/decimal-labs/agentversion) for manifest hashing: two implementations that serialize the same logical manifest in different JSON-key-order would compute different hashes. AgentVersion solves it by mandating JCS-SHA256 (RFC 8785) as the canonical serialization.

## Decision

`skillevaluation` defines a **canonical trajectory format** (v1) that all implementations MUST produce byte-equal output for, given the same input session. The format is specified in [`spec/trajectory-format.md`](../spec/trajectory-format.md) and implemented in `skillevaluation.trajectory.format_v1`.

Conformance tests under `compatibility-tests/trajectory/` verify byte-equal match against `expected.txt` files.

## Why byte-equal, not "equivalent"

A "structurally equivalent" rule (e.g., "the same JSON object, key order ignored") is harder to verify than byte-equal output. The conformance check becomes a custom comparator per implementation, and edge cases (whitespace, number formatting, escape encoding) become arguable.

Byte-equal output is the simplest test: `actual == expected`. Whatever the format's structural rules are, they are encoded in the canonical bytes.

This also forces the format to be deterministic in details like:

- Tool args formatting (string vs JSON, quote style)
- Step number increments
- Whitespace and indentation
- Truncation marker exact wording

Implementations don't get to make small "improvements" that would silently shift judge inputs.

## Why a lossy format

Full provider-format trajectories (raw OpenAI/Anthropic message arrays) cannot serve as the canonical input because:

- They're provider-specific — Anthropic and OpenAI message structures differ
- They include implementation noise (token IDs, span metadata) that doesn't affect agent behavior
- They're verbose — full tool outputs consume judge token budget unnecessarily

The lossy v1 format strips this down to: user said X, agent said/did Y, tool returned Z (first 200 chars). This is enough for a judge to grade behavioral expectations and small enough to fit comfortably in context.

## Why 200-char tool output truncation

Empirically:

- Judges rarely need more than the first 200 chars to grade an expectation
- 200 × 50 steps = 10k chars — leaves ample room in even modest context windows
- Going lower (100) drops too much; going higher (500) inflates judge costs without quality gain

This is calibrated for v1. Future versions may revisit (see [`spec/versioning-policy.md`](../spec/versioning-policy.md)).

## Why explicit FORMAT_VERSION

Stored reports may persist for years across multiple format versions. An explicit `FORMAT_VERSION = "v1"` lets consumers distinguish v1 transcripts from future v2 ones at the row level, instead of guessing from content.

This is the same reason AgentVersion pins format identifiers in its manifest schema.

## Consequences

- Conforming runners MUST produce byte-equal trajectory text
- Any change to the rendering rules requires a `FORMAT_VERSION` bump
- The format is intentionally lossy — implementations cannot "add helpful extra info"
- An LLM judge SHOULD operate on the canonical text, not on richer internal representations, so score variance across implementations is bounded by judge variance alone
