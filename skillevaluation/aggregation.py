"""Run-level aggregation of per-case A/B benchmark results.

Given per-case metrics from both arms (with-skill and without-skill),
compute the aggregate deltas that become the headline value claim
("pass rate +34 pts, turns −46%, tokens −43%").

See ``spec/runner-contract.md`` for the canonical specification —
including the **apples-to-oranges skip rule**: cases where one arm
didn't even attempt the task are persisted as outcomes but excluded
from the aggregate lift calculation, so a 73-token "I don't know"
response on the without-skill arm doesn't inflate token deltas to
nonsense values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from skillevaluation.outcomes import Outcome

# Error-dominated floor (spec/runner-contract.md — normative, so every conforming runner uses
# this same threshold): when MORE than this fraction of a run's cases errored (e.g. a
# provider 503 storm), the run is not a valid measurement — the aggregate discloses
# ``error_dominated: true`` and nulls the headline ``pass_rate.delta_pts`` (never a lift claim
# carried by the surviving minority). The per-arm rates + filter counts stay for disclosure.
# Exactly 25% does NOT trip the floor — the comparison is strictly greater.
ERROR_DOMINATED_FLOOR = 0.25


def _round(x: float, places: int) -> float:
    """Round half **away from zero** on the decimal value (spec/runner-contract.md §Rounding).

    Python's built-in ``round`` is banker's rounding (round-half-even) over the IEEE-754 binary
    value, so a ``.5`` tie like ``round(2.45, 1)`` lands on ``2.4`` and diverges from a different
    language's ``printf``. Quantizing ``Decimal(str(x))`` makes the tie deterministic and
    cross-language stable (the canonical requirement the conformance goldens pin).
    """
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


@dataclass
class CaseMetrics:
    """Per-arm metrics for one test case.

    All metric fields are optional. A conforming runner SHOULD provide
    them; missing values are treated as zero in aggregation.

    ``task_attempted`` distinguishes "the agent tried and failed" from
    "the agent never engaged with the task" — only attempted cases
    contribute to aggregate lift.

    ``errored`` flags transient infrastructure failure (LLM API down,
    sandbox crash) so the outcome can be classified as ``error`` and
    the case excluded from aggregate lift.
    """

    passed: bool = False
    duration_ms: int = 0
    turns: int = 0
    total_tokens: int = 0
    tool_call_count: int = 0
    task_attempted: bool = True
    errored: bool = False


@dataclass
class CaseResult:
    """One case's A/B result."""

    case_name: str
    outcome: str
    with_skill: CaseMetrics | None = None
    without_skill: CaseMetrics | None = None


@dataclass
class DeltaResult:
    """Per-dimension delta in the run aggregate."""

    with_skill_avg: float
    without_skill_avg: float
    delta_pct: float | None  # None when without_skill_avg == 0


