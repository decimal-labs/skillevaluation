# `eval.yaml` — File Format Reference

> Status: **schema rev 2** (current — [ADR-0007](../adrs/0007-one-execution-contract.md)).
> Schema: [`schemas/eval-yaml.schema.json`](../schemas/eval-yaml.schema.json).

A skill's `eval.yaml` is a declarative A/B test suite that lives next to `SKILL.md` in a skill directory. A conforming runner executes each case twice — once with the skill loaded, once without — and compares outcomes. See [`runner-contract.md`](./runner-contract.md) for what "executes" means.

**Rev 2 in one paragraph.** There is ONE execution contract: the agent is invoked once per case, in a
workspace prepared from `setup`; it may take many tool steps (a trajectory), capped by `max_turns`;
the whole trajectory — tool calls, token usage, file changes, final response — is recorded; and the
case is graded by `expectations` (LLM judge whose evidence is the full trajectory transcript) plus
`validators` (deterministic code run in the workspace). A prose-only reply is simply a one-step
trajectory, not a different kind of case. The rev-1 `mode` enum (`single_shot`/`agentic`/`explore`/
`conversation`) and its per-mode fields (`user_goal`, `environment`, `simulator`, `policy_check`,
per-case `trials`) are **removed**; a conforming parser rejects them with a migration message.
Repetition is a **runner-level** setting (`--runs N`), aggregated by MEAN — never a per-case field.

## On-disk layout

```
skills/
  api-error-envelope/
    SKILL.md         ← the skill body
    eval.yaml        ← this spec
    scripts/         ← optional bundled graders/fixtures, staged into each workspace
```

## Top-level shape

```yaml
cases:                 # required, non-empty list
  - name: ...
    prompt: ...
    setup: [...]        # optional — commands list, or a {files:, commands:} mapping (the ENVIRONMENT)
    expectations: [...] # optional — LLM-judged over the full trajectory
    validators: [...]   # optional — code-graded in the workspace
    max_turns: ...      # optional — step cap on the agent's tool loop
    should_trigger: ... # optional, boolean — trigger-eval case (see "should_trigger")
    tags: [...]         # optional
    description: ...    # optional
```

Every non-trigger-only case must carry at least one grader (`expectations` or `validators`) — see
the at-least-one-grader rule below.

A conforming parser MUST reject any document whose top-level is not a mapping with a `cases:` key.

## Case fields

### `name` (required, string)

Identifies the test case within the suite. MUST be non-empty after whitespace trimming. MUST be unique within the same `eval.yaml` — duplicate case names are a parse error (this prevents silent loss of cases through copy-paste).

Conventionally lower_snake_case, but the spec does not enforce a pattern.

### `prompt` (required, string)

The user-message text the runner sends to the agent. MUST be a non-empty string. The runner sends the same prompt to both arms of the A/B (with-skill and without-skill) — that's the point of the test.

For a policy/conversation skill, the prompt MAY seed a transcript-so-far as data (the
**seeded-transcript** pattern) and ask for the agent's next reply. Authoring guardrail: seeded
histories contain the counterparty's lines verbatim and only neutral, policy-consistent stage
directions for prior agent turns — never scripted weak/conceding agent lines, which would let a
suite manufacture baseline failures the live agent never produces.

### `setup` (optional, list of strings OR mapping)

Workspace preparation before the agent is invoked — the case's **environment, as data**. A CSV
fixture, a seeded SQLite database, a git repo, a transcript file: all just files and commands. Two
forms:

**List form (legacy, unchanged):** shell commands, executed sequentially.

```yaml
setup:
  - "echo '{...}' > schema.json"
  - "cp references/no_dsr_schema.json ."
```

**Mapping form (spec 0.3.0):** declarative `files` plus optional `commands`. Only the keys `files` and `commands` are allowed; the list form is exactly equivalent to a mapping with only `commands`.

```yaml
setup:
  files:
    schema.json: '{"email": "string"}'   # relative path → file content
    data/rows.csv: "email\na@b.co\n"
  commands:
    - "sqlite3 orders.db < seed.sql"
```

