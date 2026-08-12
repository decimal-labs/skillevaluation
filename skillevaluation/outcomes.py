"""Outcome classification for A/B benchmark cases.

A conforming runner executes each test case TWICE — with the skill loaded
and without — then classifies the per-case outcome based on the pass
result of each arm. The mapping is total and deterministic.

See ``spec/runner-contract.md`` for the canonical specification.

The five outcomes:

  - ``flip_to_pass``  — without-skill failed, with-skill passed.
                        The skill rescued the agent. The "win" outcome.
  - ``pass_kept``     — both arms passed. The skill didn't hurt; may or
                        may not have helped (case wasn't discriminating).
  - ``fail_kept``     — both arms failed. The skill didn't help on this
                        case (the task may be too hard, or unrelated).
  - ``flip_to_fail``  — without-skill passed, with-skill failed.
                        REGRESSION marker — the skill made things worse.
  - ``error``         — at least one arm couldn't be evaluated (transient
                        API failure, infrastructure issue). Excluded from
                        aggregate lift calculations so transient failures
                        don't contaminate the headline number.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final


class Outcome:
    """String constants for the five outcomes."""

    FLIP_TO_PASS: Final[str] = "flip_to_pass"
    PASS_KEPT: Final[str] = "pass_kept"
    FAIL_KEPT: Final[str] = "fail_kept"
    FLIP_TO_FAIL: Final[str] = "flip_to_fail"
    ERROR: Final[str] = "error"

    ALL: Final[tuple[str, ...]] = (
        FLIP_TO_PASS,
        PASS_KEPT,
        FAIL_KEPT,
        FLIP_TO_FAIL,
        ERROR,
    )


def classify_outcome(
    *,
    with_passed: bool,
    without_passed: bool,
    errored: bool = False,
) -> str:
    """Classify a per-case outcome.

    Args:
        with_passed: did the with-skill arm pass?
        without_passed: did the without-skill arm pass?
        errored: did either arm fail to execute (transient API failure,
            sandbox crash)? If True, returns "error" regardless of the
            pass flags.

    Returns:
        One of the five strings in ``Outcome.ALL``.
    """
    if errored:
        return Outcome.ERROR
    if with_passed and not without_passed:
        return Outcome.FLIP_TO_PASS
    if with_passed and without_passed:
        return Outcome.PASS_KEPT
    if not with_passed and without_passed:
        return Outcome.FLIP_TO_FAIL
    return Outcome.FAIL_KEPT


class Verdict:
    """String constants for the four run-level verdicts.

    Unlike the five outcome strings, these are derived (never stored as
    ground truth), but they ship in user-facing UIs and external APIs, so
    they are treated as frozen alongside the outcomes.
    """

    PASS: Final[str] = "pass"
    FAIL: Final[str] = "fail"
    MIXED: Final[str] = "mixed"
    ERROR: Final[str] = "error"

    ALL: Final[tuple[str, ...]] = (PASS, FAIL, MIXED, ERROR)


def compute_verdict(outcomes: Iterable[str]) -> str:
    """Derive the run-level verdict from per-case outcomes.

    Implements the verdict table in ``spec/runner-contract.md``. The
    pass/fail decision is made over the NON-errored cases, so a run where
    every case that actually ran passed is a ``pass`` even if some cases
    errored out.

    Args:
        outcomes: the per-case outcome strings (each one of ``Outcome.ALL``).

    Returns:
        One of the four strings in ``Verdict.ALL``.

        - ``error`` — there were no cases, or every case errored.
        - ``pass``  — every non-errored case is ``flip_to_pass``/``pass_kept``.
        - ``fail``  — every non-errored case is ``flip_to_fail``/``fail_kept``.
        - ``mixed`` — any combination of passing and failing non-errored cases.
    """
    items = list(outcomes)
    if not items:
        return Verdict.ERROR
    # "Every case errored" is checked before the pass/fail branches because
    # an empty non-errored set makes both `all(...)` checks vacuously true.
    non_errored = [o for o in items if o != Outcome.ERROR]
    if not non_errored:
        return Verdict.ERROR
    if all(o in (Outcome.FLIP_TO_PASS, Outcome.PASS_KEPT) for o in non_errored):
        return Verdict.PASS
    if all(o in (Outcome.FLIP_TO_FAIL, Outcome.FAIL_KEPT) for o in non_errored):
        return Verdict.FAIL
    return Verdict.MIXED