@dataclass
class RunAggregate:
    """Aggregated run-level metrics."""

    pass_rate: dict[str, float | None] = field(default_factory=dict)
    duration_ms: DeltaResult | None = None
    turns: DeltaResult | None = None
    tokens: DeltaResult | None = None
    tool_calls: DeltaResult | None = None

    # Correctness-gated efficiency deltas — computed over cases where BOTH arms passed (the only
    # honest basis for a turn/token saving). The registry efficiency leaderboards rank
    # on these, NOT the raw un-gated fields above (which a "fast but wrong" arm can inflate).
    duration_ms_correctness_gated: DeltaResult | None = None
    turns_correctness_gated: DeltaResult | None = None
    tokens_correctness_gated: DeltaResult | None = None
    tool_calls_correctness_gated: DeltaResult | None = None

    # Honest disclosure of what got filtered.
    errors: int = 0
    # True when errored / total > ERROR_DOMINATED_FLOOR — the run is too error-contaminated for
    # an honest headline: pass_rate.delta_pts is nulled and verdict logic must treat it failing.
    error_dominated: bool = False
    cases_aggregated: int = 0
    # Cases where BOTH arms passed — the divisor for the (correctness-gated) efficiency deltas.
    cases_correctness_gated: int = 0
    cases_skipped_apples_oranges: int = 0
    total_cases: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form matching the documented wire schema."""
        return {
            "pass_rate": self.pass_rate,
            "duration_ms": _delta_to_dict(self.duration_ms),
            "turns": _delta_to_dict(self.turns),
            "tokens": _delta_to_dict(self.tokens),
            "tool_calls": _delta_to_dict(self.tool_calls),
            "duration_ms_correctness_gated": _delta_to_dict(self.duration_ms_correctness_gated),
            "turns_correctness_gated": _delta_to_dict(self.turns_correctness_gated),
            "tokens_correctness_gated": _delta_to_dict(self.tokens_correctness_gated),
            "tool_calls_correctness_gated": _delta_to_dict(self.tool_calls_correctness_gated),
            "errors": self.errors,
            "error_dominated": self.error_dominated,
            "cases_aggregated": self.cases_aggregated,
            "cases_correctness_gated": self.cases_correctness_gated,
            "cases_skipped_apples_oranges": self.cases_skipped_apples_oranges,
            "total_cases": self.total_cases,
        }


def delta_pct(with_val: float, without_val: float) -> DeltaResult:
    """Signed percentage delta.

    Negative = with-skill is LOWER (good for turns/tokens/duration).
    Positive = with-skill is HIGHER.

    When the without-skill average is zero, ``delta_pct`` is None (the
    delta is undefined; consumers must surface this honestly rather
    than fabricating a 0% or 100%).
    """
    if without_val == 0:
        return DeltaResult(
            with_skill_avg=_round(with_val, 2),
            without_skill_avg=_round(without_val, 2),
            delta_pct=None,
        )
    pct = ((with_val - without_val) / without_val) * 100
    return DeltaResult(
        with_skill_avg=_round(with_val, 2),
        without_skill_avg=_round(without_val, 2),
        delta_pct=_round(pct, 1),
    )


def compute_run_aggregates(case_results: list[CaseResult]) -> RunAggregate:
    """Aggregate per-case results into a run-level summary.

    Implements the **apples-to-oranges skip rule** from the spec:
    cases where one arm errored, or where either arm did not attempt
    the task, are persisted as outcomes but excluded from the
    aggregate lift calculation.

    Args:
        case_results: list of per-case results from the runner.

    Returns:
        A RunAggregate with per-dimension deltas + filter disclosure.
    """
    total_cases = len(case_results)
    error_count = 0
    aggregated_n = 0

    totals = {
        "with_pass": 0, "without_pass": 0,
        "with_duration": 0, "without_duration": 0,
        "with_turns": 0, "without_turns": 0,
        "with_tokens": 0, "without_tokens": 0,
        "with_tool_calls": 0, "without_tool_calls": 0,
    }
    # Correctness-gated efficiency: accumulated ONLY over cases where BOTH arms passed
    # (Cost-of-Pass). Comparing turns/tokens when the arms disagree on correctness lets a
    # "fast but wrong" arm fake a saving. The raw `turns`/`tokens` fields
    # (un-gated, over all attempted cases) stay for backward compatibility; the *_correctness_gated
    # fields are what the registry efficiency leaderboards should rank on.
    eff = {
        "with_duration": 0, "without_duration": 0,
        "with_turns": 0, "without_turns": 0,
        "with_tokens": 0, "without_tokens": 0,
        "with_tool_calls": 0, "without_tool_calls": 0,
    }
    both_correct_n = 0

    for cr in case_results:
        if cr.outcome == Outcome.ERROR:
            error_count += 1
            continue

        wm = cr.with_skill or CaseMetrics()
        om = cr.without_skill or CaseMetrics()

        # Apples-to-oranges skip: if either arm didn't attempt, the
        # comparison is meaningless. Persist the outcome but skip from
        # the aggregate divisor.
        if not wm.task_attempted or not om.task_attempted:
            continue

        aggregated_n += 1
        if wm.passed:
            totals["with_pass"] += 1
        if om.passed:
            totals["without_pass"] += 1
        totals["with_duration"] += int(wm.duration_ms or 0)
        totals["without_duration"] += int(om.duration_ms or 0)
        totals["with_turns"] += int(wm.turns or 0)
        totals["without_turns"] += int(om.turns or 0)
        totals["with_tokens"] += int(wm.total_tokens or 0)
        totals["without_tokens"] += int(om.total_tokens or 0)
        totals["with_tool_calls"] += int(wm.tool_call_count or 0)
        totals["without_tool_calls"] += int(om.tool_call_count or 0)

        # Correctness gate: only when BOTH arms reached a correct answer is a turns/tokens
        # difference a real efficiency signal rather than a wrong-answer artifact.
        if wm.passed and om.passed:
            both_correct_n += 1
            eff["with_duration"] += int(wm.duration_ms or 0)
            eff["without_duration"] += int(om.duration_ms or 0)
            eff["with_turns"] += int(wm.turns or 0)
            eff["without_turns"] += int(om.turns or 0)
            eff["with_tokens"] += int(wm.total_tokens or 0)
            eff["without_tokens"] += int(om.total_tokens or 0)
            eff["with_tool_calls"] += int(wm.tool_call_count or 0)
            eff["without_tool_calls"] += int(om.tool_call_count or 0)

    n = max(aggregated_n, 1)
    skipped = total_cases - aggregated_n - error_count
    # Mutual-exclusivity invariant: every case is exactly one of errored / aggregated / skipped.
    # `skipped` is derived by subtraction, so assert the partition holds — a future miscount then
    # fails loudly instead of silently corrupting a spec-mandated honesty-disclosure count.
    assert error_count + aggregated_n + skipped == total_cases, (
        f"case partition broken: {error_count}+{aggregated_n}+{skipped} != {total_cases}"
    )

    # Pass rate. When NOTHING aggregated (every case errored or was apples-to-oranges skipped) the
    # run measured no comparable case — report N/A (null), never a fabricated 0% / +0 pts that would
    # contradict the null token deltas on the very same run (spec/runner-contract.md). With
    # runner-level runs > 1, each (case, run) record enters this mean — rates are averages over
    # all runs (rev 2), so the expected value does not depend on the run count.
    if aggregated_n == 0:
        pass_rate: dict[str, float | None] = {
            "with_skill": None,
            "without_skill": None,
            "delta_pts": None,
        }
    else:
        with_pr = totals["with_pass"] / aggregated_n
        without_pr = totals["without_pass"] / aggregated_n
        pass_rate = {
            "with_skill": _round(with_pr, 4),
            "without_skill": _round(without_pr, 4),
            "delta_pts": _round((with_pr - without_pr) * 100, 2),
        }

    # Error-dominated invalidation (ERROR_DOMINATED_FLOOR): null the headline delta — the
    # surviving minority can't honestly carry a lift claim — but keep the per-arm rates and every
    # disclosure count so a consumer sees exactly what happened (and why the claim is withheld).
    error_dominated = total_cases > 0 and (error_count / total_cases) > ERROR_DOMINATED_FLOOR
    if error_dominated:
        pass_rate["delta_pts"] = None

    # Raw (un-gated) deltas over all attempted cases — kept for backward compatibility.
    duration_d = delta_pct(totals["with_duration"] / n, totals["without_duration"] / n)
    turns_d = delta_pct(totals["with_turns"] / n, totals["without_turns"] / n)
    tokens_d = delta_pct(totals["with_tokens"] / n, totals["without_tokens"] / n)
    tool_calls_d = delta_pct(totals["with_tool_calls"] / n, totals["without_tool_calls"] / n)

    # Correctness-gated deltas over both-correct cases only (what the leaderboards rank on). With no
    # both-correct case the totals are 0 → delta_pct(0, 0) gives the honest "undefined" (delta
    # None).
    cn = max(both_correct_n, 1)
    duration_g = delta_pct(eff["with_duration"] / cn, eff["without_duration"] / cn)
    turns_g = delta_pct(eff["with_turns"] / cn, eff["without_turns"] / cn)
    tokens_g = delta_pct(eff["with_tokens"] / cn, eff["without_tokens"] / cn)
    tool_calls_g = delta_pct(eff["with_tool_calls"] / cn, eff["without_tool_calls"] / cn)

    return RunAggregate(
        pass_rate=pass_rate,
        duration_ms=duration_d,
        turns=turns_d,
        tokens=tokens_d,
        tool_calls=tool_calls_d,
        duration_ms_correctness_gated=duration_g,
        turns_correctness_gated=turns_g,
        tokens_correctness_gated=tokens_g,
        tool_calls_correctness_gated=tool_calls_g,
        errors=error_count,
        error_dominated=error_dominated,
        cases_aggregated=aggregated_n,
        cases_correctness_gated=both_correct_n,
        cases_skipped_apples_oranges=skipped,
        total_cases=total_cases,
    )


def _delta_to_dict(d: DeltaResult | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "with_skill_avg": d.with_skill_avg,
        "without_skill_avg": d.without_skill_avg,
        "delta_pct": d.delta_pct,
    }
