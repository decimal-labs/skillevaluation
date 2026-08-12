"""The A/B execution loop — the heart of the reference runner.

Implements ``spec/runner-contract.md`` end to end:

1. per case, prepare one workspace PER ARM from the same setup steps
   (setup failure ⇒ outcome ``error``). Isolation matters: a
   file-writing agent (e.g. the claude-code adapter) must not leak
   artifacts — or readable solutions — from one arm into the other,
   and each arm's validators must grade only that arm's state.
2. execute the agent twice via the adapter (with skill / without skill,
   the without arm served from the baseline cache when possible)
3. grade BOTH arms against the same expectations + validators, each in
   its own workspace
4. classify the outcome (``classify_outcome``) and aggregate
   (``compute_run_aggregates`` / ``compute_verdict``)

One deliberate cost optimization over naive grading: an arm that
*errored* (transport failure, CLI crash) is recorded as failed without
spending judge calls on its empty output — the case outcome is ``error``
either way, and the spec excludes errored cases from aggregation.

Schema rev 2 (ADR-0007): ONE execution contract — every case is a single
agent invocation (which may take many tool steps) graded by expectations
+ validators. Repetition is the runner-level ``runs`` parameter (uniform
i.i.d. re-runs, MEAN-aggregated); the per-case ``trials`` field, the mode
dispatch, and menu-selection trigger execution are gone. Trigger-only cases are
still skipped with disclosure (``cases_skipped_trigger_only``) — routing
grading is a platform-side rail, not part of this runner.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillevaluation import __version__
from skillevaluation.aggregation import (
    CaseMetrics,
    CaseResult,
    RunAggregate,
    compute_run_aggregates,
)
from skillevaluation.baseline import baseline_cache_key
from skillevaluation.outcomes import Outcome, Verdict, classify_outcome, compute_verdict
from skillevaluation.parser import ParsedTestCase, is_trigger_only_case
from skillevaluation.trajectory.format_v1 import build_transcript_v1

from .adapters.base import AgentAdapter, ArmExecution
from .cache import BaselineCache
from .judge import JudgeCall, judge_expectations
from .validators import run_validators
from .workspace import SetupStepError, prepare_workspace

logger = logging.getLogger("skillevaluation.runner.orchestrator")

# Attempt heuristic, kept identical to the platform runner so both agree: an
# arm "attempted" the task when it produced at least this much non-whitespace
# output. Below the threshold the arm is treated as a no-op rather than a
# genuine failure, so an empty/refused response doesn't count as a real try.
TASK_ATTEMPTED_MIN_CHARS = 20

# Per-arm transcript budget when embedding into results.json — bounds the
# document size (200-case cap × 2 arms × 20KB ≈ 8MB worst case).
TRANSCRIPT_EMBED_MAX_CHARS = 20_000

# progress callback: (event, case_name, detail) — purely informational.
ProgressFn = Callable[[str, str, str], None]


@dataclass
class ArmRecord:
    """One arm's execution + grading, ready for serialization."""

    execution: ArmExecution
    passed: bool = False
    task_attempted: bool = True
    cached: bool = False
    transcript: str = ""

    def metrics(self) -> CaseMetrics:
        e = self.execution
        return CaseMetrics(
            passed=self.passed,
            duration_ms=e.duration_ms,
            turns=e.turns,
            total_tokens=e.total_tokens,
            tool_call_count=e.tool_call_count,
            task_attempted=self.task_attempted,
            errored=e.errored,
        )

    def to_dict(self) -> dict[str, Any]:
        """armMetrics shape from test-case-result.schema.json (+ extras)."""
        e = self.execution
        out: dict[str, Any] = {
            "passed": self.passed,
            "task_attempted": self.task_attempted,
            "errored": e.errored,
            "duration_ms": e.duration_ms,
            "turns": e.turns,
            "total_tokens": e.total_tokens,
            "tool_call_count": e.tool_call_count,
        }
        if self.cached:
            out["cached"] = True
        if e.error:
            out["error"] = e.error
        if e.extra:
            out["extra"] = e.extra
        return out


