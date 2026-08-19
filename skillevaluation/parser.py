"""eval.yaml parser.

A skill's ``eval.yaml`` file lives next to ``SKILL.md`` and declares a
suite of test cases a conforming runner exercises against the agent
both with and without the skill. See ``spec/eval-yaml.md`` for the
canonical format.

Example::

    cases:
      - name: tracks_with_id
        prompt: "Classify these schema fields: email, ip_address, name, age."
        setup:
          - "echo '{...}' > /workspace/schema.json"
        expectations:
          - "The response classifies email as PII"
          - "The response does not over-classify"
        validators:
          - cmd: "test -f /workspace/output.json"

This module is intentionally narrow:
  * ``parse_eval_yaml(text) -> list[ParsedTestCase]``
  * ``is_trigger_only_case(case)`` — the spec-0.3.0 trigger-only classifier
  * Strict validation — every field is type-checked against the spec
  * Errors carry the offending case name + field for actionable messages

It does **not** persist anything or invoke an agent — those concerns
belong to a runner implementation.

Schema rev 2 (2026-07-15, ADR-0007): ONE execution contract — the agent is
invoked once per case in a prepared workspace and may take many tool steps
(``max_turns`` caps them); the whole trajectory is recorded and graded by
``expectations`` (LLM judge over the transcript) + ``validators`` (code).
The rev-1 ``mode`` enum (single_shot/agentic/explore/conversation) and its
per-mode fields (``user_goal``, ``environment``, ``simulator``,
``policy_check``, per-case ``trials``) are REMOVED — a case that carries
them is rejected loudly as unknown keys, pointing authors at the migration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger("skillevaluation.parser")

# Allowed keys at each level, mirroring `additionalProperties: false` in
# schemas/eval-yaml.schema.json. Unknown keys are rejected so a typo like
# `expectation:` (singular) fails loudly instead of being silently dropped.
_ALLOWED_TOP_LEVEL_KEYS = frozenset({"cases"})
_ALLOWED_CASE_KEYS = frozenset(
    {"name", "prompt", "setup", "expectations", "validators", "tags", "description",
     # Optional step cap on the (single) agent invocation's tool loop.
     "max_turns",
     # Trigger evaluation (spec 0.3.0): should this prompt surface the skill?
     # A case may be trigger-only (no grader) or carry should_trigger alongside graders.
     "should_trigger"}
)
# Schema rev-1 keys removed in rev 2 (ADR-0007). Named here ONLY to give authors a
# targeted migration error instead of a generic unknown-key message.
_REMOVED_REV1_KEYS = frozenset(
    {"mode", "user_goal", "environment", "simulator", "policy_check", "trials"}
)
_ALLOWED_VALIDATOR_KEYS = frozenset({"cmd", "expect_exit_code", "label"})
# `setup:` mapping form (spec 0.3.0): {files: {relpath: content}, commands: [...]}. The legacy
# list-of-shell-strings form stays valid and is equivalent to commands-only.
_ALLOWED_SETUP_KEYS = frozenset({"files", "commands"})


def _unknown_keys(mapping: dict[Any, Any], allowed: frozenset[str]) -> list[str]:
    """Return the mapping's keys not in ``allowed``, sorted for determinism."""
    return sorted(str(k) for k in mapping if k not in allowed)


class EvalYamlParseError(ValueError):
    """Raised when eval.yaml is malformed."""


@dataclass
class ParsedTestCase:
    """One test case from eval.yaml, validated + normalized."""

    name: str
    prompt: str
    setup_steps: list[str] = field(default_factory=list)
    # setup mapping form (spec 0.3.0): {relative_path: content} written into the case workspace
    # BEFORE any setup command runs. The legacy list-of-commands `setup:` leaves this empty.
    setup_files: dict[str, str] = field(default_factory=dict)
    expectations: list[str] = field(default_factory=list)
    script_validators: list[dict[str, Any]] = field(default_factory=list)

    # Optional metadata (publishers can tag tests for organization).
    tags: list[str] = field(default_factory=list)
    description: str | None = None

    # Optional cap on the agent's tool-loop steps for this case (rev 2: one execution
    # contract — every case is a single invocation that may take many steps).
    max_turns: int | None = None
    # Trigger evaluation (spec 0.3.0): True = this prompt SHOULD surface the skill; False = a
    # near-miss that should NOT. None = not a trigger case. Trigger grading is a separate rail;
    # a case with should_trigger and no grader is TRIGGER-ONLY (see is_trigger_only_case).
    should_trigger: bool | None = None


