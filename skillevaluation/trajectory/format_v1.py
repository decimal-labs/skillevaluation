"""Trajectory → transcript text converter (format v1).

See ``spec/trajectory-format.md`` for the canonical specification.

Produces a structured text representation of a trace + its spans +
llm_calls, suitable for feeding to an LLM judge. The format is
intentionally lossy:

  Included:
    - User messages (full)
    - Agent tool calls (name + args)
    - Tool outputs (first 200 chars + truncation marker)
    - Agent responses (full)
    - Step numbers (every line) + a per-step duration on agent-response
      lines only — those carry the originating LLM call's latency. User,
      tool-call, and tool-output lines have no duration, so they render
      ``[Step N]`` with no ``· Ts`` segment.

  Excluded:
    - rendered_input message arrays (raw OpenAI/Anthropic format)
    - Full tool output bodies (trimmed to 200 chars)
    - Agent's internal reasoning (reasoning tokens not surfaced)

Example output::

    [Step 1] User: Where is my order ORD-12345?
    [Step 2 · 1.1s] Agent: Let me look that up for you.
    [Step 3] Agent → tool: get_order_status(order_id="ORD-12345")
    [Step 4] Tool output: {"status":"shipped","carrier":"UPS"}
    [Step 5] Agent: Your order ORD-12345 was shipped via UPS.

The format is stable. If it changes, FORMAT_VERSION bumps so historical
reports remain distinguishable.

This module is intentionally agnostic about input shape — it uses duck
typing (``getattr``) so callers can pass ORM objects or dict-shaped
equivalents interchangeably.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

FORMAT_VERSION = "v1"

# Tool-output truncation budget. Set with the judge's context window in
# mind — at 200 chars × ~50 steps = 10k chars, well within Gemini Flash's
# 1M-token budget even with the prompt overhead.
TOOL_OUTPUT_MAX_CHARS = 200


def build_transcript_v1(
    *,
    user_input: str | None,
    final_output: str | None,
    llm_calls: Iterable[Any],
    spans: Iterable[Any] | None = None,
) -> str:
    """Convert trace data into a transcript string.

    All trace data must already be loaded — this function does no I/O.

    Args:
        user_input: the user's input that started the session. First
            line of the transcript.
        final_output: the agent's final response to the user. Last line.
        llm_calls: list of LLM call records ordered by step. Each
            contributes its output text and tool calls.
        spans: optional list of span records. Used to extract tool
            outputs (span_type='tool' → output_preview).

    Returns:
        Multi-line transcript string with [Step N · Ts] prefixes.
    """
    lines: list[str] = []
    step = 1

    if user_input:
        lines.append(_format_line(step, None, f"User: {user_input.strip()}"))
        step += 1

    # Build per-name queues of tool outputs, consumed in call order so
    # repeated calls to the same tool get their own output (not the first).
    tool_outputs_by_name = _index_tool_outputs(spans)

    for call in _sorted(llm_calls):
        duration_s = _duration_seconds(call)

        # The assistant's response text.
        output_text = _coerce_output_text(call)
        if output_text:
            lines.append(_format_line(step, duration_s, f"Agent: {output_text.strip()}"))
            step += 1

        # Tool calls — name + args, no full output body.
        tool_calls = _get_tool_calls(call)
        for tc in tool_calls:
            name = tc.get("name") or tc.get("function", {}).get("name") or "unknown_tool"
            args = (
                tc.get("args")
                or tc.get("arguments")
                or tc.get("function", {}).get("arguments")
                or {}
            )
            args_str = _format_tool_args(args)
            lines.append(
                _format_line(step, None, f"Agent → tool: {name}({args_str})")
            )
            step += 1

            # Pair with the next unconsumed output for this tool name.
            queue = tool_outputs_by_name.get(name)
            if queue:
                output_blob = queue.popleft()
                lines.append(
                    _format_line(step, None, f"Tool output: {_truncate(output_blob)}")
                )
                step += 1

    if final_output:
        lines.append(_format_line(step, None, f"Agent: {final_output.strip()}"))

    return "\n".join(lines)


# ── helpers ─────────────────────────────────────────────────────────────


def _duration_marker(duration_s: float | None) -> str | None:
    """The ``· Ts`` segment, rounded to one decimal place **half away from zero** on the decimal
    value (spec/trajectory-format.md — NOT Python's binary round-half-even, which would drift ties
    like 0.25→0.2 and break cross-language byte-equality). Returns None when the duration is absent
    or rounds to 0.0 (a sub-50ms step carries no marker). Formatting the quantized ``Decimal``
    directly (e.g. ``Decimal('0.3')`` → ``'0.3'``) avoids re-introducing a binary-float ``%.1f``."""
    if duration_s is None:
        return None
    q = Decimal(str(duration_s)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if q <= 0:
        return None
    return f"{q}s"


def _format_line(step: int, duration_s: float | None, body: str) -> str:
    marker = _duration_marker(duration_s)
    prefix = f"[Step {step} · {marker}]" if marker else f"[Step {step}]"
    return f"{prefix} {body}"


def _sort_key_ts(call: Any) -> float:
    """Epoch-seconds sort key. Missing/invalid timestamps sort to the end.

    Using a float key (not the datetime itself) sidesteps two crashes:
    comparing tz-aware against naive datetimes, and comparing a real
    datetime against ``datetime.max`` when only some rows have a timestamp.
    """
    ts = getattr(call, "started_at", None)
    if ts is None:
        return float("inf")
    try:
        return float(ts.timestamp())
    except (AttributeError, OSError, ValueError, OverflowError):
        return float("inf")


def _sorted(calls: Iterable[Any]) -> list[Any]:
    """Sort llm_calls by started_at; rows without started_at fall to the end.

    Secondary key (id coerced to str) keeps the sort stable and avoids
    int-vs-str comparison errors across mixed id types.
    """
    return sorted(
        list(calls),
        key=lambda c: (_sort_key_ts(c), str(getattr(c, "id", "") or "")),
    )


def _duration_seconds(call: Any) -> float | None:
    """Best-effort step duration in seconds, from latency_ms or end-start."""
    latency_ms = getattr(call, "latency_ms", None)
    if latency_ms:
        return float(latency_ms) / 1000.0
    started = getattr(call, "started_at", None)
    ended = getattr(call, "ended_at", None)
    if started and ended:
        return float((ended - started).total_seconds())
    return None


def _coerce_output_text(call: Any) -> str | None:
    """Pull the assistant's response out of an LLM call record."""
    preview = getattr(call, "output_preview", None)
    if preview:
        return str(preview)
    output_json = getattr(call, "output_json", None)
    if isinstance(output_json, dict):
        content = output_json.get("content")
        if isinstance(content, str):
            return content
        # Anthropic-style {"content": [{"type": "text", "text": "..."}]}.
        if isinstance(content, list):
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            joined = " ".join(t for t in texts if t)
            if joined:
                return joined
    return None


