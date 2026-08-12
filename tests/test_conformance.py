"""Conformance suite — drives the golden in/out fixtures under compatibility-tests/.

This module IS the conformance test. An alternate-language implementation
of skillevaluation should reproduce each `expected.*` for the corresponding
`input.*` byte-for-byte (texts) or value-equal (JSON).

See CONFORMANCE.md for the rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skillevaluation.aggregation import CaseMetrics, CaseResult, compute_run_aggregates
from skillevaluation.outcomes import classify_outcome, compute_verdict
from skillevaluation.parser import EvalYamlParseError, parse_eval_yaml
from skillevaluation.trajectory.format_v1 import build_transcript_v1

ROOT = Path(__file__).resolve().parent.parent
COMPAT = ROOT / "compatibility-tests"


# ── Parser ─────────────────────────────────────────────────────────────


def _parser_happy_dirs():
    return sorted(d for d in (COMPAT / "parser").iterdir() if (d / "expected.json").exists())


def _parser_error_dirs():
    return sorted(d for d in (COMPAT / "parser").iterdir() if (d / "expected-error.json").exists())


@pytest.mark.parametrize("fixture_dir", _parser_happy_dirs(), ids=lambda p: p.name)
def test_parser_happy(fixture_dir: Path):
    input_yaml = (fixture_dir / "input.yaml").read_text()
    expected = json.loads((fixture_dir / "expected.json").read_text())

    parsed = parse_eval_yaml(input_yaml)
    actual = {
        "cases": [
            {
                "name": c.name,
                "prompt": c.prompt,
                "setup_steps": c.setup_steps,
                # setup mapping form (spec 0.3.0) — legacy list-form suites carry {}.
                "setup_files": c.setup_files,
                "expectations": c.expectations,
                "script_validators": c.script_validators,
                "tags": c.tags,
                "description": c.description,
                # Optional step cap on the single agent invocation (schema rev 2).
                "max_turns": c.max_turns,
                # Trigger evaluation (spec 0.3.0) — None when the case is not a trigger case.
                "should_trigger": c.should_trigger,
            }
            for c in parsed
        ]
    }
    assert actual == expected


@pytest.mark.parametrize("fixture_dir", _parser_error_dirs(), ids=lambda p: p.name)
def test_parser_error(fixture_dir: Path):
    input_yaml = (fixture_dir / "input.yaml").read_text()
    expected = json.loads((fixture_dir / "expected-error.json").read_text())

    with pytest.raises(EvalYamlParseError) as exc_info:
        parse_eval_yaml(input_yaml)

    assert exc_info.type.__name__ == expected["error_class"]
    msg = str(exc_info.value)
    for kw in expected["message_keywords"]:
        assert kw in msg, f"expected keyword {kw!r} not in error message {msg!r}"


# ── Outcomes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fixture_dir",
    sorted((COMPAT / "outcomes").iterdir()),
    ids=lambda p: p.name,
)
def test_outcome_classification(fixture_dir: Path):
    input_data = json.loads((fixture_dir / "input.json").read_text())
    expected = json.loads((fixture_dir / "expected.json").read_text())

    result = classify_outcome(
        with_passed=input_data["with_passed"],
        without_passed=input_data["without_passed"],
        errored=input_data.get("errored", False),
    )
    assert result == expected["outcome"]


# ── Verdict ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fixture_dir",
    sorted((COMPAT / "verdict").iterdir()),
    ids=lambda p: p.name,
)
def test_run_verdict(fixture_dir: Path):
    input_data = json.loads((fixture_dir / "input.json").read_text())
    expected = json.loads((fixture_dir / "expected.json").read_text())

    assert compute_verdict(input_data["outcomes"]) == expected["verdict"]


# ── Aggregation ────────────────────────────────────────────────────────


def _make_case_result(d: dict) -> CaseResult:
    """Build a CaseResult from the input.json shape."""
    return CaseResult(
        case_name=d["case_name"],
        outcome=d["outcome"],
        with_skill=CaseMetrics(**d["with_skill"]) if "with_skill" in d else None,
        without_skill=CaseMetrics(**d["without_skill"]) if "without_skill" in d else None,
    )


@pytest.mark.parametrize(
    "fixture_dir",
    sorted((COMPAT / "aggregation").iterdir()),
    ids=lambda p: p.name,
)
def test_aggregation(fixture_dir: Path):
    input_data = json.loads((fixture_dir / "input.json").read_text())
    expected = json.loads((fixture_dir / "expected.json").read_text())

    case_results = [_make_case_result(d) for d in input_data["case_results"]]
    actual = compute_run_aggregates(case_results).to_dict()
    assert actual == expected


# ── Trajectory ─────────────────────────────────────────────────────────


def _dict_to_namespace(d: dict):
    """Shallow conversion: dict → SimpleNamespace, so getattr works."""
    from types import SimpleNamespace
    if not isinstance(d, dict):
        return d
    ns = SimpleNamespace()
    for k, v in d.items():
        setattr(ns, k, v)
    return ns


@pytest.mark.parametrize(
    "fixture_dir",
    sorted((COMPAT / "trajectory").iterdir()),
    ids=lambda p: p.name,
)
def test_trajectory_byte_equal(fixture_dir: Path):
    input_data = json.loads((fixture_dir / "input.json").read_text())
    expected = (fixture_dir / "expected.txt").read_text()
    # Strip a trailing newline if the file editor added one — the
    # canonical form does NOT terminate with a newline.
    if expected.endswith("\n"):
        expected = expected[:-1]

    calls = [_dict_to_namespace(c) for c in input_data.get("llm_calls", [])]
    spans = [_dict_to_namespace(s) for s in input_data.get("spans") or []]

    actual = build_transcript_v1(
        user_input=input_data.get("user_input"),
        final_output=input_data.get("final_output"),
        llm_calls=calls,
        spans=spans,
    )
    assert actual == expected


# ── Example smoke ──────────────────────────────────────────────────────


def test_worked_examples_parse_and_validate_against_schema():
    """EVERY shipped example MUST both parse AND validate against the eval-yaml schema — a single
    check that the hand-rolled parser and the published schema agree on real, shipped suites."""
    import jsonschema

    schema = json.loads((ROOT / "schemas" / "eval-yaml.schema.json").read_text())
    example_dirs = sorted(p for p in (ROOT / "examples").iterdir() if (p / "eval.yaml").is_file())
    assert example_dirs, "no examples found to validate"
    for d in example_dirs:
        text = (d / "eval.yaml").read_text()
        jsonschema.validate(yaml.safe_load(text), schema)  # schema accepts it
        parsed = parse_eval_yaml(text)                      # parser accepts it
        assert len(parsed) >= 1 and all(c.name for c in parsed), d.name