def parse_eval_yaml(text: str) -> list[ParsedTestCase]:
    """Parse + validate the contents of an eval.yaml file.

    Args:
        text: full file contents as a string

    Returns:
        list of ParsedTestCase

    Raises:
        EvalYamlParseError: on any structural problem. The message names
            the offending case + field so the caller can render a useful
            error like `eval.yaml: case "tracks_with_id" — 'prompt' must
            be a string, got int`.
    """
    if not text or not text.strip():
        raise EvalYamlParseError("eval.yaml is empty")

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EvalYamlParseError(f"eval.yaml is not valid YAML: {exc}") from exc

    if not isinstance(doc, dict):
        raise EvalYamlParseError(
            f"eval.yaml top-level must be a mapping with a `cases:` list, got {type(doc).__name__}"
        )

    cases = doc.get("cases")
    if not isinstance(cases, list):
        raise EvalYamlParseError(
            "eval.yaml must declare a top-level `cases:` list"
        )
    if not cases:
        raise EvalYamlParseError("eval.yaml `cases:` list is empty")

    # The required `cases:` check runs first so that a doc missing it gets
    # the "declare a cases list" message; only once cases is valid do we
    # reject sibling keys (a typo like `setup:` at the top level).
    unknown_top = _unknown_keys(doc, _ALLOWED_TOP_LEVEL_KEYS)
    if unknown_top:
        raise EvalYamlParseError(
            f"eval.yaml: unknown top-level key(s): {', '.join(unknown_top)} — "
            f"only 'cases' is allowed"
        )

    seen_names: set[str] = set()
    parsed: list[ParsedTestCase] = []

    for idx, raw in enumerate(cases):
        if not isinstance(raw, dict):
            raise EvalYamlParseError(
                f"eval.yaml: case at index {idx} must be a mapping, got {type(raw).__name__}"
            )

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise EvalYamlParseError(
                f"eval.yaml: case at index {idx} is missing a non-empty 'name'"
            )
        name = name.strip()

        if name in seen_names:
            raise EvalYamlParseError(
                f"eval.yaml: duplicate case name '{name}' — names must be unique within a suite"
            )
        seen_names.add(name)

        unknown_case = _unknown_keys(raw, _ALLOWED_CASE_KEYS)
        if unknown_case:
            removed = sorted(k for k in unknown_case if k in _REMOVED_REV1_KEYS)
            if removed:
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — key(s) removed in schema rev 2: "
                    f"{', '.join(removed)}. There is one execution contract now: the agent runs "
                    f"once per case in the prepared workspace (setup) and may take many tool "
                    f"steps; grade with 'expectations' (LLM judge over the trajectory) and/or "
                    f"'validators' (code). Repetition is runner-level (--runs), not per-case."
                )
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — unknown key(s): {', '.join(unknown_case)}"
            )

        max_turns = raw.get("max_turns")
        if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool)
                                      or max_turns < 1):
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — 'max_turns' must be a positive integer if present"
            )

        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — 'prompt' must be a non-empty string"
            )

        setup_raw = raw.get("setup") or []
        setup_files: dict[str, str] = {}
        if isinstance(setup_raw, dict):
            # Mapping form (spec 0.3.0): {files: {relpath: content}, commands: [...]}. Files are
            # written into the case workspace BEFORE any command runs.
            unknown_setup = _unknown_keys(setup_raw, _ALLOWED_SETUP_KEYS)
            if unknown_setup:
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — 'setup' mapping has unknown key(s): "
                    f"{', '.join(unknown_setup)} — only 'files' and 'commands' are allowed"
                )
            files_raw = setup_raw.get("files") or {}
            if not isinstance(files_raw, dict) or not all(
                isinstance(k, str) and k.strip() and isinstance(v, str)
                for k, v in files_raw.items()
            ):
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — 'setup.files' must map non-empty relative "
                    f"paths to string file contents"
                )
            setup_files = dict(files_raw)
            setup_steps = setup_raw.get("commands") or []
            if not isinstance(setup_steps, list) or not all(
                isinstance(s, str) for s in setup_steps
            ):
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — 'setup.commands' must be a list of shell "
                    f"command strings"
                )
        else:
            setup_steps = setup_raw
            if not isinstance(setup_steps, list) or not all(
                isinstance(s, str) for s in setup_steps
            ):
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — 'setup' must be a list of shell command "
                    f"strings (or a mapping with 'files' and/or 'commands')"
                )

        expectations = raw.get("expectations") or []
        if not isinstance(expectations, list) or not all(
            isinstance(e, str) and e.strip() for e in expectations
        ):
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — 'expectations' must be a list of non-empty strings"
            )

        validators_raw = raw.get("validators") or []
        if not isinstance(validators_raw, list):
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — 'validators' must be a list"
            )
        script_validators: list[dict[str, Any]] = []
        for v_idx, v in enumerate(validators_raw):
            if not isinstance(v, dict) or "cmd" not in v:
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — validator #{v_idx} must be a "
                    f"mapping with a 'cmd' field"
                )
            unknown_v = _unknown_keys(v, _ALLOWED_VALIDATOR_KEYS)
            if unknown_v:
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — validator #{v_idx} has unknown key(s): "
                    f"{', '.join(unknown_v)}"
                )
            cmd = v["cmd"]
            if not isinstance(cmd, str) or not cmd.strip():
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — validator #{v_idx}.cmd must be a non-empty string"
                )
            expect_exit_code = v.get("expect_exit_code", 0)
            if not isinstance(expect_exit_code, int) or isinstance(expect_exit_code, bool):
                raise EvalYamlParseError(
                    f"eval.yaml: case '{name}' — validator #{v_idx}.expect_exit_code "
                    "must be an integer if present"
                )
            script_validators.append({
                "cmd": cmd,
                "expect_exit_code": expect_exit_code,
                "label": v.get("label") or cmd[:80],
            })

        # Trigger evaluation (spec 0.3.0). Explicit bool check — YAML ints would sneak past a
        # truthiness test (isinstance(1, bool) is False, so 0/1 are rejected loudly).
        should_trigger = raw.get("should_trigger")
        if should_trigger is not None and not isinstance(should_trigger, bool):
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — 'should_trigger' must be a boolean if present"
            )

        # A case with no grader can't be scored: it needs 'expectations' (LLM-judged over the
        # trajectory) or 'validators' (code). A `should_trigger` flag also satisfies the rule:
        # a case with should_trigger and NO grader is TRIGGER-ONLY (spec 0.3.0) — graded by the
        # trigger rail, not the A/B loop.
        has_assertion = bool(
            expectations or script_validators or should_trigger is not None
        )
        if not has_assertion:
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' has neither 'expectations' nor 'validators' (nor a "
                f"'should_trigger' flag) — a benchmark case must have at least one assertion"
            )

        tags = raw.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — 'tags' must be a list of strings"
            )

        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise EvalYamlParseError(
                f"eval.yaml: case '{name}' — 'description' must be a string if present"
            )

        parsed.append(
            ParsedTestCase(
                name=name,
                prompt=prompt,
                setup_steps=list(setup_steps),
                setup_files=setup_files,
                expectations=list(expectations),
                script_validators=script_validators,
                tags=list(tags),
                description=description,
                max_turns=max_turns,
                should_trigger=should_trigger,
            )
        )

    return parsed


def is_trigger_only_case(case: ParsedTestCase) -> bool:
    """True when the case declares ``should_trigger`` and carries NO grader.

    A trigger-only case is exempt from the at-least-one-grader rule: it exists for the trigger
    rail (router/menu grading — a separate contract), not the A/B lift loop. A conforming
    A/B runner SKIPS it with disclosure (``cases_skipped_trigger_only``) rather than erroring.
    """
    return (
        case.should_trigger is not None
        and not case.expectations
        and not case.script_validators
    )