`files` entries MUST be written into the case workspace **before any command runs**, so commands (and later the agent + validators) can rely on them. Paths are workspace-relative; a conforming runner MUST reject a path that escapes the workspace (absolute paths, `..` traversal) — the same guard applied to a skill's bundled `scripts/` files. Prefer `files` over `echo`/heredoc commands for seeding content: no shell-quoting hazards, and the fixture is readable in the suite.

Setup commands MUST be executed sequentially, with the **case workspace as the working directory** — the same directory the agent then acts in and validators later run in. Suites SHOULD use paths relative to the workspace (`output.json`, not `/workspace/output.json`); absolute paths tie the suite to one runner's filesystem layout and break local execution. A failure in `setup` should be surfaced as the case's outcome being `error` (see [`runner-contract.md`](./runner-contract.md)) — the runner SHOULD NOT proceed to agent invocation if setup failed.

### `expectations` (optional, list of strings)

Natural-language assertions graded by an **LLM judge** (see [`llm-judge.md`](./llm-judge.md)) whose
required evidence is the **full canonical trajectory transcript** — agent actions, tool calls, and
the final response — so an expectation may assert process ("checked the schema before writing") as
well as outcome. Each entry MUST be a non-empty string.

```yaml
expectations:
  - "The response classifies email as PII"
  - "The agent inspected the seeded schema before classifying"
  - "The response does not over-classify the age field"
```

Expectations are intentionally fuzzy — they say what the *meaning* of a passing trajectory should be, not its exact wording. **Every expectation is LLM-judged — always** (there is no structural short-circuit). For exact-match or structural assertions (`response_contains:`, valid-JSON, regex), use `validators` instead — those are graded by code, never the judge. This two-category split keeps each check's grading method unambiguous and publicly displayable.

### `validators` (optional, list of mappings)

Shell-command assertions executed after the agent finishes. Each validator is a mapping:

```yaml
validators:
  - cmd: "test -f output.json"                # required
    expect_exit_code: 0                       # optional, defaults to 0
    label: "wrote output file"                # optional, defaults to first 80 chars of cmd
  - cmd: "jq -e '.email == \"PII\"' output.json"
```

Validators run with the case workspace as the working directory (same as `setup`, same workspace the agent acted in), with the agent's final output staged as `./response.txt` and `$RESPONSE_TEXT`. Use workspace-relative paths.

The validator passes iff the command's actual exit code equals `expect_exit_code`, and fails iff it is the other binary code. Anything else means no verdict was produced — the command timed out, could not be spawned, or exited with a code that is neither `0`/`1` nor the `expect_exit_code` the author declared. Those are recorded as **ungraded** (`errored: true`), which excludes the whole case from lift rather than scoring it a model failure; see [`runner-contract.md`](./runner-contract.md). (A declared `expect_exit_code` is always a clean verdict, whatever its value.) Validators are useful for:

- File-existence checks over produced artifacts (`test -f`, `test -d`)
- Structural assertions on JSON output (`jq -e`)
- Deterministic checks on the reply text (`grep -q ... response.txt`), including negation (`! grep -q ...`)

Validators are **cheaper, more deterministic, and immune to judge subjectivity** than expectations. Prefer a validator when a precise structural check is possible; reserve expectations for genuinely subjective claims.

### `max_turns` (optional, positive integer)

Cap on the agent's tool-loop steps for this case. Absent = the runner's default budget. This is a
cost/safety cap, not a mode: every case is the same single invocation whether it takes one step or
twenty.

### `should_trigger` (optional, boolean) — spec 0.3.0

Declares the case as a **trigger-evaluation** case: does this prompt look like one the skill should activate on?

- `should_trigger: true` — the skill SHOULD be surfaced/selected for this prompt.
- `should_trigger: false` — a **near-miss**: adjacent topic, same keywords with different intent, or a generic task the skill must NOT fire on.

```yaml
cases:
  - name: fires_on_error_format_question
    prompt: "How should I format an API error envelope for our service?"
    should_trigger: true            # trigger-only: no expectations/validators needed
  - name: near_miss_status_code_poetry
    prompt: "Write a limerick about HTTP status codes."
    should_trigger: false
    expectations:                   # composes: this case is ALSO graded for lift
      - "The response is a limerick"
```

Two shapes are legal:

