"""Adapters — provider routing, env keys, prompt construction, error paths."""

from __future__ import annotations

import json

import pytest

from skillevaluation.runner.adapters.base import AdapterError, ArmExecution
from skillevaluation.runner.adapters.claude_code import ClaudeCodeAdapter
from skillevaluation.runner.adapters.llm import (
    CompletionResult,
    LLMAdapter,
    LLMClient,
    _provider_for,
    make_judge_call,
)
from skillevaluation.runner.adapters.mock import MockAdapter, mock_judge_call

# ── ArmExecution serialization (cache round-trip) ────────────────────


def test_arm_execution_payload_round_trip():
    arm = ArmExecution(
        final_output="out", duration_ms=5, turns=2, total_tokens=10,
        tool_call_count=1, errored=False, extra={"model": "m"},
    )
    again = ArmExecution.from_payload(arm.to_payload())
    assert again == arm


# ── provider routing ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model,provider",
    [
        ("claude-haiku-4-5", "anthropic"),
        ("claude-opus-4-8", "anthropic"),
        ("gpt-5.2", "openai"),
        ("o3-mini", "openai"),
        ("gemini-3.5-flash", "gemini"),
    ],
)
def test_provider_inference(model, provider):
    assert _provider_for(model) == provider


def test_unknown_model_prefix_raises_adapter_error():
    with pytest.raises(AdapterError, match="cannot infer provider"):
        _provider_for("mistral-large")


def test_missing_api_key_raises_with_var_name(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = LLMClient("claude-haiku-4-5")
    with pytest.raises(AdapterError, match="ANTHROPIC_API_KEY"):
        client.validate()


def test_gemini_accepts_either_env_var(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    LLMClient("gemini-3.5-flash").validate()


# ── LLMAdapter behavior (transport stubbed) ──────────────────────────


def _patched_adapter(monkeypatch, *, text="answer", tokens=33, raise_exc=None):
    adapter = LLMAdapter("claude-haiku-4-5")
    captured = {}

    def fake_complete(prompt):
        captured["prompt"] = prompt
        if raise_exc:
            raise raise_exc
        return CompletionResult(text=text, total_tokens=tokens)

    monkeypatch.setattr(adapter.client, "complete", fake_complete)
    return adapter, captured


def test_with_skill_arm_injects_skill_body(monkeypatch, tmp_path):
    adapter, captured = _patched_adapter(monkeypatch)
    arm = adapter.run(prompt="do the task", skill_body="# My Skill\nAlways do X.",
                      workspace=tmp_path)
    assert "Follow this skill" in captured["prompt"]
    assert "Always do X." in captured["prompt"]
    assert "User: do the task" in captured["prompt"]
    assert arm.final_output == "answer"
    assert arm.total_tokens == 33
    assert arm.turns == 1 and arm.tool_call_count == 0


def test_without_skill_arm_has_bare_system(monkeypatch, tmp_path):
    adapter, captured = _patched_adapter(monkeypatch)
    adapter.run(prompt="do the task", skill_body=None, workspace=tmp_path)
    assert "Follow this skill" not in captured["prompt"]
    assert captured["prompt"].startswith("You are an agent.")


def test_token_estimate_fallback_when_no_usage(monkeypatch, tmp_path):
    adapter, captured = _patched_adapter(monkeypatch, text="four char", tokens=0)
    arm = adapter.run(prompt="p", skill_body=None, workspace=tmp_path)
    assert arm.total_tokens == (len(captured["prompt"]) + len("four char")) // 4


def test_transport_failure_marks_arm_errored_not_raise(monkeypatch, tmp_path):
    adapter, _ = _patched_adapter(monkeypatch, raise_exc=RuntimeError("503 upstream"))
    arm = adapter.run(prompt="p", skill_body=None, workspace=tmp_path)
    assert arm.errored is True
    assert "503 upstream" in arm.error
    assert arm.final_output == ""


def test_adapter_identity_scopes_by_model():
    assert LLMAdapter("claude-haiku-4-5").identity == "llm:claude-haiku-4-5"


def test_make_judge_call_parses_fenced_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    calls = {}

    def fake_complete(self, prompt):
        calls["prompt"] = prompt
        return CompletionResult(text='```json\n{"passed": true, "reason": "ok"}\n```',
                                total_tokens=5)

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    judge = make_judge_call("claude-haiku-4-5")
    assert judge("rendered prompt")["passed"] is True
    assert calls["prompt"] == "rendered prompt"


# ── retry/backoff on transient provider failures ─────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )


def _gemini_ok_body(text="answer"):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"totalTokenCount": 7},
    }


