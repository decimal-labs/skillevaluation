"""Direct-LLM adapter — single-shot completion against a provider API.

This is the reference runner's *supported* adapter. It mirrors the shape
of DecimalAI's hosted benchmark execution, which is likewise a single
stubbed completion rather than a full agent runtime: one completion per
arm, the skill body injected into the prompt for the with-skill arm.
That makes pass-rate and token deltas real, while ``turns`` and
``tool_call_count`` are honestly trivial (1 / 0) — a single-shot
completion has no tool loop. Use an agent-runtime adapter when those
dimensions matter.

Provider routing is by model-name prefix, with the conventional
environment variables:

| prefix              | provider  | env var                              |
|---------------------|-----------|--------------------------------------|
| ``claude…``         | Anthropic | ``ANTHROPIC_API_KEY``                |
| ``gpt…`` / ``o1/3/4…`` | OpenAI | ``OPENAI_API_KEY``                   |
| ``gemini…``         | Google    | ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` |

``httpx`` is imported lazily so the core package install stays
dependency-light; install ``skillevaluation[runner]`` to use this module.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..judge import JudgeCall, parse_judge_json
from .base import AdapterError, AgentAdapter, ArmExecution

logger = logging.getLogger("skillevaluation.runner.adapters.llm")

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_TOKENS = 4096

_ANTHROPIC_VERSION = "2023-06-01"
_OPENAI_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt")

# Same prompt construction as the platform's reference stub: the answer is
# captured VERBATIM (no JSON envelope) so output-format skills show their
# lift — wrapping would clean up the bare arm and mask exactly what such
# skills fix.
_SYSTEM_WITH_SKILL = "You are an agent. Follow this skill:\n\n{skill_body}\n\n"
_SYSTEM_WITHOUT_SKILL = "You are an agent."
_PROMPT_TEMPLATE = "{system}\n\nUser: {prompt}\n\nRespond as the agent. Use the skill if relevant."


@dataclass
class CompletionResult:
    text: str
    total_tokens: int  # 0 when the provider returned no usage block


def _provider_for(model: str) -> str:
    m = model.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(_OPENAI_PREFIXES):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    raise AdapterError(
        f"cannot infer provider for model {model!r} — expected a name starting with "
        f"'claude' (Anthropic), {'/'.join(_OPENAI_PREFIXES)} (OpenAI), or 'gemini' (Google)"
    )


def _body_snippet(response: Any, limit: int = 200) -> str:
    """First bytes of an error response body, for actionable messages."""
    try:
        return " ".join(str(response.text)[:limit].split())
    except Exception:
        return "(unreadable error body)"


def _api_key_for(provider: str) -> str:
    candidates = {
        "anthropic": ("ANTHROPIC_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    }[provider]
    for var in candidates:
        value = os.environ.get(var)
        if value:
            return value
    raise AdapterError(
        f"no API key for {provider} — set {' or '.join(candidates)} "
        f"(the local runner uses your own key; nothing is sent to DecimalAI)"
    )


# Transient provider failures worth retrying: rate limits + overload.
# Free-tier Gemini in particular 503s on bursts of back-to-back calls (a
# 3-case suite fires ~6+ requests with no think time between them).
_RETRYABLE_STATUS = (429, 500, 502, 503, 529)
DEFAULT_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 2.0  # 2s, 4s, 8s between the 4 attempts


class LLMClient:
    """Minimal provider-agnostic completion client (used by adapter + judge).

    Retries transient provider failures (429 / 5xx / connect errors) with
    exponential backoff, honoring ``Retry-After`` when the provider sends
    one. Non-transient errors (401, 400, …) raise immediately.
    """

    def __init__(
        self,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        self.model = model
        self.provider = _provider_for(model)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.max_attempts = max(1, max_attempts)

    def validate(self) -> None:
        """Raise AdapterError when the provider key is missing or httpx isn't installed."""
        _api_key_for(self.provider)
        self._httpx()

    @staticmethod
    def _httpx() -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — exercised via error message test
            raise AdapterError(
                "the direct-LLM adapter needs httpx — install the runner extra: "
                "pip install 'skillevaluation[runner]'"
            ) from exc
        return httpx

    def complete(self, prompt: str, *, temperature: float | None = None) -> CompletionResult:
        """One completion. Raises on transport/HTTP errors (caller maps to errored arm).

        ``temperature`` overrides the client default for this call only (used to pin the
        honesty-critical conversation labeler at 0 while the agent reply may sample); ``None`` keeps
        the client's configured temperature.

        Transient failures (429/5xx, connect errors, read timeouts) are
        retried ``max_attempts`` times with exponential backoff before the
        final exception propagates.
        """
        temp = self.temperature if temperature is None else temperature
        httpx = self._httpx()
        key = _api_key_for(self.provider)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                if self.provider == "anthropic":
                    return self._complete_anthropic(httpx, key, prompt, temp)
                if self.provider == "openai":
                    return self._complete_openai(httpx, key, prompt, temp)
                return self._complete_gemini(httpx, key, prompt, temp)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    # Append the provider's error body — that's where the
                    # actionable message lives ("temperature is not
                    # supported", "credit balance is too low", …).
                    raise httpx.HTTPStatusError(
                        f"{exc} — {_body_snippet(exc.response)}",
                        # _request, not .request: the property raises when
                        # the error was constructed without one.
                        request=getattr(exc, "_request", None),
                        response=exc.response,
                    ) from None
                last_exc = exc
                retry_after = exc.response.headers.get("retry-after")
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                retry_after = None
            if attempt == self.max_attempts:
                break
            delay = _BACKOFF_BASE_S * (2 ** (attempt - 1))
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            logger.warning(
                "%s transient failure (attempt %d/%d): %s — retrying in %.0fs",
                self.provider, attempt, self.max_attempts, last_exc, delay,
            )
            time.sleep(delay)
        assert last_exc is not None  # loop ran at least once to get here
        raise last_exc

    def _complete_anthropic(
        self, httpx: Any, key: str, prompt: str, temperature: float
    ) -> CompletionResult:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            part.get("text", "")
            for part in data.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return CompletionResult(text=text, total_tokens=tokens)

    def _complete_openai(
        self, httpx: Any, key: str, prompt: str, temperature: float
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        # OpenAI's reasoning-family models (gpt-5*, o-series) reject the
        # temperature parameter outright (400: "temperature is not
        # supported"). Older chat models still accept it.
        if not self.model.lower().startswith(("gpt-5", "o1", "o3", "o4")):
            payload["temperature"] = temperature
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
            json=payload,
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or [{}]
        text = str(((choices[0] or {}).get("message") or {}).get("content") or "")
        tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        return CompletionResult(text=text, total_tokens=tokens)

    def _complete_gemini(
        self, httpx: Any, key: str, prompt: str, temperature: float
    ) -> CompletionResult:
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            # Key goes in a header, NOT ?key= — httpx error strings embed the
            # full URL, and those strings end up in results.json (and any
            # --export-url upload). A query-param key would leak there.
            headers={"x-goog-api-key": key, "content-type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature},
            },
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or [{}]
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        tokens = int((data.get("usageMetadata") or {}).get("totalTokenCount") or 0)
        return CompletionResult(text=text, total_tokens=tokens)


class LLMAdapter(AgentAdapter):
    """Single-shot direct-LLM agent. The supported reference adapter."""

    name = "llm"

    def __init__(
        self,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.client = LLMClient(
            model, temperature=temperature, max_tokens=max_tokens, timeout_s=timeout_s
        )

    @property
    def model(self) -> str:
        return self.client.model

    @property
    def identity(self) -> str:
        return f"llm:{self.client.model}"

    def validate(self) -> None:
        self.client.validate()

    def run(
        self, *, prompt: str, skill_body: str | None, workspace: Path
    ) -> ArmExecution:
        system = (
            _SYSTEM_WITH_SKILL.format(skill_body=skill_body)
            if skill_body
            else _SYSTEM_WITHOUT_SKILL
        )
        full_prompt = _PROMPT_TEMPLATE.format(system=system, prompt=prompt)

        started = time.monotonic()
        try:
            result = self.client.complete(full_prompt)
        except Exception as exc:
            logger.warning("LLM call failed (%s) — marking arm errored", exc)
            return ArmExecution(
                errored=True,
                error=str(exc)[:300],
                duration_ms=int((time.monotonic() - started) * 1000),
                extra={"model": self.client.model},
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        text = (result.text or "").strip()
        # Fall back to the spec-era estimate only when the provider sent no
        # usage block — real counts are strictly better for token deltas.
        tokens = result.total_tokens or (len(full_prompt) + len(text)) // 4
        return ArmExecution(
            final_output=text,
            duration_ms=duration_ms,
            turns=1,
            total_tokens=tokens,
            tool_call_count=0,
            extra={"model": self.client.model},
        )

    def complete_turn(
        self, *, system: str | None, prompt: str, role: str = "agent"
    ) -> tuple[str, int]:
        """One completion for a multi-turn rollout. The client takes a single prompt, so fold the
        system in (mirrors ``run``). Returns (text, total_tokens).

        ``role`` is one of ``"agent"`` / ``"simulate"`` / ``"label"`` / ``"trigger"``. The
        honesty-critical event ``label`` is pinned to temperature 0 so the deterministic
        state-machine grader's input is reproducible (a sampled labeler can verdict-flip the same
        trajectory); agent replies use the client's configured temperature so repeat runs
        (--runs) can genuinely diverge.
        """
        folded = f"{system}\n\n{prompt}" if system else prompt
        temperature = 0.0 if role == "label" else None
        result = self.client.complete(folded, temperature=temperature)
        text = (result.text or "").strip()
        return text, int(result.total_tokens or (len(folded) + len(text)) // 4)


def make_judge_call(
    model: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> JudgeCall:
    """Build a :data:`JudgeCall` backed by :class:`LLMClient`.

    Temperature is pinned to 0 per the spec's determinism recommendation.
    The model's raw text goes through :func:`parse_judge_json`, so fenced
    or preambled JSON still parses.
    """
    client = LLMClient(model, temperature=0.0, timeout_s=timeout_s)
    client.validate()

    def judge_call(rendered_prompt: str) -> dict[str, Any]:
        completion = client.complete(rendered_prompt)
        return parse_judge_json(completion.text)

    return judge_call
