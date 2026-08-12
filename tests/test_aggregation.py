"""Tests for skillevaluation.aggregation."""

from __future__ import annotations

from skillevaluation.aggregation import (
    CaseMetrics,
    CaseResult,
    compute_run_aggregates,
    delta_pct,
)
from skillevaluation.outcomes import Outcome


def test_delta_pct_negative_when_with_is_lower():
    """Lower is better for turns/tokens/duration — delta should be negative."""
    d = delta_pct(50, 100)
    assert d.delta_pct == -50.0
    assert d.with_skill_avg == 50
    assert d.without_skill_avg == 100


def test_delta_pct_positive_when_with_is_higher():
    d = delta_pct(150, 100)
    assert d.delta_pct == 50.0


def test_delta_pct_none_when_baseline_is_zero():
    """Don't fabricate a delta when the baseline is zero — surface honestly."""
    d = delta_pct(10, 0)
    assert d.delta_pct is None


def test_aggregate_skips_errors():
    """Errored cases are persisted but excluded from aggregate lift."""
    results = [
        CaseResult(case_name="a", outcome=Outcome.ERROR),
        CaseResult(
            case_name="b",
            outcome=Outcome.FLIP_TO_PASS,
            with_skill=CaseMetrics(passed=True, total_tokens=100),
            without_skill=CaseMetrics(passed=False, total_tokens=200),
        ),
    ]
    agg = compute_run_aggregates(results)
    assert agg.errors == 1
    assert agg.cases_aggregated == 1
    assert agg.total_cases == 2


def test_aggregate_skips_apples_to_oranges():
    """When one arm didn't attempt the task, skip from aggregate divisor."""
    results = [
        CaseResult(
            case_name="not_attempted",
            outcome=Outcome.FAIL_KEPT,
            with_skill=CaseMetrics(passed=False, total_tokens=1000, task_attempted=True),
            without_skill=CaseMetrics(passed=False, total_tokens=20, task_attempted=False),
        ),
        CaseResult(
            case_name="attempted",
            outcome=Outcome.FLIP_TO_PASS,
            with_skill=CaseMetrics(passed=True, total_tokens=500),
            without_skill=CaseMetrics(passed=False, total_tokens=400),
        ),
    ]
    agg = compute_run_aggregates(results)
    assert agg.cases_aggregated == 1  # only the second case
    assert agg.cases_skipped_apples_oranges == 1
    # Raw (un-gated) token delta still uses the attempted case (500 vs 400 = +25%).
    assert agg.tokens.delta_pct == 25.0
    # But the gated efficiency excludes it: the arms DISAGREE on correctness (with passed, without
    # failed), so there is no both-correct case → gated delta is undefined (None).
    assert agg.cases_correctness_gated == 0
    assert agg.tokens_correctness_gated.delta_pct is None


def test_efficiency_delta_is_correctness_gated():
    """The correctness-gated efficiency deltas count ONLY cases where BOTH arms passed — a
    fast-but-wrong arm must never score a turn/token saving."""
    results = [
        # both correct: with uses fewer turns/tokens → a real saving, counted in the gated delta.
        CaseResult(
            case_name="both_pass",
            outcome=Outcome.PASS_KEPT,
            with_skill=CaseMetrics(passed=True, turns=2, total_tokens=100),
            without_skill=CaseMetrics(passed=True, turns=5, total_tokens=200),
        ),
        # fast-but-wrong base: the base "saves" turns by being WRONG → excluded from gated delta.
        CaseResult(
            case_name="base_wrong_fast",
            outcome=Outcome.FLIP_TO_PASS,
            with_skill=CaseMetrics(passed=True, turns=8, total_tokens=900),
            without_skill=CaseMetrics(passed=False, turns=1, total_tokens=50),
        ),
    ]
    agg = compute_run_aggregates(results)
    assert agg.cases_aggregated == 2  # both attempted
    assert agg.cases_correctness_gated == 1  # only the both-pass case feeds the gated delta
    # GATED turns delta from the both-pass case ONLY (2 vs 5 = -60%) — the honest saving.
    assert agg.turns_correctness_gated.delta_pct == -60.0
    assert agg.tokens_correctness_gated.delta_pct == -50.0  # 100 vs 200
    # The RAW un-gated turns delta is polluted by the fast-but-wrong case (with avg 5, base avg 3 =
    # +66.7%) — exactly the artifact the gate exists to keep off the leaderboard.
    assert agg.turns.delta_pct == 66.7