def test_transient_503_retried_then_succeeds(monkeypatch):
    """Free-tier Gemini 503s on bursts; the client must back off and retry
    instead of failing the arm on the first 503."""
    import httpx

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    responses = iter(
        [_FakeResponse(503), _FakeResponse(503), _FakeResponse(200, _gemini_ok_body())]
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: next(responses))

    result = LLMClient("gemini-3.5-flash").complete("p")
    assert result.text == "answer"
    assert sleeps == [2.0, 4.0]  # exponential backoff between attempts


def test_retry_honors_retry_after_header(monkeypatch):
    import httpx

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    responses = iter(
        [
            _FakeResponse(429, headers={"retry-after": "11"}),
            _FakeResponse(200, _gemini_ok_body()),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: next(responses))

    LLMClient("gemini-3.5-flash").complete("p")
    assert sleeps == [11.0]  # provider's wait wins over the 2s base


def test_non_transient_error_raises_immediately_with_body(monkeypatch):
    """401s (bad key) must NOT burn retry sleeps — and the provider's
    error body (the actionable part) must ride in the message."""
    import httpx

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    resp = _FakeResponse(401)
    resp.text = '{"error": {"message": "credit balance is too low"}}'
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: resp)

    with pytest.raises(httpx.HTTPStatusError, match="credit balance is too low"):
        LLMClient("gemini-3.5-flash").complete("p")
    assert sleeps == []


def test_openai_reasoning_models_omit_temperature(monkeypatch):
    """gpt-5*/o-series reject the temperature param (400); the adapter
    must not send it. Older chat models still get it."""
    import httpx

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    payloads = []

    def fake_post(url, **kw):
        payloads.append(kw["json"])
        return _FakeResponse(200, {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 3},
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    LLMClient("gpt-5-mini").complete("p")
    LLMClient("o3-mini").complete("p")
    LLMClient("gpt-4o-mini").complete("p")
    assert "temperature" not in payloads[0]
    assert "temperature" not in payloads[1]
    assert payloads[2]["temperature"] == 0.0


def test_exhausted_retries_raise_last_error(monkeypatch):
    import httpx

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResponse(503))

    with pytest.raises(httpx.HTTPStatusError):
        LLMClient("gemini-3.5-flash", max_attempts=3).complete("p")


# ── MockAdapter + mock judge ─────────────────────────────────────────


def test_mock_adapter_default_responses_are_deterministic(tmp_path):
    adapter = MockAdapter()
    a = adapter.run(prompt="p1", skill_body="body", workspace=tmp_path)
    b = adapter.run(prompt="p1", skill_body="body", workspace=tmp_path)
    assert a.final_output == b.final_output
    assert "[mock with-skill response]" in a.final_output


def test_mock_adapter_canned_table_and_metrics(tmp_path):
    adapter = MockAdapter(
        {"q": {"with": "good answer", "without": "bad answer"}},
        with_metrics={"total_tokens": 100},
        without_metrics={"total_tokens": 300},
    )
    w = adapter.run(prompt="q", skill_body="s", workspace=tmp_path)
    wo = adapter.run(prompt="q", skill_body=None, workspace=tmp_path)
    assert (w.final_output, wo.final_output) == ("good answer", "bad answer")
    assert (w.total_tokens, wo.total_tokens) == (100, 300)
    assert len(adapter.calls) == 2
    assert adapter.calls[0]["with_skill"] is True


def test_mock_adapter_response_fn(tmp_path):
    adapter = MockAdapter(lambda prompt, with_skill: f"{prompt}|{with_skill}")
    arm = adapter.run(prompt="x", skill_body=None, workspace=tmp_path)
    assert arm.final_output == "x|False"


def test_mock_judge_passes_on_verbatim_expectation():
    from skillevaluation.runner.judge import judge_expectations

    results = judge_expectations(
        ["mentions the magic word"],
        final_output="this response mentions the magic word proudly",
        judge_call=mock_judge_call,
    )
    assert results[0]["passed"] is True
    miss = judge_expectations(
        ["mentions the magic word"], final_output="nothing relevant",
        judge_call=mock_judge_call,
    )
    assert miss[0]["passed"] is False


# ── ClaudeCodeAdapter (subprocess stubbed) ───────────────────────────


def _fake_proc(stdout: bytes, returncode: int = 0, stderr: bytes = b""):
    class P:
        pass

    p = P()
    p.stdout, p.stderr, p.returncode = stdout, stderr, returncode
    return p