def _get_tool_calls(call: Any) -> list[dict[str, Any]]:
    """Return tool_calls as a normalized list of dicts."""
    raw = getattr(call, "tool_calls_json", None)
    if not raw:
        return []
    if isinstance(raw, list):
        return [tc for tc in raw if isinstance(tc, dict)]
    return []


def _format_tool_args(args: Any) -> str:
    """Render tool args inline — short and readable, not pretty-printed."""
    if isinstance(args, str):
        return args
    if not isinstance(args, dict):
        return str(args)
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        if isinstance(v, str):
            v_str = f'"{v}"'
        else:
            # Normative separators (spec/trajectory-format.md): ", " and ": " — pinned explicitly so
            # the canonical bytes don't depend on a language's JSON default (a compact ",",":" or a
            # pretty-printer would diverge). Source key order is preserved (no sort_keys).
            v_str = json.dumps(v, ensure_ascii=False, separators=(", ", ": "), default=str)
        parts.append(f"{k}={v_str}")
    return ", ".join(parts)


def _truncate(text: str, max_chars: int = TOOL_OUTPUT_MAX_CHARS) -> str:
    """Truncate tool output, append a count of how much was dropped."""
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return f"{text[:max_chars]} [truncated, {dropped} more chars]"


def _index_tool_outputs(spans: Iterable[Any] | None) -> dict[str, deque[str]]:
    """Build {tool_name → queue of output_previews} from 'tool' spans.

    One queue per tool name, in span iteration order, so the loop can
    ``popleft`` the matching output for each successive call to that tool.
    Previously this kept only the first output per name, so a tool called
    N times rendered the first output N times.
    """
    result: dict[str, deque[str]] = {}
    if not spans:
        return result
    for span in spans:
        if getattr(span, "span_type", None) != "tool":
            continue
        name = getattr(span, "name", "")
        out = getattr(span, "output_preview", None)
        if not name or not out:
            continue
        out_str = out if isinstance(out, str) else json.dumps(out, default=str)
        result.setdefault(name, deque()).append(out_str)
    return result