def test_aggregate_pass_rate_math():
    """Pass-rate delta is in percentage points, not percent."""
    results = [
        CaseResult(
            case_name="a",
            outcome=Outcome.FLIP_TO_PASS,
            with_skill=CaseMetrics(passed=True),
            without_skill=CaseMetrics(passed=False),
        ),
        CaseResult(
            case_name="b",
            outcome=Outcome.PASS_KEPT,
            with_skill=CaseMetrics(passed=True),
            without_skill=CaseMetrics(passed=True),
        ),
    ]
    agg = compute_run_aggregates(results)
    # with-skill: 2/2 = 1.0; without-skill: 1/2 = 0.5; delta_pts = 50.0
    assert agg.pass_rate["with_skill"] == 1.0
    assert agg.pass_rate["without_skill"] == 0.5
    assert agg.pass_rate["delta_pts"] == 50.0


def test_aggregate_to_dict_is_json_serializable():
    """The wire-format dict has the documented shape."""
    results = [
        CaseResult(
            case_name="a",
            outcome=Outcome.FLIP_TO_PASS,
            with_skill=CaseMetrics(passed=True, duration_ms=1000),
            without_skill=CaseMetrics(passed=False, duration_ms=2000),
        ),
    ]
    d = compute_run_aggregates(results).to_dict()
    assert "pass_rate" in d
    assert "duration_ms" in d
    assert "errors" in d
    assert "cases_aggregated" in d
    assert "turns_correctness_gated" in d
    assert "pass_at_k" not in d  # retired in schema rev 2 — repetition is MEAN-averaged


def test_repeat_runs_average_by_mean():
    """Schema rev 2: uniform re-runs of a case (same case_name) enter the aggregate as ordinary
    records, so rates are MEANS over runs — a flaky with-arm that passes 2/3 runs scores 0.6667,
    the unbiased estimate of its pass probability. The expected value does not depend on the run
    count (pass^k's AND-fold, which made the number k-dependent, is retired)."""
    runs = [
        CaseResult(case_name="c", outcome=Outcome.PASS_KEPT,
                   with_skill=CaseMetrics(passed=p), without_skill=CaseMetrics(passed=False))
        for p in (True, True, False)  # with arm flaky: 2/3
    ]
    agg = compute_run_aggregates(runs)
    assert agg.pass_rate["with_skill"] == round(2 / 3, 4)
    assert agg.pass_rate["without_skill"] == 0.0
    assert agg.pass_rate["delta_pts"] == round(200 / 3, 2)


def test_repeat_runs_unanimous_pass_is_full_rate():
    runs = [
        CaseResult(case_name="c", outcome=Outcome.PASS_KEPT,
                   with_skill=CaseMetrics(passed=True), without_skill=CaseMetrics(passed=False))
        for _ in range(4)
    ]
    agg = compute_run_aggregates(runs)
    assert agg.pass_rate["with_skill"] == 1.0
    assert agg.pass_rate["delta_pts"] == 100.0


def _flip_to_pass(name: str) -> CaseResult:
    return CaseResult(
        case_name=name,
        outcome=Outcome.FLIP_TO_PASS,
        with_skill=CaseMetrics(passed=True, total_tokens=100),
        without_skill=CaseMetrics(passed=False, total_tokens=200),
    )


def test_error_floor_trips_above_25_percent():
    """>25% errored (2/7 ≈ 28.6%) → error_dominated, and the headline delta is WITHHELD (null),
    never a lift claim carried by the surviving minority. Disclosure counts stay populated."""
    results = [CaseResult(case_name=f"err{i}", outcome=Outcome.ERROR) for i in range(2)]
    results += [_flip_to_pass(f"ok{i}") for i in range(5)]
    agg = compute_run_aggregates(results)
    assert agg.error_dominated is True
    assert agg.pass_rate["delta_pts"] is None
    # Per-arm rates + disclosure counts survive — only the claim is withdrawn.
    assert agg.pass_rate["with_skill"] == 1.0
    assert agg.pass_rate["without_skill"] == 0.0
    assert agg.errors == 2
    assert agg.cases_aggregated == 5
    assert agg.total_cases == 7
    assert agg.to_dict()["error_dominated"] is True


def test_error_floor_not_tripped_at_exactly_25_percent():
    """Exactly 25% errored (1/4) does NOT trip the floor — the comparison is strictly greater,
    as spec/runner-contract.md requires of every conforming runner."""
    results = [CaseResult(case_name="err", outcome=Outcome.ERROR)]
    results += [_flip_to_pass(f"ok{i}") for i in range(3)]
    agg = compute_run_aggregates(results)
    assert agg.error_dominated is False
    assert agg.pass_rate["delta_pts"] == 100.0


def test_error_floor_false_on_empty_and_error_free_runs():
    """Zero cases (the degenerate-run rule already covers it) and error-free runs are not
    error-dominated."""
    assert compute_run_aggregates([]).error_dominated is False
    assert compute_run_aggregates([_flip_to_pass("ok")]).error_dominated is False
