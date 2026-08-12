"""Tests for skillevaluation.trajectory.format_v1.

Uses dict-shaped inputs so we don't depend on any ORM. The function
itself uses ``getattr`` so duck-typed objects work; ``SimpleNamespace``
gives us a clean stand-in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from skillevaluation.trajectory.format_v1 import FORMAT_VERSION, build_transcript_v1


def _llm_call(*, output_preview=None, tool_calls_json=None, latency_ms=None, started_at=None):
    return SimpleNamespace(
        output_preview=output_preview,
        tool_calls_json=tool_calls_json,
        latency_ms=latency_ms,
        started_at=started_at,
        ended_at=None,
        output_json=None,
        id="x",
    )


def test_format_version_is_v1():
    assert FORMAT_VERSION == "v1"


def test_user_input_appears_first():
    out = build_transcript_v1(
        user_input="hello world",
        final_output=None,
        llm_calls=[],
    )
    assert out.startswith("[Step 1] User: hello world")


def test_agent_response_after_user():
    out = build_transcript_v1(
        user_input="hi",
        final_output=None,
        llm_calls=[_llm_call(output_preview="hello back")],
    )
    lines = out.split("\n")
    assert "User: hi" in lines[0]
    assert "Agent: hello back" in lines[1]


def test_tool_call_renders_name_and_args():
    out = build_transcript_v1(
        user_input=None,
        final_output=None,
        llm_calls=[
            _llm_call(
                tool_calls_json=[
                    {"name": "get_order", "args": {"order_id": "ORD-1"}},
                ],
            ),
        ],
    )
    assert 'Agent → tool: get_order(order_id="ORD-1")' in out


def test_step_durations_render_when_present():
    out = build_transcript_v1(
        user_input=None,
        final_output=None,
        llm_calls=[_llm_call(output_preview="hi", latency_ms=1200)],
    )
    assert "1.2s" in out


def test_tool_output_truncated_at_200_chars():
    long_output = "x" * 300
    span = SimpleNamespace(span_type="tool", name="get_thing", output_preview=long_output)
    out = build_transcript_v1(
        user_input=None,
        final_output=None,
        llm_calls=[
            _llm_call(tool_calls_json=[{"name": "get_thing", "args": {}}]),
        ],
        spans=[span],
    )
    assert "[truncated, 100 more chars]" in out


def test_final_output_appears_last():
    out = build_transcript_v1(
        user_input="hi",
        final_output="goodbye",
        llm_calls=[],
    )
    assert out.endswith("Agent: goodbye")


def test_anthropic_style_content_list_extracts_text():
    """Anthropic API responses use {"content": [{"type": "text", "text": "..."}]}."""
    call = SimpleNamespace(
        output_preview=None,
        output_json={"content": [{"type": "text", "text": "hello"}]},
        tool_calls_json=None,
        latency_ms=None,
        started_at=None,
        ended_at=None,
        id="x",
    )
    out = build_transcript_v1(user_input=None, final_output=None, llm_calls=[call])
    assert "Agent: hello" in out


def test_mixed_tz_aware_and_missing_started_at_does_not_crash():
    """Regression: sorting tz-aware datetimes against naive ``datetime.max``
    (the old missing-timestamp sentinel) raised TypeError. Rows without a
    timestamp must sort to the end without crashing."""
    aware = _llm_call(output_preview="second", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    no_ts = _llm_call(output_preview="first", started_at=None)
    # no_ts first in input, but `aware` has a real timestamp so it sorts ahead.
    out = build_transcript_v1(user_input=None, final_output=None, llm_calls=[no_ts, aware])
    lines = out.split("\n")
    assert "Agent: second" in lines[0]
    assert "Agent: first" in lines[1]


def test_repeated_tool_call_gets_its_own_output():
    """Regression: a tool called N times used to render the FIRST output N
    times. Each call must be paired with its own output, in call order."""
    spans = [
        SimpleNamespace(span_type="tool", name="get_order", output_preview='{"id":"ORD-1"}'),
        SimpleNamespace(span_type="tool", name="get_order", output_preview='{"id":"ORD-2"}'),
    ]
    out = build_transcript_v1(
        user_input=None,
        final_output=None,
        llm_calls=[
            _llm_call(tool_calls_json=[{"name": "get_order", "args": {"order_id": "ORD-1"}}]),
            _llm_call(tool_calls_json=[{"name": "get_order", "args": {"order_id": "ORD-2"}}]),
        ],
        spans=spans,
    )
    assert '{"id":"ORD-1"}' in out
    assert '{"id":"ORD-2"}' in out
    # Order preserved: ORD-1's output before ORD-2's.
    assert out.index('{"id":"ORD-1"}') < out.index('{"id":"ORD-2"}')
