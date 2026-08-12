---
name: api-error-envelope
description: Use when writing a JSON error-response body for our HTTP API — applies the house error envelope (a nested `error` object with a SCREAMING_SNAKE `code`, a `message`, and a `request_id`) the base model won't guess, so every endpoint returns errors in one consistent, client-parseable shape. Template: replace the codes with your own.
category: coding
tags: [coding, api, http, json, error-handling, conventions, template]
stability: stable
---

> **Worked example.** The envelope below is a fictional "Acme API" house contract.
> It's a shape a model *cannot* guess from training — which is the whole point: the
> with-skill vs without-skill delta this example measures is real knowledge-lift
> (our exact envelope), not generic JSON competence. Fork this skill and drop in
> your own API's contract.

# Acme API — Error Response Envelope

Every error response body our API returns MUST be this exact shape — no more, no less:

```json
{
  "error": {
    "code": "SCREAMING_SNAKE_CASE",
    "message": "A human-readable, client-safe sentence.",
    "request_id": "the request's correlation id"
  }
}
```

Rules a model cannot guess without this skill:

- The payload is **nested under a top-level `error` object** — never a bare `{"message": ...}` or a
  flat `{"error": "..."}` string.
- `code` is **SCREAMING_SNAKE_CASE** (e.g. `NOT_FOUND`), never an HTTP number, never lowercase, never
  a sentence. Use our vocabulary: `NOT_FOUND`, `VALIDATION_ERROR`, `UNAUTHORIZED`, `RATE_LIMITED`,
  `INTERNAL`.
- `message` is a human-readable sentence safe to show a client — no stack traces, no internals.
- **`request_id` is REQUIRED on every error** so clients can quote it in support tickets. Omitting it
  is the single most common way a hand-written error body fails our contract.

## When to use

Load this skill only when writing an **error response body** for the Acme API. Do not impose the
envelope on a plain factual answer or a success body — see the non-activation case in `eval.yaml`.

## Output

Return **only** the JSON error body — no prose, no code fence, no explanation.
