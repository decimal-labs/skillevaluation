"""validators.run_validators — staging, exit codes, timeouts, result shape."""

from __future__ import annotations

from skillevaluation.runner.validators import run_validators
from skillevaluation.runner.workspace import prepare_workspace


def test_empty_validator_list_returns_empty():
    ws = prepare_workspace([])
    assert run_validators([], ws, response_text="x") == []


def test_response_staged_as_env_var_and_file():
    ws = prepare_workspace([])
    results = run_validators(
        [
            {"cmd": 'echo "$RESPONSE_TEXT" | grep -q SELECT', "expect_exit_code": 0,
             "label": "env carries response"},
            {"cmd": "grep -q SELECT response.txt", "expect_exit_code": 0,
             "label": "response.txt staged"},
        ],
        ws,
        response_text="SELECT 1;",
    )
    assert all(r["passed"] for r in results), results


def test_pass_iff_exit_code_matches_expectation():
    ws = prepare_workspace([])
    results = run_validators(
        [
            {"cmd": "true", "expect_exit_code": 0, "label": "ok"},
            {"cmd": "false", "expect_exit_code": 0, "label": "fails"},
            {"cmd": "exit 3", "expect_exit_code": 3, "label": "wants 3"},
        ],
        ws,
    )
    assert [r["passed"] for r in results] == [True, False, True]
    assert results[1]["exit_code"] == 1
    assert results[2]["expect_exit_code"] == 3


def test_nonbinary_exit_code_is_errored_not_fail():
    """A grader is a binary verdict: 0=pass, 1=fail. Any OTHER exit code the
    author did not declare (exit 2 = bad spec / missing case / unknown mode) is
    an AUTHOR error, not a model fail — it must be flagged errored so the caller
    EXCLUDES it from lift instead of scoring a model fail. A broken grader must
    never masquerade as honest no-lift."""
    ws = prepare_workspace([])
    results = run_validators(
        [
            {"cmd": "true", "expect_exit_code": 0, "label": "clean pass"},
            {"cmd": "false", "expect_exit_code": 0, "label": "clean fail"},
            {"cmd": "exit 2", "expect_exit_code": 0, "label": "author error"},
        ],
        ws,
    )
    by = {r["label"]: r for r in results}
    # 0 and 1 keep IDENTICAL pass/fail behavior, and are NOT errored.
    assert by["clean pass"]["passed"] is True
    assert by["clean pass"]["errored"] is False
    assert by["clean fail"]["passed"] is False
    assert by["clean fail"]["exit_code"] == 1
    assert by["clean fail"]["errored"] is False
    # exit 2 → errored (excluded from lift), NOT counted as a model fail.
    assert by["author error"]["exit_code"] == 2
    assert by["author error"]["errored"] is True
    assert by["author error"]["passed"] is False


def test_author_declared_nonbinary_exit_is_clean_not_errored():
    """When the author explicitly opts into a non-binary code via
    expect_exit_code (e.g. 3), that exact code is a clean pass — NOT errored.
    Preserves the existing expect_exit_code contract."""
    ws = prepare_workspace([])
    results = run_validators(
        [{"cmd": "exit 3", "expect_exit_code": 3, "label": "wants 3"}], ws
    )
    assert results[0]["passed"] is True
    assert results[0]["errored"] is False


def test_sandbox_clamps_cpu_rlimit():
    """Validators run under a clamped CPU limit (proves the preexec sandbox ran).

    Without the resource-limit preexec, `ulimit -t` reports 'unlimited'; with it, the
    child sees the finite cap.
    """
    ws = prepare_workspace([])
    results = run_validators(
        [{"cmd": "ulimit -t", "expect_exit_code": 0, "label": "cpu rlimit clamped"}],
        ws,
    )
    out = results[0]["stdout"].strip()
    assert out and out != "unlimited", results
    assert int(out) <= 60, results


def test_runs_with_cwd_workspace():
    ws = prepare_workspace([], files={"present.txt": "here"})
    results = run_validators(
        [{"cmd": "test -f present.txt", "expect_exit_code": 0, "label": "cwd"}], ws
    )
    assert results[0]["passed"]


def test_captures_stdout_and_stderr_truncated():
    ws = prepare_workspace([])
    results = run_validators(
        [{"cmd": "python3 -c \"import sys; print('o'*1000); print('e'*1000, file=sys.stderr)\"",
          "expect_exit_code": 0, "label": "noisy"}],
        ws,
    )
    assert len(results[0]["stdout"]) == 500
    assert len(results[0]["stderr"]) == 500


