"""run_suite — the full A/B loop with mock adapter + mock judge."""

from __future__ import annotations

import json

import pytest

from skillevaluation.parser import parse_eval_yaml
from skillevaluation.runner.adapters.mock import MockAdapter, mock_judge_call
from skillevaluation.runner.cache import BaselineCache
from skillevaluation.runner.orchestrator import run_suite

SUITE = """
cases:
  - name: rescued
    prompt: "classify the fields"
    expectations:
      - "labels email as PII"
  - name: still_failing
    prompt: "do the impossible"
    expectations:
      - "solves it perfectly"
"""

RESPONSES = {
    # with-skill rescues `rescued`; nothing helps `still_failing`.
    "classify the fields": {
        "with": "I labels email as PII and ip_address as pseudonymous.",
        "without": "I am not sure what you mean by classification.",
    },
    "do the impossible": {
        "with": "Attempting, but here is something unrelated and long enough.",
        "without": "Attempting, but failing in a different long-enough way.",
    },
}


def _run(cache=None, responses=RESPONSES, suite=SUITE, **kwargs):
    return run_suite(
        parse_eval_yaml(suite),
        MockAdapter(responses),
        skill_name="demo-skill",
        skill_body="# Demo\nAlways label PII.",
        judge_call=mock_judge_call,
        cache=cache,
        **kwargs,
    )


def test_outcomes_classified_per_case():
    result = _run()
    by_name = {c.case_name: c.outcome for c in result.cases}
    assert by_name == {"rescued": "flip_to_pass", "still_failing": "fail_kept"}
    assert result.verdict == "mixed"


def test_aggregate_pass_rate_delta():
    result = _run()
    pr = result.aggregate.pass_rate
    assert pr["with_skill"] == 0.5
    assert pr["without_skill"] == 0.0
    assert pr["delta_pts"] == 50.0


def test_metric_deltas_flow_from_adapter_metrics():
    result = _run()
    tokens = result.aggregate.tokens
    # Mock defaults: with=400, without=900 per case.
    assert tokens.with_skill_avg == 400
    assert tokens.without_skill_avg == 900
    assert tokens.delta_pct == pytest.approx(-55.6, abs=0.1)


def test_both_arms_graded_symmetrically():
    """The without arm is graded with the same assertions (flip detection
    depends on it) — visible in `rescued` not being pass_kept."""
    result = _run()
    rescued = next(c for c in result.cases if c.case_name == "rescued")
    assert rescued.with_arm.passed is True
    assert rescued.without_arm.passed is False


def test_expectation_results_carry_with_arm_detail():
    result = _run()
    rescued = next(c for c in result.cases if c.case_name == "rescued")
    assert rescued.expectation_results[0]["expectation"] == "labels email as PII"
    assert rescued.expectation_results[0]["passed"] is True


def test_setup_failure_is_error_outcome_strict():
    suite = """
cases:
  - name: bad_setup
    prompt: "p"
    setup:
      - "false"
    expectations:
      - "anything"
"""
    result = _run(suite=suite)
    assert result.cases[0].outcome == "error"
    assert "setup step failed" in result.cases[0].error
    assert result.verdict == "error"
    # Schema-required disclosure fields still present.
    assert result.aggregate.errors == 1


def test_setup_failure_lenient_mode_proceeds():
    suite = """
cases:
  - name: bad_setup
    prompt: "classify the fields"
    setup:
      - "false"
    expectations:
      - "labels email as PII"
"""
    result = _run(suite=suite, strict_setup=False)
    assert result.cases[0].outcome == "flip_to_pass"


