"""Every shipped example runs end-to-end through the reference runner (mock adapter, no network) and
emits an aggregate that validates against ``test-run-result.schema.json``.

SHAPE-only by design: the checked-in ``expected-run-result.json`` files are explicitly ILLUSTRATIVE
(not golden VALUES — real numbers come from the user's own run), so we assert the runner EXECUTES
and the emitted wire shape is valid, never specific deltas. This closes the "no example ever flows
through the runner in CI" gap without manufacturing false golden assertions.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from skillevaluation.resources import load_schema
from skillevaluation.runner.adapters.mock import MockAdapter, mock_judge_call
from skillevaluation.runner.orchestrator import run_suite
from skillevaluation.runner.skill_dir import load_skill_dir

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIRS = sorted(p for p in (ROOT / "examples").iterdir() if (p / "eval.yaml").is_file())


def test_examples_exist():
    # Guard against an empty parametrization silently passing (e.g. a packaging/path regression).
    names = {d.name for d in EXAMPLE_DIRS}
    assert {"api-error-envelope", "commit-conventions", "refund-policy"} <= names


@pytest.mark.parametrize("example_dir", EXAMPLE_DIRS, ids=[d.name for d in EXAMPLE_DIRS])
def test_example_runs_and_aggregate_validates(example_dir):
    skill = load_skill_dir(str(example_dir))
    result = run_suite(
        skill.cases,
        MockAdapter(),
        skill_name=skill.name,
        skill_body=skill.body,
        judge_call=mock_judge_call,
        files=skill.files or None,
    )
    doc = result.to_results_json()
    jsonschema.validate(doc, load_schema("test-run-result"))
    assert doc["total_cases"] == len(skill.cases)
    assert doc["verdict"] in ("pass", "fail", "mixed", "error")


@pytest.mark.parametrize("example_dir", EXAMPLE_DIRS, ids=[d.name for d in EXAMPLE_DIRS])
def test_example_expected_result_shape_validates(example_dir):
    """The illustrative expected-run-result.json must still validate against the wire schema (modulo
    documented extra keys like _comment/_note) — so an example's 'shape documentation' can't
    silently diverge from the schema the runner actually emits."""
    f = example_dir / "expected-run-result.json"
    if not f.is_file():
        pytest.skip("no expected-run-result.json")
    doc = json.loads(f.read_text())
    jsonschema.validate(doc, load_schema("test-run-result"))
    # The runner emits per-case rows under `cases` (never `case_results`); guard the old typo.
    assert "case_results" not in doc, "use `cases`, not `case_results` (matches the runner output)"
