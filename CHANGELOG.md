# Changelog

All notable changes to `skillevaluation` are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

Safety scanner — two detection gaps closed (`SCANNER_VERSION` 4 → 5). Behavior change:
some content that scanned `clean` before now scans `flagged` or `blocked`.

- **A placeholder marker can no longer delete a credential finding.** `live_secret` is the
  only CRITICAL check that catches a committed live key, and any of `test`, `my`, `your`,
  `example`, `fake` within ±30 chars of the match used to skip it silently — no finding at
  any severity. Two fixes: (a) those five are word-anchored in the surrounding **prose**, so
  they no longer fire inside "latest", "MySQL", "protest" (the unanchored form still applies
  **inside the value**, where `AKIAIOSFODNN7EXAMPLE` is decisive); (b) a suppressed match is
  now **recorded** — INFO when the marker is inside the value (status stays `clean`), WARNING
  when the suppression rests on nearby prose (status `flagged`), never dropped.
- **Every check now scans `name` + `description`, not just the body.** Only the two
  credential checks looked at metadata; the ten behavioural / unicode / URL / injection
  checks read the body alone, so a reverse shell, SSRF endpoint, or injection phrase parked
  in a skill's description — the text a router shows an agent first — scanned `clean`.
  Metadata findings carry `"field": "name/description"` instead of `"line"`, so a payload in
  the frontmatter is never attributed to an unrelated body line. `skillevaluation scan` text
  output prints the field where it would print a line number.

## [0.7.0] — 2026-07-31

Measurement-honesty fix. One behavior change in the reference runner (it makes numbers
strictly more honest; no eval.yaml format change):

- **A validator that never returns a verdict is now an error channel, not a fail** —
  `runner.validators.run_validators` marks a wall-clock **timeout** and a **spawn
  failure** `errored: true` (previously `errored: false`, i.e. a model failure), and the
  orchestrator already rolls that into the case outcome `error`, excluding it from lift
  — exactly like a non-binary undeclared exit code and like a judge transport error
  (0.5.0). Recording "fail" asserted a verdict no grader produced, and since graders run
  **per arm**, a grader that stalled or failed to spawn on only one arm's output skewed
  the delta. It was also internally inconsistent: the same grader killed by `RLIMIT_CPU`
  exits with a negative returncode and already errored, so two flavours of the same
  failure classified oppositely.
- Schema: `validatorResult` now declares the optional `errored` property (it previously
  rode on `additionalProperties`), mirroring `judge-result.schema.json`.
- Spec: new normative sentence in `runner-contract.md` (step 3) and a clarifying clause
  in `eval-yaml.md`; the exit-code contract had lived only in a docstring.
- No conformance golden changes — the goldens exercise the classifier and aggregator
  directly and never execute a validator.

## [0.6.0] — 2026-07-15

**Schema rev 2 — one execution contract (ADR-0007).** Returns the spec to the original design:
the agent is invoked once per case in a prepared workspace, may take many tool steps
(`max_turns` caps them), the whole trajectory is recorded, and grading is `expectations`
(LLM judge over the full transcript) + `validators` (code). Breaking (0.x minor bump per SemVer):

- **Removed** from `eval.yaml`: the `mode` enum (`single_shot`/`agentic`/`explore`/`conversation`),
  `user_goal`, `environment` (computed gold), `simulator`, `policy_check`, and per-case `trials`.
  The parser rejects them with a targeted migration error. `agentic` had been a no-op alias;
  `explore` bypassed the real agent entirely; `conversation` served multi-USER-turn dialogue the
  original design never specified. Policy skills are now tested as **seeded-transcript** cases —
  see the re-authored `examples/refund-policy` (formerly `refund-policy-conversation`).
- **Removed** modules: `skillevaluation.conversation`, `skillevaluation.explore`,
  `skillevaluation.runner.trigger` (menu-selection trigger EXECUTION leaves this spec entirely —
  routing is graded by the tooling that owns the router; the `should_trigger` file format and the
  `cases_skipped_trigger_only` disclosure stay).
- **Removed** wire fields: `pass_at_k`, `trigger_metrics`, `trigger_cases`.
- **Added**: runner-level repetition — `skillevaluation run --runs N` (uniform i.i.d. re-runs per
  case, capped at 10), aggregated by **MEAN** over all (case, run) records; the optional top-level
  `runs` integer disclosure in the results document. pass^k's AND-fold (which made the headline
  depend on an author-chosen k) is retired; flakiness stays visible in per-run records.