def test_errored_arm_classifies_error_without_judging():
    judge_calls = []

    def counting_judge(prompt):
        judge_calls.append(prompt)
        return {"passed": True, "reason": "ok"}

    class FlakyAdapter(MockAdapter):
        def run(self, *, prompt, skill_body, workspace):
            arm = super().run(prompt=prompt, skill_body=skill_body, workspace=workspace)
            if skill_body is not None:
                arm.errored = True
                arm.error = "503"
                arm.final_output = ""
            return arm

    result = run_suite(
        parse_eval_yaml(SUITE),
        FlakyAdapter(RESPONSES),
        skill_name="s",
        skill_body="b",
        judge_call=counting_judge,
    )
    assert all(c.outcome == "error" for c in result.cases)
    # Judge ran only for the non-errored (without) arms: 1 expectation × 2 cases.
    assert len(judge_calls) == 2
    assert result.verdict == "error"


def test_judge_transport_error_classifies_case_error_not_fail():
    """A judge-TRANSPORT failure (API down) rolls up into case outcome 'error'
    (excluded from lift), not a model fail. judge_expectations stamps
    ``errored: true`` on the result and the orchestrator ORs it into the case's
    errored flag. Without that rollup glue every case would grade fail_kept (both
    arms 'fail' on the judge exception) — an invisible, wrong 0-lift measurement.
    A judge outage on ONE arm would instead manufacture a flip."""
    def broken_judge(prompt):
        raise RuntimeError("judge api down")

    result = run_suite(
        parse_eval_yaml(SUITE),
        MockAdapter(RESPONSES),
        skill_name="s",
        skill_body="b",
        judge_call=broken_judge,
    )
    assert all(c.outcome == "error" for c in result.cases)
    assert result.verdict == "error"
    # The lift claim is withheld (no comparable case), never a fabricated 0.
    assert result.aggregate.pass_rate["delta_pts"] is None


def test_baseline_cache_hit_skips_without_arm(tmp_path):
    cache = BaselineCache("mock", base_dir=tmp_path)
    first = _run(cache=cache)
    assert first.cache_hits == 0

    adapter = MockAdapter(RESPONSES)
    second = run_suite(
        parse_eval_yaml(SUITE),
        adapter,
        skill_name="demo-skill",
        skill_body="# Demo\nAlways label PII.",
        judge_call=mock_judge_call,
        cache=cache,
    )
    assert second.cache_hits == 2
    # Adapter ran only the with-skill arms on the second run.
    assert [c["with_skill"] for c in adapter.calls] == [True, True]
    # Cached arm is flagged and outcomes unchanged.
    assert all(c.without_arm.cached for c in second.cases)
    assert {c.case_name: c.outcome for c in second.cases} == {
        "rescued": "flip_to_pass", "still_failing": "fail_kept",
    }


def test_cached_baseline_regraded_fresh(tmp_path):
    """Cache stores the EXECUTION; grading must rerun (expectations can change)."""
    cache = BaselineCache("mock", base_dir=tmp_path)
    _run(cache=cache)

    relaxed = """
cases:
  - name: rescued
    prompt: "classify the fields"
    expectations:
      - "not sure"
"""
    result = _run(cache=cache, suite=relaxed)
    # The CACHED without-output ("I am not sure…") passes the relaxed
    # expectation while the with-output doesn't contain "not sure" → the
    # fresh regrade of an old execution yields flip_to_fail. Proves grading
    # ran against the new expectation, not a cached verdict.
    assert result.cases[0].without_arm.cached is True
    assert result.cases[0].outcome == "flip_to_fail"


