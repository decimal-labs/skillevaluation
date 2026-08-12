"""Adapter contract: invoke an agent once, with or without the skill.

An adapter runs ONE arm of one case: given the case prompt, an optional
skill body (``None`` for the without-skill arm), and a workspace to
operate in, it returns what happened as an :class:`ArmExecution`.

Metric semantics follow the runner contract's per-arm table
(``spec/runner-contract.md``): ``passed``/``task_attempted`` are NOT the
adapter's job — grading happens in the orchestrator against the case's
expectations + validators; the adapter only reports execution facts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AdapterError(RuntimeError):
    """Adapter misconfiguration (missing API key, missing binary, …).

    Raised at *construction/validation* time for problems that would fail
    every case. Per-case transient failures are NOT raised — they are
    reported via ``ArmExecution(errored=True)`` so the runner can classify
    the case outcome as ``error`` and keep going.
    """


@dataclass
class ArmExecution:
    """What one agent invocation did. The adapter's output."""

    final_output: str = ""
    duration_ms: int = 0
    turns: int = 0
    total_tokens: int = 0
    tool_call_count: int = 0
    errored: bool = False
    error: str | None = None
    # Multi-turn trajectory: the labeled conversation event log [(objection_count, event)] for a
    # conversation arm (empty for single_shot) — inspectable + makes the state-machine verdict
    # reproducible.
    events: list[Any] = field(default_factory=list)
    # Optional adapter-specific extras (model ids, cost, raw usage…) —
    # carried into results.json under the arm's metrics (the schema
    # allows additional properties).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the baseline cache."""
        return {
            "final_output": self.final_output,
            "duration_ms": self.duration_ms,
            "turns": self.turns,
            "total_tokens": self.total_tokens,
            "tool_call_count": self.tool_call_count,
            "errored": self.errored,
            "error": self.error,
            "events": self.events,
            "extra": self.extra,
        }

    @classmethod
    def from_payload(cls, d: dict[str, Any]) -> ArmExecution:
        """Rehydrate from a baseline-cache payload."""
        return cls(
            final_output=str(d.get("final_output") or ""),
            duration_ms=int(d.get("duration_ms") or 0),
            turns=int(d.get("turns") or 0),
            total_tokens=int(d.get("total_tokens") or 0),
            tool_call_count=int(d.get("tool_call_count") or 0),
            errored=bool(d.get("errored")),
            error=d.get("error"),
            events=list(d.get("events") or []),
            extra=dict(d.get("extra") or {}),
        )


class AgentAdapter(ABC):
    """Invoke an agent for one arm of one case."""

    #: short machine name ("llm", "mock", "claude-code", …)
    name: str = "adapter"

    @abstractmethod
    def run(
        self, *, prompt: str, skill_body: str | None, workspace: Path
    ) -> ArmExecution:
        """Execute the agent once.

        Args:
            prompt: the case's user prompt.
            skill_body: the SKILL.md body for the with-skill arm,
                ``None`` for the without-skill arm.
            workspace: prepared per-case directory the agent may read
                and write.

        Returns:
            An :class:`ArmExecution`. Transient failures set
            ``errored=True`` rather than raising.
        """

    @property
    def identity(self) -> str:
        """Stable identity string used to scope the baseline cache.

        Must change when the agent configuration changes in a way that
        invalidates baselines (model swap, runtime swap) — the cache
        contract requires it.
        """
        return self.name

    def complete_turn(
        self, *, system: str | None, prompt: str, role: str = "agent"
    ) -> tuple[str, int]:
        """One raw LLM completion — returns ``(text, total_tokens)``.

        A single-message primitive some adapters expose alongside ``run`` (used by tests and by
        platform tooling that needs a bare completion, e.g. an LLM judge transport or a routing
        grader). ``role`` lets a caller pin an honesty-critical call deterministic (temperature 0)
        while agent replies may sample. Optional: the default raises so an adapter without the
        primitive fails loudly.

        Raises:
            AdapterError: when the adapter does not expose a completion primitive.
        """
        raise AdapterError(
            f"adapter '{self.name}' does not expose a completion primitive (complete_turn)"
        )

    def validate(self) -> None:
        """Fail fast on misconfiguration (missing key/binary).

        Called once before a run. Default: no checks.

        Raises:
            AdapterError: when the adapter cannot run any case.
        """
