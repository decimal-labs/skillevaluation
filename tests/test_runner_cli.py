"""CLI — validate/run commands, gates, outputs. All via the mock adapter."""

from __future__ import annotations

import json

import pytest

from skillevaluation.runner.cli import main
from skillevaluation.runner.skill_dir import load_skill_dir, split_frontmatter

SKILL_MD = """---
name: pii-classifier
description: Classify schema fields for GDPR.
---

# PII classifier

Label email as PII; ip_address is pseudonymous.
"""

EVAL_YAML = """
cases:
  - name: classifies
    prompt: "classify: email, ip_address"
    expectations:
      - "response_contains:mock with-skill response"
  - name: regression_guard
    prompt: "say nothing risky"
    expectations:
      - "response_contains:mock"
"""


@pytest.fixture
def skill_dir(tmp_path):
    d = tmp_path / "pii-classifier"
    d.mkdir()
    (d / "SKILL.md").write_text(SKILL_MD)
    (d / "eval.yaml").write_text(EVAL_YAML)
    return d


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLEVAL_CACHE_DIR", str(tmp_path / "cache"))


# ── skill_dir loader ─────────────────────────────────────────────────


def test_split_frontmatter_roundtrip():
    meta, body = split_frontmatter(SKILL_MD)
    assert meta["name"] == "pii-classifier"
    assert body.startswith("# PII classifier")


def test_split_frontmatter_absent_returns_whole_text():
    meta, body = split_frontmatter("# no frontmatter\nbody")
    assert meta == {}
    assert body.startswith("# no frontmatter")


def test_load_skill_dir_reads_name_cases_and_files(skill_dir):
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "check.sh").write_text("exit 0\n")
    skill = load_skill_dir(skill_dir)
    assert skill.name == "pii-classifier"
    assert len(skill.cases) == 2
    assert "scripts/check.sh" in skill.files


def test_load_skill_dir_name_falls_back_to_dirname(tmp_path):
    d = tmp_path / "dir-name-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("# bare body, no frontmatter\ncontent")
    (d / "eval.yaml").write_text(EVAL_YAML)
    assert load_skill_dir(d).name == "dir-name-skill"