def test_runs_param_baseline_reruns_live_per_run(tmp_path):
    """With runner-level runs>1 (rev 2), the without-arm must run LIVE each run — sharing one
    baseline cache key across runs would freeze it to run 0's output and understate its variance.

    Each without-arm call returns a DIFFERENT output here; if the cache were shared the runner would
    invoke the adapter's without-arm exactly once (run 0) and replay it. With the per-run key it
    runs 3 times — and the cache holds 3 distinct entries.
    """
    suite = """
cases:
  - name: drift
    prompt: "classify"
    expectations:
      - "labels email as PII"
"""
    n_without = {"n": 0}

    def responses(prompt: str, with_skill: bool) -> str:
        if with_skill:
            return "I labels email as PII here."
        n_without["n"] += 1
        return f"baseline rollout #{n_without['n']} — distinct enough to differ"

    cache = BaselineCache("mock", base_dir=tmp_path)
    adapter = MockAdapter(responses)
    result = run_suite(
        parse_eval_yaml(suite),
        adapter,
        skill_name="demo-skill",
        skill_body="# Demo",
        judge_call=mock_judge_call,
        cache=cache,
        runs=3,
    )
    # All 3 runs ran the baseline live (not 1 + 2 cache replays).
    without_calls = [c for c in adapter.calls if c["with_skill"] is False]
    assert len(without_calls) == 3, without_calls
    assert n_without["n"] == 3
    # The aggregate saw all 3 runs (rates are means over them); one representative report row.
    assert result.aggregate.total_cases == 3
    assert result.runs == 3
    assert len(result.cases) == 1
    # And the 3 trials are cached under DISTINCT keys (no single shared baseline entry).
    cache_files = list(tmp_path.glob("**/*.json"))
    assert len(cache_files) == 3, cache_files


def test_validators_graded_in_workspace():
    suite = """
cases:
  - name: artifact
    prompt: "emit sql"
    setup:
      - "echo ready > seed.txt"
    validators:
      - cmd: "grep -q SELECT response.txt"
        label: "sql present"
      - cmd: "test -f seed.txt"
        label: "setup ran"
"""
    responses = {"emit sql": {"with": "SELECT * FROM t;", "without": "no query here, sorry friend"}}
    result = _run(suite=suite, responses=responses)
    case = result.cases[0]
    assert case.outcome == "flip_to_pass"
    assert [v["passed"] for v in case.validator_results] == [True, True]


def test_broken_grader_exit2_errors_case_excluded_from_lift():
    """A script validator that exits with a non-binary, undeclared code (exit 2 =
    bad spec / missing case / unknown mode) is an AUTHOR error, not a model fail.
    The case must classify as ``error`` and be EXCLUDED from the lift aggregate —
    a broken grader can't masquerade as honest no-lift (a fail_kept)."""
    suite = """
cases:
  - name: broken_grader
    prompt: "emit sql"
    validators:
      - cmd: "exit 2"
        label: "broken spec"
"""
    responses = {"emit sql": {"with": "SELECT * FROM t;", "without": "no query here friend"}}
    result = _run(suite=suite, responses=responses)
    case = result.cases[0]
    # The exit-2 grader errors the case — NOT fail_kept (which would be honest no-lift).
    assert case.outcome == "error"
    assert case.validator_results[0]["errored"] is True
    # Excluded from the lift aggregate (no aggregated case, counted as an error).
    assert result.aggregate.cases_aggregated == 0
    assert result.aggregate.errors == 1


def test_grader_that_never_ran_errors_case_instead_of_manufacturing_a_flip():
    """0.7.0: a grader that returns NO verdict is UNGRADED — the case leaves the lift aggregate.

    Covers the spawn-failure branch (deterministic and instant; the sibling timeout branch is
    unit-tested in test_runner_validators with an explicit ``timeout_s``, since ``run_suite``
    exposes no timeout knob). Both are the same channel: before this change each recorded a
    model FAIL, and because graders run per arm, a grader that broke on only one arm's output
    fabricated a flip in whichever direction it landed.
    """
    suite = """
cases:
  - name: ungraded_grader
    prompt: "emit sql"
    validators:
      - cmd: "echo \\x00bad"
        label: "grader that cannot be spawned"
"""
    responses = {"emit sql": {"with": "SELECT * FROM t;", "without": "no query here friend"}}
    result = _run(suite=suite, responses=responses)
    case = result.cases[0]
    assert case.outcome == "error"
    assert case.validator_results[0]["errored"] is True
    assert result.aggregate.cases_aggregated == 0
    assert result.aggregate.errors == 1
    # And no lift is claimed from a case nobody graded.
    assert result.aggregate.pass_rate["delta_pts"] is None


