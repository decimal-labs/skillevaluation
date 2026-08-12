# Trajectory Format (v1)

> Status: v1 (current). Reference implementation: `skillevaluation.trajectory.format_v1`.

The trajectory format is a **deterministic, lossy text rendering** of an agent's session. It is the input to any LLM judge or analyzer that reads what happened in an agent run. Its purpose is **canonicalization** — two conforming implementations operating on the same agent session MUST produce byte-equal trajectory text, so their LLM judges see identical inputs.

This is the same role JCS-SHA256 plays for manifest hashing in [agentversion](https://github.com/decimal-labs/agentversion): a stable canonical form that makes cross-implementation comparison possible.

## Why a single canonical format

Without canonicalization, an LLM-judge implementation comparison is contaminated: implementation A renders tool outputs at 500 chars while B renders at 200, the judge sees different prompts, and reported `passed` differs. Conformance becomes impossible to assert.

By pinning the format at v1, all implementations get the same input from the same session and any judge disagreement is genuinely about *judging*, not about *rendering*.

## What is included

A v1 trajectory MUST include:

| Element | Source |
|---|---|
| **User messages** | Full text, no truncation |
| **Agent tool calls** | Name + args (args formatted inline; see below) |
| **Tool outputs** | First 200 characters + truncation marker if longer |
| **Agent responses** | Full text |
| **Step numbers** | Incrementing from 1 per logical step |
| **Per-step durations** | Optional; included when the source data has them |

## What is excluded

A v1 trajectory MUST omit:

| Element | Why |
|---|---|
| Raw provider message arrays (OpenAI/Anthropic JSON) | Format-specific; not comparable cross-implementation |
| Full tool output bodies beyond 200 chars | Token cost; the judge is fine with summaries |
| Internal reasoning / chain-of-thought tokens | Not always available; not part of the observable interaction |
| System prompts | Out of scope — the trajectory describes the session, not the agent configuration |
| Trace IDs, span IDs, internal infrastructure metadata | Not user-meaningful |

## Line format

Each line MUST follow the structure:

```
[Step N · Ts] body
```

Or, when no duration is available:

```
[Step N] body
```

Where:

- `N` is the 1-indexed step number
- `T` is the per-step duration in seconds, formatted with exactly one decimal place. **Rounding is normative: round half *away from zero* on the decimal value** (e.g. `0.25s` → `0.3s`, `2.45s` → `2.5s`), NOT IEEE-754 binary round-half-even — a language whose default `printf("%.1f")` rounds half-to-even (or rounds the binary representation) will diverge on tie values and break byte-equality. Compute it as `round_half_up(latency_ms / 1000, 1)` over the *decimal* value (e.g. `Decimal(str(latency_ms)) / 1000`). The duration segment is **omitted entirely** when the duration is absent or rounds to `0.0` (a sub-50ms step renders `[Step N]` with no `· Ts`).
- `body` is the content (User/Agent/Tool output)

## Body patterns

### User message

```
[Step 1] User: <user text, stripped of leading/trailing whitespace>
```

### Agent response

```
[Step N · 1.2s] Agent: <response text, stripped>
```

Multi-line agent responses are joined into a single line; the canonical form does not preserve internal newlines in agent output. (Future format versions may revisit this.)

### Agent tool call

```
[Step N] Agent → tool: <tool_name>(<args>)
```

Args are formatted inline:

- String values are double-quoted: `name="foo"`
- Non-string values are JSON-serialized: `count=5`, `flags=["x", "y"]`
- Empty args render as `tool_name()`
- Multiple args are comma-separated: `tool_name(a="x", b=5)`

The args formatting is part of the canonical form — implementations MUST match exactly. Reference: `skillevaluation.trajectory.format_v1._format_tool_args`. The JSON serialization of non-string values is **normative and must be byte-stable across languages**:

- **Separators are `", "` and `": "`** (one space after each comma and colon) — i.e. Python's `json.dumps(v, separators=(", ", ": "))`, NOT the compact `(",", ":")` a `JSON.stringify`/`serde_json` default emits, and NOT pretty-printed.
- **Object keys preserve source insertion order** — do NOT sort them. The canonical form mirrors the order the runner observed the args in.
- **Non-ASCII is preserved literally** (`ensure_ascii=False`) — a `"café"` arg renders `café`, not `café`.
- **Numbers** render as the source carries them (`5` stays `5`, `1.0` stays `1.0`); a value the runtime cannot JSON-serialize falls back to its string form (`default=str`).
- Argument keys themselves are rendered bare (`key=value`), not quoted.

### Tool output

```
[Step N] Tool output: <first 200 chars of output>[truncated, K more chars]
```

The truncation marker:
- Appears only when the original output exceeds 200 **characters**
- Includes the exact count `K` of dropped characters
- Reads literally: `[truncated, 184 more chars]`

**The unit is normative: a "character" is one Unicode codepoint on the decoded string**, NOT a UTF-8/UTF-16 byte or code unit. A 250-codepoint output containing emoji or CJK truncates at codepoint 200 and reports `250 - 200 = 50` dropped codepoints — an implementation that counts UTF-8 bytes (or UTF-16 code units, as a naive JS `.length` does for astral characters) would cut at a different offset and report a different `K`, breaking byte-equality. Slice and count on the decoded codepoint sequence (Python `str` / Rust `chars()` / JS `[...str]`).

If the source data does not pair a tool call with a tool output (the runner didn't capture it), the trajectory MAY omit the output line. It MUST NOT fabricate one.

## Worked example

Input:
- User input: `Where is my order ORD-12345?`
- Agent tool call: `get_order_status(order_id="ORD-12345")` — emitted by an LLM call with no response text, so that call's latency is not rendered
- Tool output: 384-char JSON blob starting `{"status":"shipped","carrier":"UPS","tracking":"1Z…"}`
- Agent response: `Your order ORD-12345 was shipped via UPS. Tracking: 1Z…` — from an LLM call with 2.3s latency

Canonical v1 output:

```
[Step 1] User: Where is my order ORD-12345?
[Step 2] Agent → tool: get_order_status(order_id="ORD-12345")
[Step 3] Tool output: {"status":"shipped","carrier":"UPS","tracking":"1Z..."} [truncated, 184 more chars]
[Step 4 · 2.3s] Agent: Your order ORD-12345 was shipped via UPS. Tracking: 1Z...
```

Note the duration appears only on the agent-response line (Step 4) — it carries the originating LLM call's latency. User, tool-call, and tool-output lines never carry a duration, so they render `[Step N]` with no `· Ts` segment.

## Duck-typed input

The reference implementation accepts both ORM objects and plain dictionaries via `getattr`. Conforming implementations SHOULD do the same — the canonical form depends on **what the trajectory data carries**, not **what type the source uses to carry it**.

Required attributes (read via `getattr`):

- On the call: `output_preview` (string) OR `output_json` (dict), `tool_calls_json` (list of dicts), `latency_ms` (int), `started_at` (datetime)
- On the span (optional, used for tool outputs): `span_type`, `name`, `output_preview`

Missing attributes are tolerated — the corresponding line is simply omitted from the output.

## Truncation budget

The 200-character cap on tool output is a **deliberate** choice. The reasoning:

- 200 chars × 50 steps = 10k chars — fits comfortably in any modern LLM judge's context window
- Judges rarely need the full tool output to grade an expectation; the first 200 chars typically convey "the call succeeded and returned X-shaped data"
- Longer tool outputs would push judge token costs to a place where running cases at scale becomes prohibitive

This cap MAY be tightened in future format versions (v2 might drop to 150 chars) but it will not loosen — implementations and judges have settled on a budget around this size.

## Versioning

The format identifier is exposed as `FORMAT_VERSION` in the reference implementation. Any change to canonicalization rules MUST bump the version. See [`versioning-policy.md`](./versioning-policy.md).

A v2 might include reasoning tokens, expand the truncation budget, or change line prefixes. It will not change v1's rules — historical reports referencing v1 will continue to round-trip cleanly.

## See also

- [`llm-judge.md`](./llm-judge.md) — what consumes this format
- [`versioning-policy.md`](./versioning-policy.md) — how format versions evolve
- Reference impl: `skillevaluation/trajectory/format_v1.py`