- CLI: `--distractors`, `--skip-trigger-cases`, `--fail-on-trigger` removed; `--runs` added.
- Conformance: conversation/explore/multi-turn-parser/pass-at-k goldens retired;
  `aggregation/runs-mean-over-repeats` and `parser/rejects-removed-rev1-keys` added.

## [0.5.0] — 2026-07-08

Measurement-honesty fixes. Two behavior changes in the reference runner/aggregation
(both make numbers strictly more honest; no eval.yaml format change):

- **Judge transport errors are now an error channel, not a fail** — `runner.judge.
  judge_expectations` marks a transport failure `errored: true` on the result dict
  (schema: new optional `errored` property), and the reference orchestrator rolls it
  up into the case outcome `error` (excluded from lift), exactly like script-validator
  breakage. Previously a judge transport failure was recorded as a failed assertion, so
  a one-arm outage skewed the delta. Spec: new edge-case row in `spec/llm-judge.md`.
- **`error_dominated` now also nulls `pass_at_k.delta_pts`** — the floor previously
  nulled only `pass_rate.delta_pts`, so a consumer preferring pass^k for its headline
  read an un-withheld delta off an invalid run. Per-arm pass^k rates and disclosure
  counts survive; only the claim is withdrawn (parity with `pass_rate`).

Safety-scanner calibration in `skillevaluation.safety`:

- **Bidi controls split by risk.** Overrides/embeddings `U+202A–U+202E` (classic
  Trojan Source) stay CRITICAL and non-downgradable; isolates `U+2066–U+2069` (the
  Unicode-recommended mechanism for embedding an LTR run in RTL text) drop to WARNING,
  treated like zero-width (flagged, skipped inside code). A skill carrying both still
  blocks on the override.
- **Review-context doc markers.** The documentation-downgrade vocabulary gains
  `hazard/irreversible/destructive/unsafe/dangerous/rollback/downtime/data loss`, so a
  migration / audit / safety-review skill that quotes a destructive command near
  danger-teaching words downgrades CRITICAL→WARNING (still flagged, never cleared).

## [0.4.0] — 2026-07-05

Adds `skillevaluation.safety` — a deterministic static safety scanner for skill
content, and a `skillevaluation scan` CLI subcommand.

- `skillevaluation.safety.scan_skill_content(...)` — a pure function (stdlib-only,
  no DB/network/LLM) that flags committed secrets, remote-code-execution / reverse
  shells, data-exfiltration, cloud-metadata/SSRF, destructive commands, hidden /
  look-alike unicode, over-broad tools, and more, grouped by category and
  context-aware (documentation of an attack is downgraded, not blocked).
- `safety.to_sarif(...)` — SARIF 2.1.0 output for GitHub code scanning.
- `skillevaluation scan PATH [--format text|json|sarif] [--fail-on blocked|flagged|
  never] [-o FILE]` — exit 0 (pass) / 1 (>= fail-on) / 2 (usage).

Additive — no change to the eval.yaml spec or the runner.

## [0.3.0] — 2026-07-04

Spec 0.3.0: the trigger-evaluation file format + reserved wire shape, the error-dominated
honesty floor, and a declarative `setup.files` form. All additive to the eval.yaml format —
existing suites parse unchanged — but note: **strict 0.2.x parsers will reject suites that use
the new `should_trigger` field or the `setup` mapping form** (unknown-key/shape strictness is
the point), so pin `skillevaluation>=0.3.0` before adopting them.

### Added
- **`should_trigger` case field** (parser + JSON Schema + `spec/eval-yaml.md`): declares a
  trigger-evaluation case — `true` = this prompt SHOULD surface the skill, `false` = a near-miss
  that should NOT. A case with `should_trigger` and **no** grader is a **trigger-only** case,
  exempt from the at-least-one-grader rule; `should_trigger` also composes with graded fields.
  Trigger-only cases are always **excluded from the A/B loop with disclosure**
  (`cases_skipped_trigger_only` in `results.json`, the delta table, and `validate` output).
  `parser.is_trigger_only_case()` is the shared classifier.