def test_trigger_only_case_skipped_with_disclosure():
    """Spec 0.3.0 disclosure, rev-2 posture: a trigger-only case (should_trigger, no grader)
    never enters the A/B loop — it is excluded with a disclosed count, total_cases counts only
    graded cases, and the runner NEVER executes trigger cases (routing is platform-side)."""
    suite = """
cases:
  - name: fires_on_topic
    prompt: "how do I format the error envelope?"
    should_trigger: true
  - name: graded
    prompt: "classify the fields"
    expectations:
      - "labels email as PII"
"""
    events = []
    result = _run(suite=suite,
                  on_progress=lambda ev, case, detail: events.append((ev, case)))
    assert result.cases_skipped_trigger_only == 1
    assert [c.case_name for c in result.cases] == ["graded"]
    assert result.aggregate.total_cases == 1
    assert ("case_skipped_trigger_only", "fires_on_topic") in events
    assert ("case_start", "fires_on_topic") not in events
    # Disclosure rides results.json too — and never a trigger block (rev 2).
    doc = _run(suite=suite).to_results_json()
    assert doc["cases_skipped_trigger_only"] == 1
    assert "trigger_metrics" not in doc
    assert "trigger_cases" not in doc


def test_should_trigger_with_grader_still_runs_ab():
    """A composed case (should_trigger + expectations) is NOT trigger-only — it runs the loop."""
    suite = """
cases:
  - name: rescued
    prompt: "classify the fields"
    should_trigger: true
    expectations:
      - "labels email as PII"
"""
    result = _run(suite=suite)
    assert result.cases_skipped_trigger_only == 0
    assert result.cases[0].outcome == "flip_to_pass"


def test_error_dominated_run_verdicts_error():
    """>25% errored invalidates the run: verdict `error` (not `pass`/`mixed` off the surviving
    minority) and the headline delta withheld — the floor from spec/runner-contract.md."""
    suite = """
cases:
  - name: errors_out
    prompt: "p"
    setup:
      - "false"
    expectations:
      - "anything"
  - name: rescued
    prompt: "classify the fields"
    expectations:
      - "labels email as PII"
"""
    result = _run(suite=suite)
    assert result.aggregate.error_dominated is True  # 1/2 errored = 50% > 25%
    assert result.verdict == "error"
    assert result.aggregate.pass_rate["delta_pts"] is None
    # The surviving case alone would have verdicted pass — the override is what's under test.
    assert result.cases[1].outcome == "flip_to_pass"


def test_apples_to_oranges_punt_excluded_from_aggregate():
    suite = """
cases:
  - name: punt
    prompt: "structured task"
    expectations:
      - "anything"
"""
    responses = {
        "structured task": {"with": "anything — a full long structured answer", "without": "idk"}
    }
    result = _run(suite=suite, responses=responses)
    case = result.cases[0]
    assert case.without_arm.task_attempted is False  # "idk" < 20 chars
    agg = result.aggregate
    assert agg.cases_skipped_apples_oranges == 1
    assert agg.cases_aggregated == 0
    # Outcome still persisted.
    assert case.outcome == "flip_to_pass"


def test_progress_callback_sequence():
    events = []
    _run(on_progress=lambda ev, case, detail: events.append((ev, case)))
    assert ("case_start", "rescued") in events
    assert ("case_done", "still_failing") in events


# ── results.json ─────────────────────────────────────────────────────


