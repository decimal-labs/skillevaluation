"""Self-conformance: the reference runner reproduces the outcome goldens
END TO END — through `run_suite` (workspace → arms → grading → classify),
not by calling `classify_outcome` directly (tests/test_conformance.py
already covers the bare functions).

Each `compatibility-tests/outcomes/*/input.json` fixture declares
`{with_passed, without_passed, errored}`; we build an adapter whose two
arms produce exactly those grading results and assert the orchestrated
case lands on `expected.json`'s outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillevaluation.parser import parse_eval_yaml
from skillevaluation.runner.adapters.mock import MockAdapter, mock_judge_call
from skillevaluation.runner.orchestrator import run_suite

FIXTURES = sorted(
    (Path(__file__).resolve().parent.parent / "compatibility-tests" / "outcomes").iterdir()
)

# One case; the expectation passes iff the arm's response contains MAGIC.
# Two-category model (2026-06-13): expectations are LLM-judged (here, the substring
# mock_judge_call) — structural prefixes live in validators now, so this is a plain claim.
SUITE = """
cases:
  - name: golden
    prompt: "the prompt"
    expectations:
      - "MAGIC"
"""

_PASSING_TEXT = "this response contains MAGIC and is long enough to count"
_FAILING_TEXT = "this response is long enough to count but lacks the token"


class _GoldenAdapter(MockAdapter):
    """Arms produce pass/fail/error exactly as the fixture dictates."""

    def __init__(self, *, with_passed: bool, without_passed: bool, errored: bool):
        super().__init__(
            lambda prompt, with_skill: (
                _PASSING_TEXT if (with_passed if with_skill else without_passed)
                else _FAILING_TEXT
            )
        )
        self._errored = errored

    def run(self, *, prompt, skill_body, workspace):
        arm = super().run(prompt=prompt, skill_body=skill_body, workspace=workspace)
        if self._errored and skill_body is not None:
            # Per the fixtures, `errored` means at least one arm failed to
            # execute; erroring the with-arm exercises the override path
            # (error-overrides has both pass flags true).
            arm.errored = True
            arm.error = "simulated infra failure"
            arm.final_output = ""
        return arm


@pytest.mark.parametrize(
    "fixture_dir", FIXTURES, ids=[d.name for d in FIXTURES]
)
def test_run_suite_reproduces_outcome_golden(fixture_dir):
    spec_input = json.loads((fixture_dir / "input.json").read_text())
    expected = json.loads((fixture_dir / "expected.json").read_text())

    adapter = _GoldenAdapter(
        with_passed=spec_input["with_passed"],
        without_passed=spec_input["without_passed"],
        errored=spec_input["errored"],
    )
    result = run_suite(
        parse_eval_yaml(SUITE),
        adapter,
        skill_name="golden-skill",
        skill_body="# Golden\nbody",
        judge_call=mock_judge_call,
    )
    assert result.cases[0].outcome == expected["outcome"], (
        f"{fixture_dir.name}: run_suite produced {result.cases[0].outcome!r}, "
        f"golden expects {expected['outcome']!r}"
    )
