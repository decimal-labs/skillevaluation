"""Agent adapters — how the runner invokes an agent for one arm.

The runner contract deliberately does not specify how the agent is
invoked; adapters are that seam. The reference runner ships:

* :class:`~skillevaluation.runner.adapters.llm.LLMAdapter` — direct
  single-shot LLM call (Anthropic / OpenAI / Gemini REST, your own API
  key). The **supported** adapter.
* :class:`~skillevaluation.runner.adapters.mock.MockAdapter` —
  deterministic canned responses for tests, CI plumbing checks, and
  dry runs. No network.
* :class:`~skillevaluation.runner.adapters.claude_code.ClaudeCodeAdapter`
  — EXPERIMENTAL: drives a locally installed ``claude`` CLI so the
  skill is exercised by a real agent runtime.

Implement :class:`~skillevaluation.runner.adapters.base.AgentAdapter`
to plug in your own runtime (a LangChain app, an OpenAI Agents loop, a
hosted platform…).
"""

from .base import AdapterError, AgentAdapter, ArmExecution

__all__ = ["AdapterError", "AgentAdapter", "ArmExecution"]
