"""Mock adapter + mock judge — deterministic, networkless.

For unit tests, CI plumbing checks, and ``skillevaluation run --adapter
mock`` dry runs (does my eval.yaml parse? do my validators execute? does
the report render?) without spending a token.

Determinism contract:

* The **adapter** replies from a canned response table (or a response
  function), with fixed metrics.
* The **mock judge** passes an expectation iff the expectation text
  appears case-insensitively in the arm's output. Crude on purpose — a
  plumbing check, not a semantic grade. Structural assertions
  (``response_contains:`` …) still grade exactly, since those never
  reach a judge.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import AgentAdapter, ArmExecution

# (prompt, with_skill) -> response text
ResponseFn = Callable[[str, bool], str]


class MockAdapter(AgentAdapter):
    """Canned-response agent.

    Args:
        responses: either a ``ResponseFn`` or a dict of
            ``{prompt: {"with": text, "without": text}}``. Prompts absent
            from the dict echo a deterministic default.
        with_metrics / without_metrics: per-arm metric overrides
            (``duration_ms``, ``turns``, ``total_tokens``,
            ``tool_call_count``).
    """

    name = "mock"

    def __init__(
        self,
        responses: ResponseFn | dict[str, dict[str, str]] | None = None,
        *,
        with_metrics: dict[str, int] | None = None,
        without_metrics: dict[str, int] | None = None,
    ):
        self._responses = responses
        self._with_metrics = {
            "duration_ms": 1200, "turns": 2, "total_tokens": 400, "tool_call_count": 1,
            **(with_metrics or {}),
        }
        self._without_metrics = {
            "duration_ms": 2400, "turns": 4, "total_tokens": 900, "tool_call_count": 3,
            **(without_metrics or {}),
        }
        self.calls: list[dict[str, Any]] = []  # inspection hook for tests

    @property
    def identity(self) -> str:
        return "mock"

    def _text_for(self, prompt: str, with_skill: bool) -> str:
        if callable(self._responses):
            return self._responses(prompt, with_skill)
        if isinstance(self._responses, dict) and prompt in self._responses:
            arm = "with" if with_skill else "without"
            return self._responses[prompt].get(arm, "")
        arm_label = "with-skill" if with_skill else "without-skill"
        return f"[mock {arm_label} response] {prompt}"

    def run(
        self, *, prompt: str, skill_body: str | None, workspace: Path
    ) -> ArmExecution:
        with_skill = skill_body is not None
        self.calls.append(
            {"prompt": prompt, "with_skill": with_skill, "workspace": str(workspace)}
        )
        metrics = self._with_metrics if with_skill else self._without_metrics
        return ArmExecution(
            final_output=self._text_for(prompt, with_skill),
            duration_ms=metrics["duration_ms"],
            turns=metrics["turns"],
            total_tokens=metrics["total_tokens"],
            tool_call_count=metrics["tool_call_count"],
        )

    def complete_turn(
        self, *, system: str | None, prompt: str, role: str = "agent"
    ) -> tuple[str, int]:
        """One canned completion. ``with_skill`` is inferred from whether a system prompt (the
        skill body) is present, so a ``ResponseFn`` can branch on prompt + arm. ``role`` is
        recorded for test inspection."""
        self.calls.append({
            "prompt": prompt, "with_skill": system is not None,
            "complete_turn": True, "role": role,
        })
        return self._text_for(prompt, system is not None), 10


def mock_judge_call(rendered_prompt: str) -> dict[str, Any]:
    """Deterministic stand-in for an LLM judge.

    Parses the expectation and final response back out of the rendered
    reference judge prompt, then passes iff the expectation text appears
    (case-insensitively) in the response. Documented as plumbing-grade.
    """
    expectation = _between(rendered_prompt, "Expectation: ", "\n")
    # The reference judge prompt fences the agent output in an AGENT_OUTPUT block (prompt-injection
    # defense); read the fenced content. Fall back to the whole prompt if the fence is absent.
    response = _between(rendered_prompt, "<<<AGENT_OUTPUT", "AGENT_OUTPUT>>>") or rendered_prompt
    passed = bool(expectation) and expectation.lower() in response.lower()
    return {
        "passed": passed,
        "reason": (
            f"[mock judge] expectation text {'found' if passed else 'not found'} "
            f"verbatim in the response"
        ),
    }


def _between(text: str, start: str, end: str) -> str:
    try:
        chunk = text.split(start, 1)[1]
        return chunk.split(end, 1)[0].strip()
    except IndexError:
        return ""