def test_results_json_validates_against_packaged_schemas():
    jsonschema = pytest.importorskip("jsonschema")
    from skillevaluation.resources import load_schema

    referencing = pytest.importorskip("referencing")

    doc = _run().to_results_json()
    jsonschema.validate(doc, load_schema("test-run-result"))

    # test-case-result $refs judge-result by its canonical URL; resolve it
    # against the packaged copy so the test runs offline.
    case_schema = load_schema("test-case-result")
    judge_schema = load_schema("judge-result")
    registry = referencing.Registry().with_resources(
        [
            (case_schema["$id"], referencing.Resource.from_contents(case_schema)),
            (judge_schema["$id"], referencing.Resource.from_contents(judge_schema)),
        ]
    )
    validator = jsonschema.Draft202012Validator(case_schema, registry=registry)
    for case_doc in doc["cases"]:
        validator.validate(case_doc)


def test_results_json_is_json_serializable_and_complete():
    doc = _run(cache=None).to_results_json()
    json.dumps(doc)  # no exotic types
    assert doc["verdict"] == "mixed"
    assert doc["total_cases"] == 2
    assert doc["skill"]["name"] == "demo-skill"
    assert doc["runner"]["adapter"] == "mock"
    assert doc["format"] == "skillevaluation/test-run-result@v1"
    assert {c["case_name"] for c in doc["cases"]} == {"rescued", "still_failing"}
    arm = doc["cases"][0]["with_skill"]
    for key in ("passed", "task_attempted", "errored"):
        assert key in arm


# ── per-arm workspace isolation + cleanup ────────────────────────────


class _FileWritingAdapter(MockAdapter):
    """Simulates a file-writing agent (claude-code style): the with-skill
    arm drops an artifact into its workspace."""

    def run(self, *, prompt, skill_body, workspace):
        arm = super().run(prompt=prompt, skill_body=skill_body, workspace=workspace)
        if skill_body is not None:
            (workspace / "solved.txt").write_text("artifact", encoding="utf-8")
        return arm


def test_arms_get_isolated_workspaces():
    """A with-arm artifact must NOT be visible to the without arm — shared
    workspaces would let file validators 'pass' the baseline on the skill
    arm's work (biasing flip_to_pass down to pass_kept)."""
    suite = """
cases:
  - name: artifact_isolation
    prompt: "produce solved.txt"
    validators:
      - cmd: "test -f solved.txt"
        label: "artifact present"
"""
    responses = {
        "produce solved.txt": {
            "with": "I created solved.txt with the artifact contents.",
            "without": "I describe the artifact but cannot create files myself.",
        }
    }
    adapter = _FileWritingAdapter(responses)
    result = run_suite(
        parse_eval_yaml(suite),
        adapter,
        skill_name="s",
        skill_body="write solved.txt",
        judge_call=mock_judge_call,
        keep_workspaces=True,  # so the assertion below can inspect them
    )
    case = result.cases[0]
    assert case.outcome == "flip_to_pass", (
        case.outcome, case.validator_results,
    )
    # And the adapter genuinely saw two distinct directories.
    ws_paths = {c["workspace"] for c in adapter.calls}
    assert len(ws_paths) == 2
    import shutil
    for p in ws_paths:
        shutil.rmtree(p, ignore_errors=True)


def test_workspaces_cleaned_up_by_default(tmp_path):
    from pathlib import Path

    adapter = MockAdapter(RESPONSES)
    run_suite(
        parse_eval_yaml(SUITE),
        adapter,
        skill_name="s",
        skill_body="b",
        judge_call=mock_judge_call,
    )
    assert adapter.calls, "adapter ran"
    for call in adapter.calls:
        assert not Path(call["workspace"]).exists(), call["workspace"]


def test_keep_workspaces_leaves_dirs(tmp_path):
    import shutil
    from pathlib import Path

    adapter = MockAdapter(RESPONSES)
    run_suite(
        parse_eval_yaml(SUITE),
        adapter,
        skill_name="s",
        skill_body="b",
        judge_call=mock_judge_call,
        keep_workspaces=True,
    )
    kept = [Path(c["workspace"]) for c in adapter.calls]
    assert kept and all(p.exists() for p in kept)
    for p in kept:
        shutil.rmtree(p, ignore_errors=True)