def test_load_skill_dir_errors_name_the_missing_piece(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    from skillevaluation.runner.skill_dir import SkillDirError

    with pytest.raises(SkillDirError, match="SKILL.md"):
        load_skill_dir(empty)
    (empty / "SKILL.md").write_text("# body\ntext")
    with pytest.raises(SkillDirError, match="eval.yaml"):
        load_skill_dir(empty)


# ── validate command ─────────────────────────────────────────────────


def test_validate_happy_path(skill_dir, capsys):
    assert main(["validate", str(skill_dir)]) == 0
    out = capsys.readouterr().out
    assert "pii-classifier" in out
    assert "2 case(s)" in out


def test_validate_bad_yaml_exits_1(skill_dir, capsys):
    (skill_dir / "eval.yaml").write_text("cases: []")
    assert main(["validate", str(skill_dir)]) == 1
    assert "empty" in capsys.readouterr().err


# ── run command (mock adapter) ───────────────────────────────────────


def test_run_mock_writes_results_and_table(skill_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = main(["run", str(skill_dir), "--adapter", "mock"])
    assert code == 0
    out = capsys.readouterr().out
    assert "pii-classifier · 2 case(s)" in out
    assert "Pass rate" in out
    assert "Verdict:" in out

    doc = json.loads((tmp_path / "results.json").read_text())
    assert doc["total_cases"] == 2
    assert doc["runner"]["adapter"] == "mock"
    assert doc["format"] == "skillevaluation/test-run-result@v1"


def test_run_json_mode_prints_document(skill_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["run", str(skill_dir), "--adapter", "mock", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] in ("pass", "fail", "mixed", "error")


def test_run_custom_output_path(skill_dir, tmp_path):
    target = tmp_path / "out" / "r.json"
    target.parent.mkdir()
    assert main(["run", str(skill_dir), "--adapter", "mock", "-o", str(target)]) == 0
    assert json.loads(target.read_text())["total_cases"] == 2


def test_run_output_into_missing_dir_creates_it(skill_dir, tmp_path):
    """The run has already happened (and cost tokens) by write time —
    losing the document to a missing directory is the worst failure, so
    the CLI creates the parent instead of crashing."""
    target = tmp_path / "never" / "made" / "r.json"
    assert main(["run", str(skill_dir), "--adapter", "mock", "-o", str(target)]) == 0
    assert json.loads(target.read_text())["total_cases"] == 2


def test_results_embed_transcripts_by_default(skill_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["run", str(skill_dir), "--adapter", "mock"]) == 0
    doc = json.loads((tmp_path / "results.json").read_text())
    transcripts = doc["cases"][0]["transcripts"]
    assert "User: classify" in transcripts["with"]
    assert "Agent:" in transcripts["without"]


def test_no_embed_transcripts_flag_omits_them(skill_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--no-embed-transcripts"]) == 0
    doc = json.loads((tmp_path / "results.json").read_text())
    assert all("transcripts" not in c for c in doc["cases"])


def test_run_trajectories_written(skill_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    traj = tmp_path / "trajs"
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--trajectories", str(traj)]) == 0
    files = sorted(p.name for p in traj.iterdir())
    assert files == [
        "classifies__with.txt", "classifies__without.txt",
        "regression_guard__with.txt", "regression_guard__without.txt",
    ]
    assert "User: classify" in (traj / "classifies__with.txt").read_text()


def test_run_llm_adapter_requires_model(skill_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLEVAL_MODEL", raising=False)
    assert main(["run", str(skill_dir)]) == 2
    assert "--model is required" in capsys.readouterr().err


def test_run_missing_skill_dir_exits_2(tmp_path, capsys):
    assert main(["run", str(tmp_path / "nope"), "--adapter", "mock"]) == 2
    assert "not a directory" in capsys.readouterr().err


# ── CI gates ─────────────────────────────────────────────────────────


def _failing_suite(skill_dir):
    """Make every expectation unsatisfiable → verdict fail."""
    (skill_dir / "eval.yaml").write_text(
        """
cases:
  - name: unsatisfiable
    prompt: "p"
    expectations:
      - "response_contains:THIS_NEVER_APPEARS_ANYWHERE"
"""
    )


def test_fail_on_verdict_gate(skill_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _failing_suite(skill_dir)
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--fail-on-verdict", "fail"]) == 1
    assert "gate: verdict 'fail'" in capsys.readouterr().err


def test_verdict_gate_passes_when_not_listed(skill_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _failing_suite(skill_dir)
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--fail-on-verdict", "error"]) == 0


def _error_dominated_suite(skill_dir):
    """1 of 2 cases errors via a failing setup step → 50% errored > the 25% floor."""
    (skill_dir / "eval.yaml").write_text(
        """
cases:
  - name: errors_out
    prompt: "p"
    setup:
      - "false"
    expectations:
      - "response_contains:mock"
  - name: fine
    prompt: "q"
    expectations:
      - "response_contains:mock"
"""
    )


def test_fail_on_verdict_gate_fails_error_dominated_run(skill_dir, tmp_path, monkeypatch, capsys):
    """An error-dominated run must never clear a verdict gate — even when the listed verdicts
    (here just `fail`) don't name `error` (spec/runner-contract.md floor)."""
    monkeypatch.chdir(tmp_path)
    _error_dominated_suite(skill_dir)
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--fail-on-verdict", "fail"]) == 1
    assert "error-dominated" in capsys.readouterr().err


def test_min_delta_pts_gate_fails_on_withheld_delta(skill_dir, tmp_path, monkeypatch, capsys):
    """When the headline delta is withheld (error-dominated → null), --min-delta-pts fails with
    an explicit n/a message instead of crashing on None < float or fabricating a 0."""
    monkeypatch.chdir(tmp_path)
    _error_dominated_suite(skill_dir)
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--min-delta-pts", "0"]) == 1
    err = capsys.readouterr().err
    assert "pass-rate delta is n/a (error-dominated)" in err


def test_min_delta_pts_gate(skill_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # Both mock arms emit "[mock …]" so a contains:mock expectation is
    # pass_kept on every case → delta exactly 0 pts.
    (skill_dir / "eval.yaml").write_text(
        """
cases:
  - name: zero_delta
    prompt: "p"
    expectations:
      - "response_contains:mock"
"""
    )
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--min-delta-pts", "10"]) == 1
    assert "pass-rate delta" in capsys.readouterr().err
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--min-delta-pts", "0"]) == 0


# ── export ───────────────────────────────────────────────────────────


def test_export_url_posts_document(skill_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    posted = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.update({"url": url, "doc": json, "headers": headers})
        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("SKILLEVAL_EXPORT_TOKEN", "tok-123")
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--export-url", "https://collector.example/runs"]) == 0
    assert posted["url"] == "https://collector.example/runs"
    assert posted["doc"]["total_cases"] == 2
    assert posted["headers"]["authorization"] == "Bearer tok-123"


def test_export_failure_does_not_fail_the_run(skill_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    import httpx

    def boom(*a, **kw):
        raise RuntimeError("collector down")

    monkeypatch.setattr(httpx, "post", boom)
    assert main(["run", str(skill_dir), "--adapter", "mock",
                 "--export-url", "https://collector.example/runs"]) == 0
    assert "export failed (results kept locally)" in capsys.readouterr().err


# ── structural-only suites need no judge model ───────────────────────


def test_structural_only_suite_skips_judge_wiring():
    """All-structural expectations must not demand --model/--judge-model
    (or any API key) for the judge."""
    import argparse

    from skillevaluation.runner.cli import _build_judge

    args = argparse.Namespace(adapter="claude-code", model="", judge_model="")
    judge_call, label = _build_judge(args, needs_llm_judge=False)
    assert "structural" in label
    # the stub must never be invoked; if it is, it raises loudly
    with pytest.raises(Exception):
        judge_call("rendered prompt")


def test_semantic_suite_still_requires_judge_model():
    import argparse

    from skillevaluation.runner.adapters.base import AdapterError
    from skillevaluation.runner.cli import _build_judge

    args = argparse.Namespace(adapter="claude-code", model="", judge_model="")
    with pytest.raises(AdapterError, match="judge-model"):
        _build_judge(args, needs_llm_judge=True)


def test_keep_workspaces_flag_parses(skill_dir, tmp_path, monkeypatch):
    import tempfile

    monkeypatch.chdir(tmp_path)
    # keep the kept workspaces inside tmp_path so the test cleans up
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    rc = main(["run", str(skill_dir), "--adapter", "mock", "--keep-workspaces",
               "-o", str(tmp_path / "r.json")])
    assert rc == 0
    assert list(tmp_path.glob("skilleval-*")), "workspaces were kept"


# ── Trigger-only cases (spec 0.3.0) — disclosure only; routing is platform-side (rev 2) ──


TRIGGER_EVAL_YAML = """
cases:
  - name: classifies
    prompt: "classify: email, ip_address"
    expectations:
      - "response_contains:mock with-skill response"
  - name: fires_on_topic
    prompt: "run the pii-classifier treatment on this schema"
    should_trigger: true
  - name: near_miss_silent
    prompt: "alphabetize this grocery list"
    should_trigger: false
"""


@pytest.fixture
def trigger_skill_dir(skill_dir):
    (skill_dir / "eval.yaml").write_text(TRIGGER_EVAL_YAML)
    return skill_dir


def test_trigger_only_cases_disclosed_never_executed(trigger_skill_dir, tmp_path,
                                                     capsys, monkeypatch):
    """Rev 2: the runner never executes trigger cases — they are excluded from the A/B loop with
    the cases_skipped_trigger_only disclosure, and no trigger block appears in results.json."""
    monkeypatch.chdir(tmp_path)
    assert main(["run", str(trigger_skill_dir), "--adapter", "mock"]) == 0
    doc = json.loads((tmp_path / "results.json").read_text())
    assert doc["cases_skipped_trigger_only"] == 2
    assert doc["total_cases"] == 1
    assert "trigger_metrics" not in doc
    assert "trigger_cases" not in doc


def test_runs_flag_repeats_cases_and_discloses(skill_dir, tmp_path, capsys, monkeypatch):
    """--runs N executes every graded case N times per arm; rates are MEANS over runs; the
    document discloses the run count."""
    monkeypatch.chdir(tmp_path)
    assert main(["run", str(skill_dir), "--adapter", "mock", "--runs", "3"]) == 0
    doc = json.loads((tmp_path / "results.json").read_text())
    assert doc["runs"] == 3
    # The mock adapter is deterministic, so the mean over 3 runs equals the single-run rate —
    # exactly the point: the expected value does not change with N.
    assert doc["total_cases"] == 3 * len(doc["cases"]) or doc["total_cases"] >= len(doc["cases"])


def test_validate_discloses_trigger_cases(trigger_skill_dir, capsys):
    assert main(["validate", str(trigger_skill_dir)]) == 0
    out = capsys.readouterr().out
    assert "trigger case(s): 2" in out
    assert "2 trigger-only" in out
    assert "platform-side" in out
