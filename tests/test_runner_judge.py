"""judge — structural fast path, LLM path, error degradation, JSON parsing."""

from __future__ import annotations

import pytest

from skillevaluation.runner.judge import (
    JudgeOutputError,
    judge_expectations,
    parse_judge_json,
    try_structural_assertion,
)

# ── structural assertions ────────────────────────────────────────────


def test_valid_json_passes():
    r = try_structural_assertion("response_is_valid_json", '{"a": 1}')
    assert r["passed"] is True


def test_invalid_json_fails_with_reason():
    r = try_structural_assertion("response_is_valid_json", "not json")
    assert r["passed"] is False
    assert "Not valid JSON" in r["reason"]


def test_non_empty_threshold_is_20_chars():
    assert try_structural_assertion("response_is_non_empty", "x" * 20)["passed"]
    assert not try_structural_assertion("response_is_non_empty", "x" * 19)["passed"]


def test_contains_is_case_insensitive():
    r = try_structural_assertion("response_contains:SELECT", "we ran select * from t")
    assert r["passed"] is True


def test_contains_preserves_needle_case_from_original():
    """Directive matching is case-insensitive but the needle comes from the
    ORIGINAL expectation text, not the lowercased copy."""
    r = try_structural_assertion("RESPONSE_CONTAINS:NeEdLe", "found needle here")
    assert r["passed"] is True
    assert "NeEdLe" in r["reason"]


def test_matches_regex():
    assert try_structural_assertion("response_matches:fo+bar", "xx foobar yy")["passed"]
    assert not try_structural_assertion("response_matches:^\\d+$", "abc")["passed"]


def test_bad_regex_fails_gracefully():
    r = try_structural_assertion("response_matches:([", "anything")
    assert r["passed"] is False
    assert "Bad regex" in r["reason"]


def test_prose_expectation_returns_none():
    assert try_structural_assertion("The response is polite", "hi") is None


# ── judge_expectations ───────────────────────────────────────────────


def test_expectations_are_always_judged():
    """Two-category model (2026-06-13): every `expectations` entry is LLM-judged —
    even one that *looks* structural. Deterministic structural checks belong in
    `validators` (graded by code), never in `expectations`."""
    seen = []

    def recording_judge(prompt):
        seen.append(prompt)
        return {"passed": True, "reason": "judged"}

    results = judge_expectations(
        ["response_contains:ok"],  # looks structural, but it's an expectation → judged
        final_output="ok — a sufficiently long response here",
        judge_call=recording_judge,
    )
    assert len(seen) == 1  # the judge WAS called — no structural short-circuit
    assert results[0]["passed"] is True


def test_llm_judged_expectation_uses_judge_call():
    seen_prompts = []

    def fake_judge(prompt):
        seen_prompts.append(prompt)
        return {"passed": True, "reason": "The response cites the figure."}

    results = judge_expectations(
        ["The response cites a revenue figure"],
        final_output="Revenue was $1M.",
        prompt="What was revenue?",
        transcript="[Step 1] User: What was revenue?\n[Step 2] Agent: Revenue was $1M.",
        judge_call=fake_judge,
    )
    assert results[0]["passed"] is True
    assert results[0]["expectation"] == "The response cites a revenue figure"
    # The rendered prompt carries all four contract inputs.
    rendered = seen_prompts[0]
    assert "The response cites a revenue figure" in rendered
    assert "What was revenue?" in rendered
    assert "[Step 1]" in rendered
    assert "Revenue was $1M." in rendered


def test_transcript_falls_back_to_final_output():
    captured = {}

    def fake_judge(prompt):
        captured["prompt"] = prompt
        return {"passed": False, "reason": "No."}

    judge_expectations(
        ["mentions A"], final_output="only the final text", judge_call=fake_judge
    )
    assert "only the final text" in captured["prompt"]