def test_timeout_is_ungraded_not_a_model_fail():
    """0.7.0: a grader that never returns a verdict is UNGRADED, not a fail.

    Timeouts are measured per arm, so scoring one as a model failure let a grader that
    is merely slower on one arm's longer output manufacture a flip. It also matches the
    RLIMIT_CPU kill, which already errored via its negative returncode.
    """
    ws = prepare_workspace([])
    results = run_validators(
        [{"cmd": "sleep 5", "expect_exit_code": 0, "label": "slow"}], ws, timeout_s=1
    )
    assert results[0]["passed"] is False
    assert results[0]["exit_code"] == -1
    assert "timed out" in results[0]["stderr"]
    assert results[0]["errored"] is True


def test_result_dicts_match_schema_required_fields():
    """validatorResult in test-case-result.schema.json requires label+passed."""
    ws = prepare_workspace([])
    results = run_validators(
        [{"cmd": "true", "expect_exit_code": 0, "label": "shape"}], ws
    )
    r = results[0]
    for key in ("label", "passed", "cmd", "exit_code", "expect_exit_code", "stdout", "stderr"):
        assert key in r


def test_env_response_truncated_but_file_carries_full_text():
    """RESPONSE_TEXT has a hard OS ceiling per env entry; response.txt must
    always carry the full output."""
    from skillevaluation.runner.validators import ENV_RESPONSE_MAX_CHARS

    big = "x" * (ENV_RESPONSE_MAX_CHARS + 10_000)
    ws = prepare_workspace([])
    results = run_validators(
        [
            {"cmd": f'[ "${{#RESPONSE_TEXT}}" -le {ENV_RESPONSE_MAX_CHARS} ]',
             "expect_exit_code": 0, "label": "env capped"},
            {"cmd": f'[ "$(wc -c < response.txt)" -ge {len(big)} ]',
             "expect_exit_code": 0, "label": "file full"},
        ],
        ws,
        response_text=big,
    )
    assert [r["passed"] for r in results] == [True, True], results


def test_env_allowlist_scrubs_platform_secrets(monkeypatch):
    """The runner grades UNTRUSTED, author-controlled shell server-side.

    Platform secrets in the parent environment (DB creds, provider API keys) must
    NOT reach the validator child — passing os.environ would expose them for
    exfiltration. Assert the secrets are scrubbed (the child sees empty values and
    the secret value never appears in captured output) AND that an allowlisted var
    the runner needs (PATH) DOES survive — so emptying the whole env instead of
    scrubbing selectively also fails this test.
    """
    secret = "sk-leak-CANARY-1234567890"
    db_url = "postgresql://decimal:CANARY-DBPASS@host:5432/prod"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("DECIMAL_DATABASE_URL", db_url)

    ws = prepare_workspace([])
    results = run_validators(
        [
            # A scrubbed var expands to empty, so `-z` (empty string) is true.
            {"cmd": '[ -z "$GEMINI_API_KEY" ]', "expect_exit_code": 0, "label": "gemini scrubbed"},
            {"cmd": '[ -z "$OPENAI_API_KEY" ]', "expect_exit_code": 0, "label": "openai scrubbed"},
            {"cmd": '[ -z "$DECIMAL_DATABASE_URL" ]', "expect_exit_code": 0,
             "label": "db url scrubbed"},
            # Allowlisted var the runner needs must survive selective scrubbing.
            {"cmd": '[ -n "$PATH" ]', "expect_exit_code": 0, "label": "path survives"},
            # Echo every env var so we can grep the captured output for the secret.
            {"cmd": "env", "expect_exit_code": 0, "label": "dump env"},
        ],
        ws,
    )

    by_label = {r["label"]: r for r in results}
    assert by_label["gemini scrubbed"]["passed"], results
    assert by_label["openai scrubbed"]["passed"], results
    assert by_label["db url scrubbed"]["passed"], results
    assert by_label["path survives"]["passed"], results

    # The secret VALUE must not leak into any captured output, even partially.
    for r in results:
        assert secret not in r["stdout"], (r["label"], r["stdout"])
        assert secret not in r["stderr"], (r["label"], r["stderr"])
        assert "CANARY-DBPASS" not in r["stdout"], (r["label"], r["stdout"])
        assert "CANARY-DBPASS" not in r["stderr"], (r["label"], r["stderr"])


def test_spawn_failure_fails_validator_not_suite():
    """A command the OS refuses to spawn (NUL byte) must fail that one
    validator — the arms already executed and cost tokens."""
    ws = prepare_workspace([])
    results = run_validators(
        [
            {"cmd": "echo \x00bad", "expect_exit_code": 0, "label": "unspawnable"},
            {"cmd": "true", "expect_exit_code": 0, "label": "still runs"},
        ],
        ws,
    )
    assert results[0]["passed"] is False
    assert "spawn" in results[0]["stderr"]
    # UNGRADED (0.7.0): the grader never ran, so it returned no verdict about the model.
    assert results[0]["errored"] is True
    assert results[1]["passed"] is True
    assert results[1]["errored"] is False