@dataclass
class CaseRunRecord:
    """One case's full A/B record."""

    case_name: str
    outcome: str
    with_arm: ArmRecord | None = None
    without_arm: ArmRecord | None = None
    expectation_results: list[dict[str, Any]] = field(default_factory=list)
    validator_results: list[dict[str, Any]] = field(default_factory=list)
    # The without-arm's per-check verdicts (same assertions, baseline output). The report only
    # shows the with-arm's, but the pre-graded bundle carries BOTH so a consumer can calibrate
    # judged expectations (discrimination = with vs without) without re-judging anything.
    without_expectation_results: list[dict[str, Any]] = field(default_factory=list)
    without_validator_results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self, *, include_transcripts: bool = False) -> dict[str, Any]:
        """test-case-result.schema.json shape.

        ``include_transcripts`` embeds each arm's canonical format-v1
        transcript (truncated to :data:`TRANSCRIPT_EMBED_MAX_CHARS`) so a
        consumer — e.g. a platform the document is pushed to — can render
        the sessions without a second upload.
        """
        out: dict[str, Any] = {"case_name": self.case_name, "outcome": self.outcome}
        if self.with_arm is not None:
            out["with_skill"] = self.with_arm.to_dict()
        if self.without_arm is not None:
            out["without_skill"] = self.without_arm.to_dict()
        out["expectation_results"] = self.expectation_results
        out["validator_results"] = self.validator_results
        if self.error:
            out["error"] = self.error
        if include_transcripts:
            transcripts = {
                label: arm.transcript[:TRANSCRIPT_EMBED_MAX_CHARS]
                for label, arm in (("with", self.with_arm), ("without", self.without_arm))
                if arm is not None and arm.transcript
            }
            if transcripts:
                out["transcripts"] = transcripts
        return out

    def to_aggregation_input(self) -> CaseResult:
        return CaseResult(
            case_name=self.case_name,
            outcome=self.outcome,
            with_skill=self.with_arm.metrics() if self.with_arm else None,
            without_skill=self.without_arm.metrics() if self.without_arm else None,
        )


@dataclass
class SuiteRunResult:
    """Everything one ``run_suite`` produced."""

    skill_name: str
    cases: list[CaseRunRecord]
    aggregate: RunAggregate
    verdict: str
    runner_info: dict[str, Any]
    started_at: str
    completed_at: str
    cache_hits: int = 0
    # Trigger-only cases (spec 0.3.0) the A/B loop excluded with disclosure — they belong to the
    # trigger rail (a platform-side contract, rev 2), so they never enter the lift aggregate or
    # its divisor.
    cases_skipped_trigger_only: int = 0
    # Uniform i.i.d. re-runs per case this suite executed (runner-level; MEAN-aggregated).
    runs: int = 1

    def to_results_json(self, *, include_transcripts: bool = False) -> dict[str, Any]:
        """The ``results.json`` document.

        Top level validates against ``schemas/test-run-result.schema.json``
        (whose required keys come from ``RunAggregate.to_dict()``); the
        extra keys ride on ``additionalProperties: true``. Each entry in
        ``cases`` validates against ``test-case-result.schema.json``.

        ``include_transcripts`` embeds each arm's canonical transcript per
        case (truncated) so a collector can render the sessions.
        """
        doc: dict[str, Any] = {
            **self.aggregate.to_dict(),
            "verdict": self.verdict,
            "format": "skillevaluation/test-run-result@v1",
            "skill": {"name": self.skill_name},
            "runner": self.runner_info,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cache_hits": self.cache_hits,
            "cases_skipped_trigger_only": self.cases_skipped_trigger_only,
        }
        if self.runs > 1:
            # Disclose the uniform re-run count so a reader knows the rates are means over
            # runs-per-case, not single-rollout observations.
            doc["runs"] = self.runs
        doc["cases"] = [c.to_dict(include_transcripts=include_transcripts) for c in self.cases]
        return doc

    def to_replay_bundle(self, *, runner_model: str | None = None) -> dict[str, Any]:
        """Minimal hand-off bundle — the raw A/B outputs, none of this run's grading.

        For a consumer that verifies a run by RE-GRADING it: it replays these outputs through
        its own deterministic graders (and re-judges judged cases with its own judge), so the
        verdicts end up being the consumer's and only the model outputs are taken on trust.
        """
        cases = []
        for c in self.cases:
            if c.with_arm is None or c.without_arm is None:
                continue
            cases.append({
                "name": c.case_name,
                "with_skill_output": c.with_arm.execution.final_output or "",
                "without_skill_output": c.without_arm.execution.final_output or "",
                # Carry the per-arm meta this run computed so the re-grade honors the SAME
                # task_attempted (apples-to-oranges skip rule) and turns/tokens. A consumer that
                # re-derives task_attempted from the output string alone re-includes a short punt
                # this run skipped — drifting the verified pass-rate denominator — and, with no
                # turn count to read, falls back to turns=1.
                "with_skill_meta": {**_arm_meta(c.with_arm.execution),
                                    "task_attempted": c.with_arm.task_attempted},
                "without_skill_meta": {**_arm_meta(c.without_arm.execution),
                                       "task_attempted": c.without_arm.task_attempted},
            })
        return {"runner_model": runner_model, "cases": cases}

    def to_attestation(
        self, *, runner_model: str | None = None, judge_model: str | None = None
    ) -> dict[str, Any]:
        """Complete pre-graded hand-off bundle — outputs, verdicts, and the judge's
        per-expectation results for BOTH arms.

        For a consumer that records this run's own grading as-is: everything it needs is already
        computed, so it can store the run with ZERO LLM calls (no re-grade, no re-judge). That is
        the trade — it takes the producer's verdicts on trust, where :meth:`to_replay_bundle`
        deliberately does not.
        """
        cases = []
        for c in self.cases:
            if c.with_arm is None or c.without_arm is None:
                continue
            cases.append({
                "name": c.case_name,
                "with_skill": {
                    "output": c.with_arm.execution.final_output or "",
                    "passed": c.with_arm.passed,
                    # Per-arm efficiency metrics. WITHOUT this `meta` a reader has no turn count
                    # and falls back to turns=1 for both arms, so an honest multi-turn run lands
                    # turns_delta=0. task_attempted rides here too so the reader honors the SAME
                    # apples-to-oranges skip rule this run applied.
                    "meta": {**_arm_meta(c.with_arm.execution),
                             "task_attempted": c.with_arm.task_attempted},
                    "expectation_results": c.expectation_results,
                    "validator_results": c.validator_results,
                },
                "without_skill": {
                    "output": c.without_arm.execution.final_output or "",
                    "passed": c.without_arm.passed,
                    "meta": {**_arm_meta(c.without_arm.execution),
                             "task_attempted": c.without_arm.task_attempted},
                    "expectation_results": c.without_expectation_results,
                    "validator_results": c.without_validator_results,
                },
            })
        payload = {
            "runner_model": runner_model,
            "judge_model": judge_model or self.runner_info.get("judge"),
            "runner_version": self.runner_info.get("version"),
            "cases": cases,
        }
        if self.runs > 1:
            # Disclose the uniform re-run count (rev 2): this bundle ships ONE case per name,
            # so the reader needs to know the aggregate rates are means over `runs` rollouts.
            payload["runs"] = self.runs
        return payload


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _arm_meta(execution: ArmExecution) -> dict[str, Any]:
    """Per-arm efficiency metrics for the hand-off bundles (a consumer differences the two arms
    into the turns/tokens deltas)."""
    return {
        "turns": execution.turns,
        "total_tokens": execution.total_tokens,
        "tool_call_count": execution.tool_call_count,
        "duration_ms": execution.duration_ms,
    }