def test_judge_transport_error_degrades_to_errored_expectation():
    """A transport failure is an UNGRADED expectation, not a model fail:
    ``errored: true`` rides on the result so runners roll it up into the arm's
    errored flag → case outcome 'error' → excluded from lift (spec/llm-judge.md
    edge-case table). Grading it as a plain fail let a judge outage on the
    without-arm manufacture flip_to_pass lift (2026-07 measurement audit)."""
    def broken_judge(prompt):
        raise RuntimeError("api down")

    results = judge_expectations(
        ["something semantic"], final_output="text", judge_call=broken_judge
    )
    assert results[0]["passed"] is False
    assert results[0]["errored"] is True
    assert "LLM judge error" in results[0]["reason"]
    assert "api down" in results[0]["reason"]


def test_judge_verdicts_carry_no_errored_flag():
    """A judged (pass or fail) expectation is NOT errored — the flag is
    reserved for transport failure, so runners can key case-error rollup on it."""
    results = judge_expectations(
        ["a"], final_output="text",
        judge_call=lambda p: {"passed": False, "reason": "contradicts"},
    )
    assert results[0]["passed"] is False
    assert not results[0].get("errored")


def test_reason_truncated_to_300_chars():
    results = judge_expectations(
        ["x"], final_output="y",
        judge_call=lambda p: {"passed": True, "reason": "r" * 1000},
    )
    assert len(results[0]["reason"]) == 300


def test_score_passthrough_only_when_valid():
    ok = judge_expectations(
        ["a"], final_output="y", judge_call=lambda p: {"passed": True, "reason": "r", "score": 0.9}
    )
    assert ok[0]["score"] == 0.9
    bad = judge_expectations(
        ["a"], final_output="y", judge_call=lambda p: {"passed": True, "reason": "r", "score": 7}
    )
    assert "score" not in bad[0]


def test_results_conform_to_judge_result_schema():
    jsonschema = pytest.importorskip("jsonschema")
    from skillevaluation.resources import load_schema

    schema = load_schema("judge-result")
    results = judge_expectations(
        ["response_is_non_empty", "semantic check"],
        final_output="a response easily longer than twenty characters",
        judge_call=lambda p: {"passed": True, "reason": "Cited evidence."},
    )
    for r in results:
        jsonschema.validate(r, schema)


# ── parse_judge_json ─────────────────────────────────────────────────


def test_parses_bare_json():
    assert parse_judge_json('{"passed": true, "reason": "ok"}')["passed"] is True


def test_parses_fenced_json():
    text = 'Here you go:\n```json\n{"passed": false, "reason": "nope"}\n```\nthanks'
    assert parse_judge_json(text)["passed"] is False


def test_parses_json_with_preamble():
    text = 'The verdict is {"passed": true, "reason": "fine"} overall.'
    assert parse_judge_json(text)["passed"] is True


def test_unparseable_raises():
    with pytest.raises(JudgeOutputError):
        parse_judge_json("no json here at all")


def test_directive_with_leading_whitespace_extracts_right_needle():
    """The needle must come from the STRIPPED expectation — slicing the raw
    string at a fixed offset grabs garbage when there's leading space."""
    res = try_structural_assertion("  response_contains:PII", "the answer mentions PII here")
    assert res == {"passed": True, "reason": "Found substring 'PII'"}
    res = try_structural_assertion("  response_contains:PII", "no match")
    assert res["passed"] is False
    assert "'PII'" in res["reason"]


def test_empty_contains_directive_fails_loudly():
    res = try_structural_assertion("response_contains:", "anything")
    assert res["passed"] is False
    assert "no substring" in res["reason"]


def test_empty_matches_directive_fails_loudly():
    res = try_structural_assertion("response_matches:   ", "anything")
    assert res["passed"] is False
    assert "no pattern" in res["reason"]


def test_suite_needs_llm_judge_detection():
    from types import SimpleNamespace

    from skillevaluation.runner.judge import suite_needs_llm_judge

    # Two-category model: ANY expectations → judge needed (expectations are always
    # judged now); a validator-only suite (no expectations) needs no judge.
    has_expectations = SimpleNamespace(expectations=["labels email as PII"])
    validators_only = SimpleNamespace(expectations=[])

    assert suite_needs_llm_judge([validators_only]) is False
    assert suite_needs_llm_judge([has_expectations, validators_only]) is True
    assert suite_needs_llm_judge([]) is False