def test_claude_code_validate_requires_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: None)
    with pytest.raises(AdapterError, match="not found on PATH"):
        ClaudeCodeAdapter().validate()


def test_claude_code_stages_skill_with_frontmatter(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["cwd"] = cmd, kwargs["cwd"]
        return _fake_proc(json.dumps({"result": "done", "num_turns": 3}).encode())

    monkeypatch.setattr("subprocess.run", fake_run)
    adapter = ClaudeCodeAdapter(skill_name="my-skill", model="haiku")
    arm = adapter.run(prompt="task", skill_body="# Title\nDo X.", workspace=tmp_path)

    staged = tmp_path / ".claude" / "skills" / "my-skill" / "SKILL.md"
    text = staged.read_text()
    assert text.startswith("---\nname: my-skill\n")
    assert "Do X." in text
    assert seen["cmd"][:2] == ["claude", "-p"]
    assert "--model" in seen["cmd"] and "haiku" in seen["cmd"]
    assert seen["cwd"] == str(tmp_path)
    assert arm.final_output == "done"
    assert arm.turns == 3


def test_claude_code_existing_frontmatter_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "subprocess.run", lambda cmd, **kw: _fake_proc(b'{"result": "ok"}')
    )
    adapter = ClaudeCodeAdapter(skill_name="s")
    adapter.run(prompt="t", skill_body="---\nname: custom\n---\nbody",
                workspace=tmp_path)
    staged = (tmp_path / ".claude" / "skills" / "s" / "SKILL.md").read_text()
    assert staged.startswith("---\nname: custom")
    assert staged.count("---\nname:") == 1


def test_claude_code_without_arm_unstages_skill(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "subprocess.run", lambda cmd, **kw: _fake_proc(b'{"result": "ok"}')
    )
    adapter = ClaudeCodeAdapter(skill_name="s")
    adapter.run(prompt="t", skill_body="body", workspace=tmp_path)
    assert (tmp_path / ".claude").exists()
    adapter.run(prompt="t", skill_body=None, workspace=tmp_path)
    assert not (tmp_path / ".claude").exists()


def test_claude_code_usage_and_cost_parsed(monkeypatch, tmp_path):
    payload = {
        "result": "answer",
        "num_turns": 5,
        "duration_ms": 7000,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "total_cost_usd": 0.012,
    }
    monkeypatch.setattr(
        "subprocess.run", lambda cmd, **kw: _fake_proc(json.dumps(payload).encode())
    )
    arm = ClaudeCodeAdapter().run(prompt="t", skill_body=None, workspace=tmp_path)
    assert arm.total_tokens == 150
    assert arm.duration_ms == 7000
    assert arm.extra["total_cost_usd"] == 0.012


def test_claude_code_nonzero_exit_is_errored_arm(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: _fake_proc(b"", returncode=2, stderr=b"auth failure"),
    )
    arm = ClaudeCodeAdapter().run(prompt="t", skill_body=None, workspace=tmp_path)
    assert arm.errored and "auth failure" in arm.error


def test_claude_code_non_json_stdout_degrades_to_raw_text(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "subprocess.run", lambda cmd, **kw: _fake_proc(b"plain text answer")
    )
    arm = ClaudeCodeAdapter().run(prompt="t", skill_body=None, workspace=tmp_path)
    assert not arm.errored
    assert arm.final_output == "plain text answer"
    assert arm.extra.get("raw_output") is True


# ── key hygiene: the Gemini key must never ride in the URL ───────────


def test_gemini_key_sent_as_header_not_query_param(monkeypatch):
    """httpx error strings embed the full URL, and those strings flow into
    results.json (and any --export-url upload) — a ?key= param would leak."""
    import httpx

    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["kwargs"] = kw
        return _FakeResponse(200, _gemini_ok_body())

    monkeypatch.setattr(httpx, "post", fake_post)
    LLMClient("gemini-3.5-flash").complete("p")

    assert "SECRET-KEY-123" not in seen["url"]
    assert "params" not in seen["kwargs"]
    assert seen["kwargs"]["headers"]["x-goog-api-key"] == "SECRET-KEY-123"


def test_gemini_http_error_message_has_no_key(monkeypatch):
    """The non-retryable error path re-raises with the URL in the message;
    the key must not be part of it."""
    import httpx
    import pytest

    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: _FakeResponse(400, {"error": "bad request"})
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        LLMClient("gemini-3.5-flash").complete("p")
    assert "SECRET-KEY-123" not in str(exc_info.value)
