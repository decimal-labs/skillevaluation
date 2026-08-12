"""workspace.prepare_workspace — staging, setup steps, strictness modes."""

from __future__ import annotations

import pytest

from skillevaluation.runner.workspace import SetupStepError, prepare_workspace


def test_creates_empty_workspace():
    ws = prepare_workspace([])
    assert ws.is_dir()
    assert list(ws.iterdir()) == []


def test_materializes_bundled_files_with_nested_paths():
    ws = prepare_workspace([], files={"scripts/check.py": "print('hi')", "data.txt": "x"})
    assert (ws / "scripts" / "check.py").read_text() == "print('hi')"
    assert (ws / "data.txt").read_text() == "x"


def test_setup_steps_run_inside_workspace():
    ws = prepare_workspace(["echo seeded > marker.txt"])
    assert (ws / "marker.txt").read_text().strip() == "seeded"


def test_setup_steps_run_sequentially_in_order():
    ws = prepare_workspace(["echo one > log.txt", "echo two >> log.txt"])
    assert (ws / "log.txt").read_text().splitlines() == ["one", "two"]


def test_files_staged_before_setup_steps():
    ws = prepare_workspace(["cat input.txt > copied.txt"], files={"input.txt": "payload"})
    assert (ws / "copied.txt").read_text() == "payload"


def test_case_setup_files_staged_before_commands():
    """setup.files mapping form (spec 0.3.0): case files land BEFORE any setup command runs —
    strict mode proves it, since `cat` would exit non-zero on a missing file."""
    ws = prepare_workspace(
        ["cat schema.json > copy.json"],
        case_files={"schema.json": '{"email": "string"}', "data/rows.csv": "a,b\n"},
        strict=True,
    )
    assert (ws / "copy.json").read_text() == '{"email": "string"}'
    assert (ws / "data" / "rows.csv").read_text() == "a,b\n"


def test_case_setup_files_win_over_bundled_on_collision():
    ws = prepare_workspace(
        [], files={"input.txt": "bundled"}, case_files={"input.txt": "case"}
    )
    assert (ws / "input.txt").read_text() == "case"


def test_case_setup_files_reject_path_escapes(tmp_path):
    """Same path-escape rejection as bundled skill files: absolute keys and `..` traversal are
    refused (skipped with a warning), never written outside the workspace."""
    outside = tmp_path / "escape.txt"
    ws = prepare_workspace(
        [],
        case_files={
            str(outside): "absolute escape",
            "../escape-relative.txt": "dotdot escape",
            "safe.txt": "stays",
        },
    )
    assert not outside.exists()
    assert not (ws.parent / "escape-relative.txt").exists()
    assert (ws / "safe.txt").read_text() == "stays"


def test_lenient_mode_ignores_failing_step():
    ws = prepare_workspace(["false", "echo survived > ok.txt"], strict=False)
    assert (ws / "ok.txt").exists()


def test_strict_mode_raises_on_nonzero_exit():
    with pytest.raises(SetupStepError, match="exit 1"):
        prepare_workspace(["false"], strict=True)


def test_strict_mode_raises_on_timeout():
    with pytest.raises(SetupStepError, match="timed out"):
        prepare_workspace(["sleep 5"], strict=True, timeout_s=1)


def test_strict_error_names_the_command():
    with pytest.raises(SetupStepError) as exc:
        prepare_workspace(["exit 3"], strict=True)
    assert "exit 3" in str(exc.value)
    assert exc.value.cmd == "exit 3"


def test_setup_step_env_allowlist_scrubs_platform_secrets(monkeypatch):
    """A case's UNTRUSTED `setup:` shell must not see platform
    secrets carried in the parent environment when the backend grades skills
    server-side. Run a setup step that writes every env var the child sees to a
    file, then assert the secret values are absent while an allowlisted var (PATH)
    survives — so emptying the whole env (not selective scrubbing) also fails here.
    """
    secret = "sk-leak-CANARY-1234567890"
    db_url = "postgresql://decimal:CANARY-DBPASS@host:5432/prod"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("DECIMAL_DATABASE_URL", db_url)

    ws = prepare_workspace(
        [
            "env > seen_env.txt",
            # A scrubbed secret expands to empty; an allowlisted var survives.
            'test -z "$GEMINI_API_KEY" && test -n "$PATH" && echo ok > scrub_ok.txt',
        ],
        strict=True,
    )

    dumped = (ws / "seen_env.txt").read_text()
    assert secret not in dumped, dumped
    assert "CANARY-DBPASS" not in dumped, dumped
    assert (ws / "scrub_ok.txt").read_text().strip() == "ok"
    assert "PATH=" in dumped, dumped