- **Menu-selection trigger execution** (`runner/trigger.py` — removed in 0.6.0, see that entry
  and ADR-0007): `should_trigger` cases now RUN by default (when the adapter supports
  completions) instead of being skipped.
  Each case gets a menu — a markdown table of the target skill's row (frontmatter
  name + description) plus **K=9 distractor rows**, selected deterministically (sorted by name,
  first K, target excluded, no RNG) from a `--distractors DIR` pool of sibling skill dirs or a
  builtin fallback pool shipped as package data — followed by the case prompt and the standard
  activation instruction. One single-shot completion per trial (`trials` per case, capped at
  10; one arm only — no with/without split); **fired** = the normative activation-statement
  regex names the TARGET (`trigger.case_fired`); a case counts as fired only if it fired in
  **ALL trials** (pass^k consistency), per-trial records retained. `--skip-trigger-cases`
  restores the exclusion-only behavior; the mock adapter answers menus with a deterministic
  pick, so the rail runs networkless in CI.
- **`trigger_metrics` aggregate block is now emitted** (`test-run-result.schema.json`;
  previously reserved): `{router_recall, menu_selection_rate, false_fire_rate, cases, errors}`
  with null-not-zero semantics (a bucket with no eligible case reports `null`, never a
  fabricated 0). `router_recall` is ALWAYS `null` from the reference runner — grading retrieval needs a live
  router index, which a local runner does not have (reserved). Per-case records ride the new
  `trigger_cases` array. Runners that don't execute the stage omit both;
  consumers MUST accept documents with or without them.
- **`--fail-on-trigger RECALL_FLOOR,FALSE_FIRE_CEILING` CI gate** (e.g. `0.8,0.2`): exit 1 when
  `menu_selection_rate < floor` or `false_fire_rate > ceiling`. Only evaluates when the suite
  declares trigger cases; declared-but-unmeasured trigger cases (stage skipped, bucket
  all-errored) FAIL the gate — an unproven floor never passes (same discipline as
  `--min-delta-pts` on a withheld delta).
- **`setup.files` mapping form**: `setup:` may now be `{files: {relpath: content}, commands:
  [...]}` — files are written into the case workspace BEFORE any command runs, with the same
  path-escape rejection as bundled skill files (absolute/`..` paths refused). The legacy
  list-of-commands form is unchanged and equivalent to commands-only.
- **Error-dominated floor** (normative in `spec/runner-contract.md`):
  when errored cases / total cases > 0.25, the aggregate sets `error_dominated: true` and nulls
  the headline `pass_rate.delta_pts` (per-arm rates + disclosure counts stay); the run verdict
  becomes `error`, and the CLI's `--fail-on-verdict` / `--min-delta-pts` gates treat the run as
  failing regardless of the verdicts listed — an invalid measurement can never clear a CI gate.
  Exactly 25% does not trip the floor (`aggregation.ERROR_DOMINATED_FLOOR`).
- Conformance goldens: `parser/{accepts-trigger-only-case,accepts-setup-files-mapping}`,
  `aggregation/{error-dominated-floor,error-floor-at-boundary}`.

### Changed
- **Conformance goldens regenerated for the new output shape**: every `aggregation/*/expected.json`
  now carries `error_dominated`; `aggregation/error-excluded` (1 error / 2 cases = 50%) now trips
  the floor, so its `pass_rate.delta_pts` is `null` (the errors-excluded-from-averages behavior it
  pins is unchanged — see `error-floor-at-boundary` for the not-tripped variant); every
  `parser/*/expected.json` case carries `setup_files` + `should_trigger`.
- `--min-delta-pts` no longer crashes (`None < float`) on a run whose headline delta is null
  (degenerate or error-dominated) — the gate fails with an explicit "delta is n/a" message.

## [0.2.4] — 2026-06-24

Audit-driven hardening release: honesty fixes, multi-turn **grading**
conformance coverage, security hardening of the published runner, and better first-run examples.