- **Trigger-only case** — `should_trigger` present and NO grader. Exempt from the at-least-one-grader rule below. The A/B lift loop **excludes** trigger-only cases with disclosure (`cases_skipped_trigger_only` — see [`runner-contract.md`](./runner-contract.md)). **Rev 2: routing itself is graded by platform-side tooling, not by this spec's runner** — the boolean is the author-supplied ground truth that tooling consumes (near-miss knowledge is skill-local; a platform cannot synthesize it).
- **Composed case** — `should_trigger` alongside graders. The case runs in the A/B loop as usual AND carries the routing label for platform tooling.

`should_trigger` MUST be a boolean if present (a parse error otherwise). Unknown-key strictness is unchanged.

### At-least-one-grader rule

A case must carry `expectations` or `validators`, otherwise it cannot be scored and is a parse error.

**Trigger-only exemption (spec 0.3.0):** a case with `should_trigger` present and no grader is a
legal **trigger-only** case — it is graded by platform-side routing tooling, not the A/B loop, so
the rule does not apply.

So a case with no grader (and no `should_trigger`) is rejected:

```yaml
cases:
  - name: missing_assertions
    prompt: "do a thing"
```

with an error message naming the case and the missing grader.

### Removed rev-1 keys (migration)

`mode`, `user_goal`, `environment`, `simulator`, `policy_check`, and per-case `trials` are removed.
A conforming parser MUST reject them with a message that names the removal and points at the
migration:

- `mode: agentic` / `mode: explore` → delete the key. Every case already runs as a single
  invocation with a tool loop; seed the world with `setup` (e.g. write a SQLite file) and grade
  with `validators` (the author knows the fixed seed, so the expected answer is an ordinary
  assertion).
- `mode: conversation` + `simulator` + `policy_check` → re-author as **seeded-transcript** cases
  (see `prompt` above; worked example: [`examples/refund-policy/eval.yaml`](../examples/refund-policy/eval.yaml)).
- `trials: N` → delete; run the suite with `--runs N` (runner-level, MEAN-aggregated).

### `tags` (optional, list of strings)

Free-form tags for organization. Conforming runners MAY use tags for filtering ("run only `:pii` tests") but the spec does not assign semantics to specific tag values.

```yaml
tags: [pii, gdpr, classification]
```

### `description` (optional, string)

Human-readable explanation of what the case is testing. Surfaced in test result UIs but has no runtime semantics.

## Validation rules (summary)

A conforming parser MUST enforce:

| Rule | Error class |
|---|---|
| File is non-empty | parse error |
| Top-level is a mapping | parse error |
| `cases` key exists and is a non-empty list | parse error |
| Each case is a mapping | parse error |
| Each case has a non-empty `name` string | parse error |
| Case `name`s are unique within the suite | parse error |
| Each case has a non-empty `prompt` string | parse error |
| `setup` is a list of strings — or a mapping with only `files` (relpath → string content) and/or `commands` (list of strings) — if present | parse error |
| `expectations` is a list of non-empty strings if present | parse error |
| `validators` is a list if present | parse error |
| Each validator has a non-empty `cmd` string | parse error |
| `max_turns` is a positive integer if present | parse error |
| `should_trigger` is a boolean if present | parse error |
| Each case has a grader (`expectations` or `validators`) — OR declares `should_trigger` (trigger-only exemption) | parse error |
| Removed rev-1 keys (`mode`, `user_goal`, `environment`, `simulator`, `policy_check`, `trials`) are rejected with a migration message | parse error |
| `tags` is a list of strings if present | parse error |
| `description` is a string if present | parse error |

Errors SHOULD name the offending case (by `name` if available, else by index) and the offending field — so an author sees `eval.yaml: case "tracks_with_id" — 'prompt' must be a non-empty string` rather than just "invalid YAML."

## Complete worked example

See [`examples/api-error-envelope/eval.yaml`](../examples/api-error-envelope/eval.yaml) for a full A/B suite (validators + a non-activation expectation), and [`examples/refund-policy/eval.yaml`](../examples/refund-policy/eval.yaml) for a policy skill tested with seeded-transcript cases.

## Stability and evolution

This is schema rev 2 — the first revision to REMOVE fields, per [`versioning-policy.md`](./versioning-policy.md) (removals require a schema revision with a migration path; the migration is the "Removed rev-1 keys" section above). Future additive fields (new optional keys) remain non-breaking.