def _cleanup_workspaces(workspaces: list[Path]) -> None:
    for ws in workspaces:
        shutil.rmtree(ws, ignore_errors=True)


def _task_attempted(execution: ArmExecution) -> bool:
    if execution.errored:
        return False
    # A multi-step arm attempted the task when it recorded any step events, even if its final
    # reply is terse and under the single-shot character floor.
    if execution.events:
        return True
    return len(execution.final_output.strip()) >= TASK_ATTEMPTED_MIN_CHARS


# Cap the runner-level `runs` multiplier so a typo (--runs 500) can't blow up cost.
_MAX_RUNS = 10


def _first_per_case_name(records: list[CaseRunRecord]) -> list[CaseRunRecord]:
    """One representative record per case_name, preserving order. With ``runs > 1`` the aggregate
    sees EVERY run (the rates are means over all of them), but the report + hand-off bundles show
    a single row per case — and, critically, a single case per name, so an importer that keys one
    result row per (run, case) can't collide."""
    seen: set[str] = set()
    out: list[CaseRunRecord] = []
    for r in records:
        if r.case_name not in seen:
            seen.add(r.case_name)
            out.append(r)
    return out


def run_suite(
    cases: list[ParsedTestCase],
    adapter: AgentAdapter,
    *,
    skill_name: str,
    skill_body: str,
    judge_call: JudgeCall,
    cache: BaselineCache | None = None,
    files: dict[str, str] | None = None,
    strict_setup: bool = True,
    judge_label: str = "",
    on_progress: ProgressFn | None = None,
    keep_workspaces: bool = False,
    runs: int = 1,
) -> SuiteRunResult:
    """Execute every case A/B and aggregate the results.

    Args:
        cases: parsed eval.yaml cases.
        adapter: how to invoke the agent (validated before the first case).
        skill_name: stable skill identifier (cache key + report header).
        skill_body: SKILL.md body injected on the with-skill arm.
        judge_call: LLM transport for semantic expectations.
        cache: optional baseline cache; when given, without-skill arms are
            reused across runs. Grading always happens fresh.
        files: bundled files staged into each case workspace.
        strict_setup: spec behavior — a failing setup command makes the
            case outcome ``error``. Pass False for lenient legacy behavior.
        judge_label: human-readable judge description for the report.
        on_progress: optional callback for CLI rendering.
        keep_workspaces: leave each arm's tmpdir on disk after grading
            (debugging aid); by default they are removed per case.
        runs: uniform i.i.d. re-runs per case (rev 2, runner-level — never
            an eval.yaml field). Each run is a fresh A/B record; the
            aggregate rates are MEANS over all (case, run) records, so the
            expected value is independent of ``runs`` — more runs only
            narrow the error bars. Capped at :data:`_MAX_RUNS`.

    Returns:
        A :class:`SuiteRunResult`.
    """
    adapter.validate()
    progress = on_progress or (lambda event, case, detail: None)
    started_at = _now_iso()
    records: list[CaseRunRecord] = []
    cache_hits = 0

    # Trigger-only cases (spec 0.3.0) belong to the trigger rail — a platform-side contract in
    # rev 2, not part of this runner — so exclude them with a disclosed count
    # (cases_skipped_trigger_only) instead of erroring. A should_trigger case that ALSO carries
    # a grader still runs the normal A/B below.
    graded_cases: list[ParsedTestCase] = []
    skipped_trigger_only = 0
    for _case in cases:
        if is_trigger_only_case(_case):
            skipped_trigger_only += 1
            progress("case_skipped_trigger_only", _case.name, "trigger-only (no grader)")
        else:
            graded_cases.append(_case)

    # Uniform expansion: every graded case runs `runs` times (rev 2 — repetition is a runner
    # setting, never a per-case author knob; author-chosen k was a lever over the published
    # number). Each entry is (case, run_index); the run index disambiguates the baseline cache
    # key so runs 2..k re-run the without-arm live instead of replaying run 0's cached output
    # (which would freeze the baseline and understate its variance).
    n_runs = max(1, min(int(runs or 1), _MAX_RUNS))
    expanded: list[tuple[ParsedTestCase, int]] = []
    for _case in graded_cases:
        expanded.extend([(_case, _r) for _r in range(n_runs)])

    for case, trial in expanded:
        progress("case_start", case.name, "")

        # 1. Workspaces (spec: setup failure ⇒ error, do not proceed).
        #    One per arm: a file-writing agent must not read the other
        #    arm's artifacts, and validators must grade per-arm state.
        workspaces: list[Path] = []
        try:
            with_ws = prepare_workspace(
                case.setup_steps, files=files,
                case_files=case.setup_files or None, strict=strict_setup
            )
            workspaces.append(with_ws)
            without_ws = prepare_workspace(
                case.setup_steps, files=files,
                case_files=case.setup_files or None, strict=strict_setup
            )
            workspaces.append(without_ws)
        except SetupStepError as exc:
            logger.warning("case %s: %s", case.name, exc)
            if not keep_workspaces:
                _cleanup_workspaces(workspaces)
            records.append(
                CaseRunRecord(case_name=case.name, outcome=Outcome.ERROR, error=str(exc))
            )
            progress("case_done", case.name, Outcome.ERROR)
            continue

        try:
            cached = False
            # 2a. With-skill arm — ONE execution contract (rev 2): the adapter invokes the agent
            # once in the prepared workspace; it may take many tool steps up to case.max_turns.
            with_exec = adapter.run(
                prompt=case.prompt, skill_body=skill_body, workspace=with_ws
            )

            # 2b. Without-skill arm — baseline cache first.
            without_exec_opt: ArmExecution | None = None
            key = baseline_cache_key(
                skill_id=skill_name, case_id=case.name, prompt=case.prompt, trial=trial
            )
            if cache is not None:
                payload = cache.get(key)
                if payload is not None:
                    without_exec_opt = ArmExecution.from_payload(payload)
                    cached = True
                    cache_hits += 1
                    progress("baseline_cache_hit", case.name, key)
            if without_exec_opt is None:
                without_exec_opt = adapter.run(
                    prompt=case.prompt, skill_body=None, workspace=without_ws
                )
                if cache is not None and not without_exec_opt.errored:
                    cache.put(key, without_exec_opt.to_payload())
            without_exec = without_exec_opt

            with_arm = ArmRecord(
                execution=with_exec, task_attempted=_task_attempted(with_exec)
            )
            without_arm = ArmRecord(
                execution=without_exec,
                task_attempted=_task_attempted(without_exec),
                cached=cached,
            )

            # 3. Grade both arms against the SAME assertions (symmetric A/B),
            #    each in its own workspace. Errored arms are failed without
            #    judging — no point burning judge tokens on output that
            #    classify_outcome will mark error.
            expectation_results: list[dict[str, Any]] = []
            validator_results: list[dict[str, Any]] = []
            without_expectation_results: list[dict[str, Any]] = []
            without_validator_results: list[dict[str, Any]] = []
            # A grader (script validator) that exits with a non-binary, undeclared
            # code (exit 2 = author error: bad spec / missing case / unknown mode)
            # is recorded errored on its result dict. Roll those up so the case is
            # EXCLUDED from lift — a broken grader must never score as a model fail
            # masquerading as honest no-lift.
            validator_errored = False
            # Same rule for the LLM judge: a transport failure is recorded
            # errored on the expectation result (judge.py) — an UNGRADED
            # expectation, not a model fail. Without this rollup a judge outage
            # on the without-arm manufactures flip_to_pass lift.
            expectation_errored = False
            for arm, arm_ws in ((with_arm, with_ws), (without_arm, without_ws)):
                if arm.execution.errored:
                    arm.passed = False
                    continue
                arm.transcript = build_transcript_v1(
                    user_input=case.prompt,
                    final_output=arm.execution.final_output,
                    llm_calls=[],
                )
                exp_results = judge_expectations(
                    case.expectations,
                    final_output=arm.execution.final_output,
                    prompt=case.prompt,
                    transcript=arm.transcript,
                    judge_call=judge_call,
                )
                val_results = run_validators(
                    case.script_validators,
                    arm_ws,
                    response_text=arm.execution.final_output,
                )
                if any(r.get("errored") for r in val_results):
                    validator_errored = True
                if any(r.get("errored") for r in exp_results):
                    expectation_errored = True
                arm.passed = all(r.get("passed") for r in exp_results) and all(
                    r.get("passed") for r in val_results
                )
                if arm is with_arm:
                    # The with-arm's grading detail is what the report shows
                    # (mirrors the platform runner's persistence choice).
                    expectation_results = exp_results
                    validator_results = val_results
                else:
                    # Kept for attestation calibration (not shown in the report).
                    without_expectation_results = exp_results
                    without_validator_results = val_results

            # 4. Outcome.
            outcome = classify_outcome(
                with_passed=with_arm.passed,
                without_passed=without_arm.passed,
                errored=with_exec.errored or without_exec.errored
                or validator_errored or expectation_errored,
            )
        finally:
            if not keep_workspaces:
                _cleanup_workspaces(workspaces)
        records.append(
            CaseRunRecord(
                case_name=case.name,
                outcome=outcome,
                with_arm=with_arm,
                without_arm=without_arm,
                expectation_results=expectation_results,
                validator_results=validator_results,
                without_expectation_results=without_expectation_results,
                without_validator_results=without_validator_results,
            )
        )
        progress("case_done", case.name, outcome)

    # The aggregate + verdict see ALL (case, run) records — the rates are MEANS over them, so
    # the expected value is independent of `runs`; the report/attestation carry one
    # representative record per case.
    display_records = _first_per_case_name(records)
    aggregate = compute_run_aggregates([r.to_aggregation_input() for r in records])
    verdict = compute_verdict([r.outcome for r in records])
    # Error-dominated floor (spec/runner-contract.md): when >25% of cases errored, the run is
    # not a valid measurement — the verdict is `error` regardless of how the surviving minority
    # scored, so no gate (or consumer) can read a pass off an invalid run.
    if aggregate.error_dominated:
        verdict = Verdict.ERROR

    runner_info = {
        "name": "skillevaluation-reference-runner",
        "version": __version__,
        "adapter": adapter.name,
        "adapter_identity": adapter.identity,
        "judge": judge_label or "(injected judge_call)",
    }
    return SuiteRunResult(
        skill_name=skill_name,
        cases=display_records,
        aggregate=aggregate,
        verdict=verdict,
        runner_info=runner_info,
        started_at=started_at,
        completed_at=_now_iso(),
        cache_hits=cache_hits,
        cases_skipped_trigger_only=skipped_trigger_only,
        runs=n_runs,
    )
