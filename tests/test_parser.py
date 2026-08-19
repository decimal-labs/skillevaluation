"""Tests for skillevaluation.parser — lifted from DecimalAI's benchmark suite."""

from __future__ import annotations

import pytest

from skillevaluation.parser import EvalYamlParseError, is_trigger_only_case, parse_eval_yaml


def test_parser_happy_path():
    cases = parse_eval_yaml(
        """
cases:
  - name: a
    prompt: 'test prompt'
    expectations:
      - 'response is non-empty'
    validators:
      - cmd: "test -f /tmp/x"
        expect_exit_code: 0
"""
    )
    assert len(cases) == 1
    assert cases[0].name == "a"
    assert len(cases[0].expectations) == 1
    assert len(cases[0].script_validators) == 1
    assert cases[0].script_validators[0]["expect_exit_code"] == 0


def test_parser_rejects_missing_assertions():
    with pytest.raises(EvalYamlParseError, match="at least one assertion"):
        parse_eval_yaml(
            """
cases:
  - name: a
    prompt: 'foo'
"""
        )


def test_parser_rejects_duplicate_names():
    with pytest.raises(EvalYamlParseError, match="duplicate case name"):
        parse_eval_yaml(
            """
cases:
  - name: same
    prompt: 'foo'
    expectations:
      - 'bar'
  - name: same
    prompt: 'baz'
    expectations:
      - 'qux'
"""
        )


def test_parser_rejects_empty_input():
    with pytest.raises(EvalYamlParseError, match="empty"):
        parse_eval_yaml("")


def test_parser_rejects_missing_cases_list():
    with pytest.raises(EvalYamlParseError, match="cases:"):
        parse_eval_yaml("not_cases: []\n")


def test_parser_rejects_validator_without_cmd():
    with pytest.raises(EvalYamlParseError, match="cmd"):
        parse_eval_yaml(
            """
cases:
  - name: a
    prompt: 'foo'
    validators:
      - label: 'no command here'
"""
        )


@pytest.mark.parametrize("invalid_value", ['"3"', "true", "3.0"])
def test_parser_rejects_non_integer_expect_exit_code(invalid_value):
    with pytest.raises(
        EvalYamlParseError,
        match=r"case 'a'.*validator #0.*expect_exit_code.*integer",
    ):
        parse_eval_yaml(
            f"""
cases:
  - name: a
    prompt: 'foo'
    validators:
      - cmd: "true"
        expect_exit_code: {invalid_value}
"""
        )


def test_parser_rejects_unknown_top_level_key():
    """A sibling key next to a valid `cases:` is a typo, not silently dropped."""
    with pytest.raises(EvalYamlParseError, match="unknown top-level key"):
        parse_eval_yaml(
            """
suite_name: my-suite
cases:
  - name: a
    prompt: 'foo'
    expectations: ['ok']
"""
        )


def test_parser_rejects_unknown_case_key():
    """`expectation` (singular) is a common typo — must fail, not be ignored."""
    with pytest.raises(EvalYamlParseError, match="unknown key"):
        parse_eval_yaml(
            """
cases:
  - name: a
    prompt: 'foo'
    expectation:
      - 'should have been expectations'
"""
        )


def test_parser_rejects_unknown_validator_key():
    """`exit_code` vs `expect_exit_code` would silently default to 0 if ignored."""
    with pytest.raises(EvalYamlParseError, match="unknown key"):
        parse_eval_yaml(
            """
cases:
  - name: a
    prompt: 'foo'
    validators:
      - cmd: "test -f /tmp/x"
        exit_code: 0
"""
        )


def test_parser_preserves_optional_fields():
    cases = parse_eval_yaml(
        """
cases:
  - name: with_metadata
    prompt: 'test'
    expectations: ['ok']
    tags: [pii, gdpr]
    description: 'A test case with metadata'
"""
    )
    assert cases[0].tags == ["pii", "gdpr"]
    assert cases[0].description == "A test case with metadata"


def test_trigger_only_case_parses_without_grader():
    """Spec 0.3.0: a case with should_trigger and NO grader is a legal TRIGGER-ONLY case —
    exempt from the at-least-one-grader rule (it belongs to the trigger rail, not the A/B loop)."""
    cases = parse_eval_yaml(
        """
cases:
  - name: fires_on_topic
    prompt: "How should I format an API error envelope?"
    should_trigger: true
  - name: near_miss
    prompt: "Write a limerick about HTTP status codes."
    should_trigger: false
"""
    )
    assert [c.should_trigger for c in cases] == [True, False]
    assert all(is_trigger_only_case(c) for c in cases)


def test_should_trigger_composes_with_graded_fields():
    """A case may carry should_trigger AND graders — then it runs the normal A/B loop too."""
    cases = parse_eval_yaml(
        """
cases:
  - name: both
    prompt: "format this error"
    should_trigger: true
    expectations: ["uses the envelope"]
"""
    )
    assert cases[0].should_trigger is True
    assert is_trigger_only_case(cases[0]) is False


def test_should_trigger_must_be_boolean():
    # YAML `1` must not sneak through as truthy — the field is a bool contract.
    with pytest.raises(EvalYamlParseError, match="'should_trigger' must be a boolean"):
        parse_eval_yaml(
            """
cases:
  - name: x
    prompt: p
    should_trigger: 1
"""
        )


def test_case_without_grader_or_should_trigger_still_rejected():
    """The trigger-only exemption must not weaken the grader rule for ordinary cases."""
    with pytest.raises(EvalYamlParseError, match="at least one assertion"):
        parse_eval_yaml(
            """
cases:
  - name: x
    prompt: p
    tags: [orphan]
"""
        )


def test_setup_mapping_form_parses_files_and_commands():
    """Spec 0.3.0: setup may be {files: {relpath: content}, commands: [...]} — files staged
    before commands run."""
    cases = parse_eval_yaml(
        """
cases:
  - name: seeded
    prompt: p
    setup:
      files:
        schema.json: '{"email": "string"}'
        data/rows.csv: "a,b\\n1,2\\n"
      commands:
        - "cp schema.json copy.json"
    expectations: [ok]
"""
    )
    c = cases[0]
    assert c.setup_files == {"schema.json": '{"email": "string"}', "data/rows.csv": "a,b\n1,2\n"}
    assert c.setup_steps == ["cp schema.json copy.json"]


def test_setup_list_form_unchanged():
    """The legacy list-of-commands form stays valid and leaves setup_files empty."""
    cases = parse_eval_yaml(
        """
cases:
  - name: legacy
    prompt: p
    setup:
      - "echo hi > x.txt"
    expectations: [ok]
"""
    )
    assert cases[0].setup_steps == ["echo hi > x.txt"]
    assert cases[0].setup_files == {}


def test_setup_mapping_rejects_unknown_key():
    """`file:` (singular) would silently stage nothing if ignored — same strictness as cases."""
    with pytest.raises(EvalYamlParseError, match="'setup' mapping has unknown key"):
        parse_eval_yaml(
            """
cases:
  - name: x
    prompt: p
    setup:
      file:
        a.txt: hi
    expectations: [ok]
"""
        )


def test_setup_files_values_must_be_strings():
    with pytest.raises(EvalYamlParseError, match="'setup.files' must map"):
        parse_eval_yaml(
            """
cases:
  - name: x
    prompt: p
    setup:
      files:
        a.json: {not: a-string}
    expectations: [ok]
"""
        )
