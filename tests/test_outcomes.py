"""Tests for skillevaluation.outcomes — lifted from DecimalAI's benchmark suite."""

from __future__ import annotations

from skillevaluation.outcomes import Outcome, Verdict, classify_outcome, compute_verdict


def test_classify_outcome_flip_to_pass():
    assert classify_outcome(with_passed=True, without_passed=False) == Outcome.FLIP_TO_PASS


def test_classify_outcome_pass_kept():
    assert classify_outcome(with_passed=True, without_passed=True) == Outcome.PASS_KEPT


def test_classify_outcome_fail_kept():
    assert classify_outcome(with_passed=False, without_passed=False) == Outcome.FAIL_KEPT


def test_classify_outcome_flip_to_fail():
    """The regression marker — the skill HURT."""
    assert classify_outcome(with_passed=False, without_passed=True) == Outcome.FLIP_TO_FAIL


def test_classify_outcome_error_overrides_everything():
    """When either arm couldn't be evaluated, errored=True wins."""
    assert classify_outcome(with_passed=True, without_passed=True, errored=True) == Outcome.ERROR
    assert classify_outcome(with_passed=False, without_passed=False, errored=True) == Outcome.ERROR


def test_outcome_constants_are_stable_strings():
    """Wire-format stability check — these strings ship in user-facing UIs."""
    assert Outcome.FLIP_TO_PASS == "flip_to_pass"
    assert Outcome.PASS_KEPT == "pass_kept"
    assert Outcome.FAIL_KEPT == "fail_kept"
    assert Outcome.FLIP_TO_FAIL == "flip_to_fail"
    assert Outcome.ERROR == "error"


def test_outcome_all_contains_all_five():
    assert set(Outcome.ALL) == {
        "flip_to_pass",
        "pass_kept",
        "fail_kept",
        "flip_to_fail",
        "error",
    }


# ── Verdict ────────────────────────────────────────────────────────────


def test_verdict_pass_when_all_non_errored_passed():
    assert compute_verdict([Outcome.FLIP_TO_PASS, Outcome.PASS_KEPT]) == Verdict.PASS


def test_verdict_fail_when_all_non_errored_failed():
    assert compute_verdict([Outcome.FLIP_TO_FAIL, Outcome.FAIL_KEPT]) == Verdict.FAIL


def test_verdict_mixed_when_passing_and_failing():
    assert compute_verdict([Outcome.FLIP_TO_PASS, Outcome.FLIP_TO_FAIL]) == Verdict.MIXED


def test_verdict_error_when_every_case_errored():
    assert compute_verdict([Outcome.ERROR, Outcome.ERROR]) == Verdict.ERROR


def test_verdict_error_when_empty():
    """Degenerate run (no cases) is `error`, matching 'every case errored'."""
    assert compute_verdict([]) == Verdict.ERROR


def test_verdict_excludes_errored_cases_from_pass_decision():
    """The key spec rule: a run where every case that RAN passed is a pass,
    even if other cases errored. A naive 'all cases must be pass-outcomes'
    check would wrongly return 'mixed' here."""
    assert compute_verdict([Outcome.PASS_KEPT, Outcome.FLIP_TO_PASS, Outcome.ERROR]) == Verdict.PASS
    assert compute_verdict([Outcome.FAIL_KEPT, Outcome.ERROR]) == Verdict.FAIL


def test_verdict_constants_are_stable_strings():
    assert Verdict.PASS == "pass"
    assert Verdict.FAIL == "fail"
    assert Verdict.MIXED == "mixed"
    assert Verdict.ERROR == "error"
    assert set(Verdict.ALL) == {"pass", "fail", "mixed", "error"}