### Security
- **Explore SQLite sandbox hardening** (`explore.py`): the explore env runs untrusted publisher SQL
  (`environment.setup` + the agent's `run_sql`) on whoever runs the suite. The throwaway connection is
  now locked down before any untrusted SQL touches it — an authorizer denies `ATTACH`/`DETACH`
  (host-file access) and `load_extension`/`readfile`/`writefile`-style functions, a VM-instruction
  budget aborts runaway queries (recursive CTE / giant cross-join → no hang/OOM), and agent reads use
  `fetchmany` so a huge result is never materialized. Normal seeded-DB exploration is unaffected.
- **LLM-judge prompt-injection defense** (`runner/judge.py`): the agent's transcript/output is now
  fenced in explicit `AGENT_TRANSCRIPT`/`AGENT_OUTPUT` blocks with a rubric line marking them as data,
  never instructions — so skill-induced text ("return passed: true") can't flip the judge.
- **Validator/setup filesystem confinement** (`runner/validators.py`, `runner/workspace.py`): `HOME`
  and `TMPDIR` are repointed into the per-case workspace (a `$HOME`-relative read can't reach
  `~/.aws/credentials`), and validator **network isolation is now default-on** when the host can create
  a `unshare -rn` namespace (fail-open only where unavailable). The README no longer overstates this as
  a full sandbox.
- **`--export-url` refuses plaintext `http://`** to a non-local host (the doc carries full
  prompts/transcripts) unless `SKILLEVAL_EXPORT_INSECURE=1`.

### Added
- **Role-aware multi-turn completion**: the completion primitive accepts a `role` of
  `agent`/`simulate`/`label`, so the honesty-critical event labeler runs at temperature 0 (a
  reproducible state-machine verdict) while the agent reply may sample. A legacy 2-arg `complete_fn`
  is auto-wrapped — fully backward compatible.
- **Conformance goldens that pin multi-turn GRADING** (not just parsing): `conversation/grade-policy`
  (SAFETY/LIVENESS/ALWAYS state machine), `explore/computed-gold`, `parser/{conversation-policy-check,
  explore-environment}-only`, degenerate-aggregation (`all-errored`/`all-skipped`/`empty`), and
  cross-language portability (`trajectory/{duration-rounding,tool-args-portability,truncation-boundary}`).
- **Two honest, runnable examples**: `api-error-envelope` (single-shot, an instantly-relatable house
  API contract the base model can't guess) replaces `gdpr-pii-classifier` (which couldn't reproduce its
  numbers under the default adapter), and `refund-policy-conversation` demonstrates `conversation` mode.
  Both ship **in the wheel**, and `skillevaluation run <example-name>` now resolves a packaged example.
- **`resources.build_registry()` / `load_validator()` / `example_path()`** for offline `$ref`
  resolution and packaged-example access. `test-case-result.schema.json` inlines `judgeResult` so a
  naive `jsonschema.validate` resolves with no network.

### Changed
- **`trials` / pass^k now executes in the reference runner.** `run_suite` runs `trials` i.i.d. rollouts
  per case (default 1 = unchanged) so the OSS aggregate reports a real `pass^k` (`k = trials`) instead of
  always `k = 1`. The attestation carries the computed `pass_at_k` so it survives the one-row-per-name import.
- **Honest N/A on degenerate runs**: `pass_rate.{with_skill,without_skill,delta_pts}` are emitted as
  `null` when `cases_aggregated == 0` (every case errored or was apples-to-oranges skipped) instead of a
  fabricated `0% / +0 pts`. The schema relaxes those fields to nullable.
- **Deterministic rounding**: every emitted numeric is rounded **half away from zero on the decimal
  value** (not IEEE-754 banker's rounding), so a tie value matches across language implementations.
- **Explore numeric gold** is matched by scalar equality, not substring — a small integer in incidental
  prose (`"5 tables"`) no longer false-positives.
- **Conversation labeler fails safe** on the `always_forbidden` axis (a missed forbidden act no longer
  maps to a no-op and passes), and the parser **requires `policy_check` + `simulator.ladder` for
  `conversation` at parse time** (mirroring explore) so a malformed case fails before an LLM run.
- **`task_attempted` is carried in the replay/attestation bundle meta** so a re-grade from the bundle
  honors the same apples-to-oranges skip rule the original run used (no replay-vs-original drift).
- **Baseline cache scope** folds the claude-code adapter's `max_turns` into its identity so a config
  change invalidates a stale baseline.

### Documented (spec ↔ code reconciliation)
- `spec/eval-yaml.md`: corrected the **conversation grader** (a `policy_check` state machine, **not**
  `expectations`); documented `environment` / `simulator` / `policy_check` / `trials` with their
  structure; replaced the at-least-one-*assertion* rule with the mode-aware at-least-one-**grader** rule.
- `spec/runner-contract.md` + `schemas/test-run-result.schema.json`: documented the
  `*_correctness_gated` efficiency deltas and the `pass_at_k` aggregate field; made rounding, the
  degenerate-run N/A contract, and the baseline-cache `trial` field normative.
- `spec/trajectory-format.md`: made tool-arg JSON serialization (separators, source key order,
  `ensure_ascii=False`), the half-up duration rounding, and **codepoint** (not byte) truncation normative.
- `schemas/eval-yaml.schema.json`: the grader requirement is now **mode-conditional** (`if/then`), so a
  conversation case graded only by `policy_check` and an explore case graded only by `environment` are
  accepted — matching the parser.
- `CONFORMANCE.md`: **multi-turn grading** now requires the new `conversation`/`explore`/multi-turn-parser
  goldens (not just that the modes parse), and names the per-turn labeler as the trust boundary.

## [0.2.3] — 2026-06-21

### Added
- **Multi-turn rollout runners** — two new shared, honest A/B implementations, so every caller grades
  multi-turn cases through one code path instead of reimplementing it:
  - `skillevaluation.conversation.run_conversation_with_complete` — `conversation` mode: an LLM
    user-simulator drives the dialogue while the harness owns the intent ladder + turn counter; graded
    by a pure-code state machine over a labeled event log (prose is never the grader). The label matcher
    is normalization-hardened so an LLM re-spelling `not_refunded` as `not refunded`/`not-refunded`
    can't flip a safety/liveness verdict.
  - `skillevaluation.explore.run_explore_with_complete` — `explore`/`agentic` mode: a tool-loop over a
    seeded environment, graded against a COMPUTED gold (correctness-gated efficiency).
- **Multi-turn `eval.yaml` fields** in the parser + JSON Schema: `mode` (`single_shot` default /
  `agentic` / `explore` / `conversation`), `user_goal` (required for `conversation`), `max_turns`, and
  the `environment` / `simulator` / `policy_check` grader inputs. All additive — existing single-shot
  suites parse unchanged.

## [0.2.2] — 2026-06-15

### Fixed
- **Workspace `setup:` step sandbox hardening** (completes the 0.2.1 validator scrub): `prepare_workspace()` setup commands — the `setup:` shell from an eval.yaml — no longer inherit the full parent environment. They run with the same scrubbed `PATH`/`HOME`/`TMPDIR`/… allowlist as validators, so a malicious `setup:` step cannot read your credentials. Previously only the *validator* subprocess was scrubbed; the setup steps were not.
- Strict-mypy annotations on `cli.py` (dict type-args, `DeltaResult`) so the package type-checks clean under CI.

## [0.2.1] — 2026-06-05

### Fixed
- `LLMClient` now retries transient provider failures (429 / 500 / 502 / 503 / 529, connect errors, timeouts) with exponential backoff (2s/4s/8s, honoring `Retry-After`), instead of erroring the arm on the first hiccup. Found on first real-world walkthrough: free-tier Gemini 503s on a 3-case suite's burst of back-to-back calls. Non-transient errors (401 bad key, 400) still raise immediately.
- **Per-arm workspace isolation**: each arm now gets its own workspace prepared from the same `setup` steps. Previously both arms shared one directory, so a file-writing agent's with-skill artifacts were readable by the without-skill arm (and graded by its validators), silently biasing `flip_to_pass` down to `pass_kept`. Even chat-only runs could distort: a validator with side effects (`mkdir out && …`) passed on one arm and failed on the other purely from ordering.
- The Gemini API key moved from a `?key=` query parameter to the `x-goog-api-key` header. httpx error strings embed the full request URL and those strings flow into `results.json` (and any `--export-url` upload) — the query-param form leaked the key into both.
- Validator runs no longer crash the suite on spawn failures: `$RESPONSE_TEXT` is capped at 64k chars (single env entries have a hard OS ceiling — `response.txt` always carries the full output), and unspawnable commands (E2BIG, NUL bytes) fail that one validator instead of raising out of the run after the arms already spent tokens.
- **Validator sandbox hardening**: the validator subprocess no longer inherits the full environment (scrubbed to a safe `PATH`/`HOME`/`TMPDIR`/… allowlist, so the caller's credentials aren't visible to the validator), and now runs under an OS resource sandbox — a `preexec_fn` clamps CPU time, max file size, and core dumps (`RLIMIT_CPU`/`FSIZE`/`CORE`). Optional network isolation via `DECIMAL_VALIDATOR_SANDBOX_NET=unshare` wraps the command in `unshare -rn` (no network → closes metadata-server SSRF) when the host supports a user+net namespace; default off so it never spuriously fails where unsupported.
- Structural-assertion directives (`response_contains:`/`response_matches:`) now slice the needle from the *stripped* expectation — leading whitespace previously extracted a corrupted needle — and empty directives fail loudly instead of vacuously passing.
- `ClaudeCodeAdapter` synthesized frontmatter quotes the description (a first line containing a colon or quote no longer corrupts the YAML block).

### Added
- Case workspaces are removed after grading; `--keep-workspaces` (CLI) / `run_suite(keep_workspaces=True)` preserves them for debugging. A 200-case suite previously left 200 tmpdirs behind.
- Suites whose expectations are **all structural** no longer require `--model`/`--judge-model` (or any judge API key): the judge wiring is skipped entirely (`skillevaluation.runner.judge.suite_needs_llm_judge`).

## [0.2.0] — 2026-06-05

The package can now *produce* a delta, not just score one: this release adds the reference runner and the `skillevaluation` CLI ([ADR 0006](adrs/0006-reference-runner.md)). Everything runs locally on your own API key; nothing is uploaded unless you pass `--export-url`.

### Added
- `skillevaluation.runner` subpackage — the reference implementation of `spec/runner-contract.md`:
  - `orchestrator.run_suite()` — the full A/B loop (workspace → both arms → symmetric grading → outcome classification → aggregation)
  - `judge` — reference LLM judge (injectable transport; prompt wording remains non-normative per the spec) + structural assertion fast path (`response_is_valid_json`, `response_contains:`, …)
  - `validators` / `workspace` — sandboxed shell assertions with `response.txt` / `$RESPONSE_TEXT` staging; strict (spec) and lenient setup modes
  - `cache.BaselineCache` — local file baseline cache over the spec'd key derivation, scoped by adapter identity so a model swap invalidates baselines
  - `adapters` — the invocation seam: `LLMAdapter` (direct Anthropic/OpenAI/Gemini completion, your own key — the supported adapter), `MockAdapter` + `mock_judge_call` (deterministic, networkless), `ClaudeCodeAdapter` (EXPERIMENTAL — drives a local `claude` CLI with the skill staged as a project skill)
- `skillevaluation` CLI (`[project.scripts]`): `run` (delta table + `results.json` + optional per-arm transcripts + `--export-url` POST) and `validate`; CI gates via `--fail-on-verdict` and `--min-delta-pts`
- `skillevaluation.resources` — `load_schema()` / `read_spec()`; **the wheel now ships `spec/` and `schemas/`** so the canonical artifacts install with the package
- `[runner]` extra (`httpx`); the core install stays PyYAML-only
- `results.json` documents validate against `schemas/test-run-result.schema.json` (cases against `test-case-result.schema.json`), stamped `format: skillevaluation/test-run-result@v1`

### Changed
- `spec/eval-yaml.md` + `spec/runner-contract.md` now state explicitly that `setup` and `validators` run with the case workspace as the working directory and that the arm's output is staged as `response.txt` / `$RESPONSE_TEXT` (canonizing existing reference behavior); suites SHOULD use workspace-relative paths
- `examples/gdpr-pii-classifier/eval.yaml` rewritten to workspace-relative paths (absolute `/workspace/...` only worked on container runners)

## [0.1.1] — 2026-05-30

De-branded from "Claude Code skills" to skills in general — `skillevaluation` works with any skill format, not a single vendor's. No code or API changes.

### Changed
- README and package `description` now describe generic skills rather than "Claude Code" skills
- Removed the "Composing with agentversion" section from the README

## [0.1.0] — 2026-05-28

Initial release. Pre-stable; breaking changes expected before v1.0.

### Added
- `skillevaluation.parser` — parses `eval.yaml` files with strict validation
- `skillevaluation.outcomes` — outcome classifier (`flip_to_pass` / `pass_kept` / `fail_kept` / `flip_to_fail` / `error`)
- `skillevaluation.aggregation` — per-dimension delta math + apples-to-oranges skip rule
- `skillevaluation.baseline` — baseline-cache key derivation
- `skillevaluation.trajectory.format_v1` — canonical trajectory text rendering for LLM judges
- `spec/` — file format, runner contract, judge contract, trajectory format docs
- `schemas/` — JSON Schemas for inputs and outputs
- `compatibility-tests/` — golden in/out pairs that conforming runners must reproduce
