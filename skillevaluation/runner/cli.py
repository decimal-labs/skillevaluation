"""``skillevaluation`` CLI — run and validate eval suites locally.

::

    skillevaluation validate ./my-skill
    skillevaluation run ./my-skill --model claude-haiku-4-5
    skillevaluation run ./my-skill --adapter mock          # plumbing dry-run
    skillevaluation run ./my-skill --fail-on-verdict fail --min-delta-pts 10
    skillevaluation run ./my-skill --runs 3            # 3 i.i.d. runs per case, MEAN-averaged

Everything runs on YOUR API key, on your machine. Nothing is uploaded
anywhere unless you pass ``--export-url``.

stdlib-argparse on purpose: the core library stays PyYAML-only, and the
CLI's table output is plain text — no rich/click dependency to carry.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from skillevaluation import __version__
from skillevaluation.aggregation import DeltaResult
from skillevaluation.parser import EvalYamlParseError, is_trigger_only_case

from .adapters.base import AdapterError, AgentAdapter
from .cache import BaselineCache
from .judge import JudgeCall, suite_needs_llm_judge
from .orchestrator import SuiteRunResult, run_suite
from .skill_dir import LoadedSkill, SkillDirError, load_skill_dir

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2

_VERDICT_CHOICES = ("fail", "mixed", "error")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillevaluation",
        description="A/B benchmark a skill against its eval.yaml — locally, on your own API key.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Parse and validate a skill dir's eval.yaml")
    p_validate.add_argument("skill_dir", help="Directory containing SKILL.md + eval.yaml")

    p_scan = sub.add_parser("scan", help="Static safety scan of a skill (SKILL.md) — free, no LLM")
    p_scan.add_argument("path", help="A SKILL.md file, a skill dir, or a tree of skill dirs")
    p_scan.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    p_scan.add_argument(
        "--fail-on",
        choices=("blocked", "flagged", "never"),
        default="blocked",
        help="Non-zero exit when any skill reaches this status (default: blocked)",
    )
    p_scan.add_argument(
        "--output", "-o", default="", help="Write json/sarif here instead of stdout"
    )

    p_run = sub.add_parser("run", help="Execute the suite A/B and print the delta table")
    p_run.add_argument("skill_dir", help="Directory containing SKILL.md + eval.yaml")
    p_run.add_argument(
        "--adapter",
        choices=("llm", "claude-code", "mock"),
        default="llm",
        help="How to invoke the agent (default: llm — a direct single-shot completion)",
    )
    p_run.add_argument(
        "--model",
        default=os.environ.get("SKILLEVAL_MODEL", ""),
        help="Agent model for the llm adapter / --model for claude-code (env: SKILLEVAL_MODEL)",
    )
    p_run.add_argument(
        "--judge-model",
        default=os.environ.get("SKILLEVAL_JUDGE_MODEL", ""),
        help="Judge model for semantic expectations (default: --model; env: SKILLEVAL_JUDGE_MODEL)",
    )
    p_run.add_argument(
        "--output",
        "-o",
        default="results.json",
        help="Where to write the run result document (default: ./results.json)",
    )
    p_run.add_argument(
        "--json", action="store_true", help="Print results.json to stdout instead of the table"
    )
    p_run.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the local baseline cache (re-run the without arm)",
    )
    p_run.add_argument(
        "--trajectories",
        metavar="DIR",
        help="Also write per-arm canonical transcripts into DIR",
    )
    p_run.add_argument(
        "--no-embed-transcripts",
        dest="embed_transcripts",
        action="store_false",
        default=True,
        help="Omit per-arm transcripts from results.json (embedded by "
        "default, truncated, so a collector can render the sessions)",
    )
    p_run.add_argument(
        "--fail-on-verdict",
        action="append",
        choices=_VERDICT_CHOICES,
        default=None,
        metavar="VERDICT",
        help="Exit 1 when the run verdict is one of these (repeatable; "
        f"choices: {', '.join(_VERDICT_CHOICES)})",
    )
    p_run.add_argument(
        "--min-delta-pts",
        type=float,
        default=None,
        metavar="PTS",
        help="Exit 1 when the pass-rate delta (percentage points) is below PTS",
    )
    p_run.add_argument(
        "--export-url",
        default=os.environ.get("SKILLEVAL_EXPORT_URL", ""),
        help="POST the results document to this URL after the run "
        "(bearer token from SKILLEVAL_EXPORT_TOKEN; env: SKILLEVAL_EXPORT_URL)",
    )
    p_run.add_argument(
        "--max-turns",
        type=int,
        default=12,
        help="claude-code adapter: turn budget per arm (default: 12)",
    )
    p_run.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="Leave each arm's tmpdir on disk after grading (debugging aid)",
    )
    p_run.add_argument(
        "--emit-replay-bundle",
        metavar="PATH",
        help="Also write a replay bundle (raw A/B outputs only) to PATH, for a collector that "
        "re-grades the outputs with its own graders instead of trusting this run's verdicts.",
    )
    p_run.add_argument(
        "--emit-attestation",
        metavar="PATH",
        help="Also write a pre-graded bundle (outputs + verdicts + per-arm judge results) to "
        "PATH, for a collector that records this runner's own grading without re-grading.",
    )
    p_run.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Uniform i.i.d. re-runs per case (default 1; capped at 10). Rates are MEANS over "
        "all runs — the expected value does not change with N; more runs narrow the error "
        "bars. Runner-level on purpose: repetition is never a per-case eval.yaml field.",
    )
    return parser


# ── adapter / judge wiring ───────────────────────────────────────────


def _build_adapter(args: argparse.Namespace, skill: LoadedSkill) -> AgentAdapter:
    if args.adapter == "mock":
        from .adapters.mock import MockAdapter

        return MockAdapter()
    if args.adapter == "claude-code":
        from .adapters.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(
            model=args.model or None,
            skill_name=skill.name,
            max_turns=args.max_turns,
        )
    if not args.model:
        raise AdapterError(
            "--model is required for the llm adapter "
            "(e.g. --model claude-haiku-4-5 / gpt-5.2 / gemini-3.5-flash)"
        )
    from .adapters.llm import LLMAdapter

    return LLMAdapter(args.model)


def _build_judge(
    args: argparse.Namespace, *, needs_llm_judge: bool = True
) -> tuple[JudgeCall, str]:
    if args.adapter == "mock":
        from .adapters.mock import mock_judge_call

        return mock_judge_call, "mock (verbatim-substring plumbing judge)"
    if not needs_llm_judge:
        # Every expectation is structural — no judge model, no judge key.
        # (e.g. claude-code adapter + a response_contains:-only suite runs
        # without any raw API key at all.)
        def no_judge_call(rendered_prompt: str) -> dict[str, Any]:
            raise AdapterError("no LLM judge configured — suite was detected structural-only")

        return no_judge_call, "none (all expectations are structural)"
    judge_model = args.judge_model or args.model
    if not judge_model:
        raise AdapterError(
            "--model or --judge-model is required to grade semantic expectations "
            "(structural assertions like response_contains: need no judge)"
        )
    from .adapters.llm import make_judge_call

    return make_judge_call(judge_model), f"llm:{judge_model} @ temperature 0"


# ── output rendering ─────────────────────────────────────────────────


def _fmt_delta_row(label: str, delta: DeltaResult | None) -> str:
    if delta is None:
        return ""
    pct = delta.delta_pct
    pct_str = "n/a (zero baseline)" if pct is None else f"{pct:+.1f}%"
    return (
        f"  {label:<14}{delta.with_skill_avg:>10.1f} (with)"
        f"{delta.without_skill_avg:>12.1f} (without)   {pct_str}"
    )


def render_table(result: SuiteRunResult) -> str:
    """The human delta table. Plain text, list-UI friendly."""
    agg = result.aggregate
    pr = agg.pass_rate
    outcome_counts: dict[str, int] = {}
    for c in result.cases:
        outcome_counts[c.outcome] = outcome_counts.get(c.outcome, 0) + 1

    adapter_id = result.runner_info["adapter_identity"]
    lines = [
        "",
        f"  {result.skill_name} · {agg.total_cases} case(s) · adapter={adapter_id}",
        f"  judge: {result.runner_info['judge']}",
        "",
    ]
    for c in result.cases:
        marker = {
            "flip_to_pass": "✓",
            "pass_kept": "=",
            "fail_kept": "✗",
            "flip_to_fail": "⚠",
            "error": "!",
        }.get(c.outcome, "?")
        cached = "  (baseline cached)" if c.without_arm and c.without_arm.cached else ""
        detail = f"  — {c.error}" if c.error else cached
        if c.outcome == "error" and not c.error:
            # Surface WHY the arm errored (e.g. "credit balance too low",
            # "503 Service Unavailable") — burying it in results.json made
            # failed runs look like mysteries on the terminal.
            arm_error = next(
                (
                    arm.execution.error
                    for arm in (c.with_arm, c.without_arm)
                    if arm is not None and arm.execution.error
                ),
                None,
            )
            if arm_error:
                detail = f"  — {arm_error[:110]}"
        lines.append(f"    {marker} {c.case_name:<28} {c.outcome}{detail}")
    lines.append("")
    pr_with = pr.get("with_skill")
    if pr_with is None:
        # N/A contract: nothing aggregated (every case errored or was apples-to-oranges skipped),
        # so there is no honest pass rate — never render a fabricated 0%.
        lines.append("  Pass rate     n/a — no comparable case (all errored or skipped)")
    else:
        # with/without are set together when with_skill is not None (`or 0` only guards the
        # type), but delta_pts can still be None on an error-dominated run (the headline claim
        # is withheld) — render the withdrawal explicitly, never a fabricated +0 pts.
        pr_without = pr.get("without_skill") or 0.0
        pr_delta = pr.get("delta_pts")
        delta_str = "n/a (error-dominated)" if pr_delta is None else f"{pr_delta:+.0f} pts"
        lines.append(
            f"  Pass rate     {pr_with * 100:>9.0f}% (with)"
            f"{pr_without * 100:>11.0f}% (without)   {delta_str}"
        )
    for label, delta in (
        ("Avg duration", agg.duration_ms),
        ("Avg turns", agg.turns),
        ("Avg tokens", agg.tokens),
        ("Avg tool calls", agg.tool_calls),
    ):
        row = _fmt_delta_row(label, delta)
        if row:
            lines.append(row)
    disclosure = (
        f"  aggregated {agg.cases_aggregated}/{agg.total_cases}"
        f" · errors {agg.errors}"
        f" · apples-to-oranges skipped {agg.cases_skipped_apples_oranges}"
        f" · baseline cache hits {result.cache_hits}"
    )
    if result.cases_skipped_trigger_only:
        disclosure += f" · trigger-only A/B-excluded {result.cases_skipped_trigger_only}"
    lines += ["", disclosure]
    lines += ["", f"  Verdict: {result.verdict.upper()}", ""]
    if agg.error_dominated:
        lines += [
            f"  ⚠ error-dominated run — {agg.errors}/{agg.total_cases} case(s) errored "
            "(>25%); the headline delta is withheld.",
            "",
        ]
    if outcome_counts.get("flip_to_fail"):
        lines += [
            f"  ⚠ {outcome_counts['flip_to_fail']} regression(s) — the skill made "
            "the agent WORSE on those cases.",
            "",
        ]
    return "\n".join(lines)


def _write_trajectories(result: SuiteRunResult, directory: str) -> int:
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for case in result.cases:
        for arm_label, arm in (("with", case.with_arm), ("without", case.without_arm)):
            if arm is None or not arm.transcript:
                continue
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in case.case_name)
            (out_dir / f"{safe}__{arm_label}.txt").write_text(arm.transcript, encoding="utf-8")
            written += 1
    return written


def _export(doc: dict[str, Any], url: str) -> None:
    """POST results.json to a collector. Best-effort with a clear message.

    The document carries the full case prompts and (unless ``--no-embed-transcripts``) each arm's
    transcript, plus truncated provider error snippets — sensitive enough that we refuse to ship it
    over a plaintext ``http://`` URL to a non-local host. Use ``https``, a localhost collector, or
    set ``SKILLEVAL_EXPORT_INSECURE=1`` to override.
    """
    try:
        import httpx
    except ImportError:
        raise AdapterError(
            "--export-url needs httpx — pip install 'skillevaluation[runner]'"
        ) from None
    from urllib.parse import urlparse

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "::1")
    if scheme != "https" and not is_local:
        msg = (
            f"--export-url is {scheme or 'schemeless'}://{host or '?'} (not https) — the results "
            f"document (full prompts + per-arm transcripts) would be sent UNENCRYPTED"
        )
        _insecure = os.environ.get("SKILLEVAL_EXPORT_INSECURE", "").strip().lower()
        if _insecure not in ("1", "true", "yes"):
            raise AdapterError(
                msg + ". Refusing; use https, a localhost collector, or set "
                "SKILLEVAL_EXPORT_INSECURE=1 to override."
            )
        print(f"  ⚠ {msg}; proceeding because SKILLEVAL_EXPORT_INSECURE is set.", file=sys.stderr)
    headers = {"content-type": "application/json"}
    token = os.environ.get("SKILLEVAL_EXPORT_TOKEN")
    if token:
        headers["authorization"] = f"Bearer {token}"
    resp = httpx.post(url, json=doc, headers=headers, timeout=30)
    resp.raise_for_status()


# ── commands ─────────────────────────────────────────────────────────


def _resolve_skill_dir(skill_dir: str) -> str:
    """Resolve the skill-dir argument: a real local path wins; otherwise a bare name (no path
    separator, absent on disk) falls back to a packaged example — so ``skillevaluation run
    api-error-envelope`` works post-install from any working directory, not just from a checkout."""
    if Path(skill_dir).exists():
        return skill_dir
    if not any(sep in skill_dir for sep in ("/", "\\", os.sep)):
        try:
            from skillevaluation.resources import example_path

            return str(example_path(skill_dir))
        except FileNotFoundError:
            pass
    return skill_dir  # let load_skill_dir raise a clear "not found" for a bad path


def _find_skill_mds(path: str) -> list[Path]:
    p = Path(path)
    if p.is_file():
        return [p]
    if p.is_dir():
        direct = p / "SKILL.md"
        if direct.exists():
            return [direct]
        return sorted(p.rglob("SKILL.md"))
    return []


_SCAN_RANK = {"clean": 0, "flagged": 1, "blocked": 2}
_SCAN_ICON = {"clean": "✓", "flagged": "⚠", "blocked": "⛔"}


def _format_scan_text(per: list[tuple[str, str, dict[str, Any]]]) -> str:
    lines: list[str] = []
    for f, name, res in per:
        icon = _SCAN_ICON.get(res["status"], "?")
        lines.append(f"  {icon} {name}: {res['status']} — {res['summary']}")
        for fnd in res.get("findings", []):
            loc = f":{fnd['line']}" if fnd.get("line") else ""
            lines.append(f"      [{fnd['severity']}] {fnd['check']}{loc} — {fnd['message']}")
            if fnd.get("remediation"):
                lines.append(f"        fix: {fnd['remediation']}")
    return "\n".join(lines) if lines else "  (no skills scanned)"


def cmd_scan(args: argparse.Namespace) -> int:
    from skillevaluation import safety

    from .skill_dir import split_frontmatter

    files = _find_skill_mds(args.path)
    if not files:
        print(f"  ✗ no SKILL.md found under {args.path}", file=sys.stderr)
        return EXIT_USAGE

    per: list[tuple[str, str, dict[str, Any]]] = []
    worst = 0
    for path in files:
        fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
        name = str(fm.get("name") or path.parent.name)
        res = safety.scan_skill_content(
            body,
            name=name,
            description=str(fm.get("description") or ""),
            category=fm.get("category"),
            allowed_tools=fm.get("allowed-tools") or fm.get("allowed_tools"),
            trigger_phrases=fm.get("trigger_phrases") or fm.get("triggers"),
        )
        per.append((str(path), name, res))
        worst = max(worst, _SCAN_RANK.get(res["status"], 0))

    if args.format == "json":
        out = json.dumps({"skills": [{"file": fp, "name": n, **r} for fp, n, r in per]}, indent=2)
    elif args.format == "sarif":
        runs: list[Any] = []
        for fp, n, r in per:
            runs.extend(safety.to_sarif(r, skill_name=n, file_path=fp)["runs"])
        out = json.dumps(
            {
                "version": "2.1.0",
                "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                "runs": runs,
            },
            indent=2,
        )
    else:
        out = _format_scan_text(per)

    if args.output:
        Path(args.output).write_text(out + "\n", encoding="utf-8")
        print(f"  → wrote {args.format} to {args.output}", file=sys.stderr)
    else:
        print(out)

    fail_rank = {"blocked": 2, "flagged": 1, "never": 99}[args.fail_on]
    return EXIT_GATE_FAILED if worst >= fail_rank else EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        skill = load_skill_dir(_resolve_skill_dir(args.skill_dir))
    except (SkillDirError, EvalYamlParseError) as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return EXIT_GATE_FAILED
    n_validators = sum(len(c.script_validators) for c in skill.cases)
    n_expectations = sum(len(c.expectations) for c in skill.cases)
    n_trigger = sum(1 for c in skill.cases if c.should_trigger is not None)
    n_trigger_only = sum(1 for c in skill.cases if is_trigger_only_case(c))
    print(
        f"  ✓ {skill.name}: {len(skill.cases)} case(s), "
        f"{n_expectations} expectation(s), {n_validators} validator(s)"
    )
    if n_trigger:
        print(
            f"    trigger case(s): {n_trigger} ({n_trigger_only} trigger-only — excluded from "
            f"the A/B loop; routing is graded platform-side, not by this runner)"
        )
    if skill.files:
        print(f"    bundled files staged into the sandbox: {len(skill.files)}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    try:
        skill = load_skill_dir(_resolve_skill_dir(args.skill_dir))
    except (SkillDirError, EvalYamlParseError) as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        adapter = _build_adapter(args, skill)
        judge_call, judge_label = _build_judge(
            args, needs_llm_judge=suite_needs_llm_judge(skill.cases)
        )
    except AdapterError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return EXIT_USAGE

    cache = None if args.no_cache else BaselineCache(f"{adapter.identity}|{skill.name}")

    def progress(event: str, case: str, detail: str) -> None:
        if args.json:
            return
        if event == "case_start":
            print(f"  … {case}", file=sys.stderr)
        elif event == "case_done":
            print(f"    → {detail}", file=sys.stderr)

    try:
        result = run_suite(
            skill.cases,
            adapter,
            skill_name=skill.name,
            skill_body=skill.body,
            judge_call=judge_call,
            cache=cache,
            files=skill.files or None,
            judge_label=judge_label,
            on_progress=progress,
            keep_workspaces=args.keep_workspaces,
            runs=args.runs,
        )
    except AdapterError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return EXIT_USAGE

    doc = result.to_results_json(include_transcripts=args.embed_transcripts)

    output_path = Path(args.output)
    # mkdir, don't crash: by this point the run has already happened (and
    # cost real tokens) — losing the document to a missing directory after
    # the spend is the worst possible failure.
    if output_path.parent and not output_path.parent.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.trajectories:
        n = _write_trajectories(result, args.trajectories)
        print(f"  wrote {n} transcript(s) to {args.trajectories}", file=sys.stderr)

    # Optional hand-off bundles for a collector — one re-graded from raw outputs, one
    # carrying this runner's own grading.
    if getattr(args, "emit_replay_bundle", None):
        bundle = result.to_replay_bundle(runner_model=args.model or None)
        Path(args.emit_replay_bundle).write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if not args.json:
            print(f"  replay bundle written to {args.emit_replay_bundle}")
    if getattr(args, "emit_attestation", None):
        att = result.to_attestation(
            runner_model=args.model or None,
            judge_model=args.judge_model or args.model or None,
        )
        Path(args.emit_attestation).write_text(
            json.dumps(att, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if not args.json:
            print(f"  attestation written to {args.emit_attestation}")

    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        print(render_table(result))
        print(f"  results written to {output_path}")

    if args.export_url:
        try:
            _export(doc, args.export_url)
            print(f"  exported results to {args.export_url}", file=sys.stderr)
        except Exception as exc:
            print(f"  ⚠ export failed (results kept locally): {exc}", file=sys.stderr)

    # ── CI gates ──
    failed = False
    fail_verdicts = args.fail_on_verdict or []
    if result.verdict in fail_verdicts:
        print(f"  ✗ gate: verdict {result.verdict!r} is in --fail-on-verdict", file=sys.stderr)
        failed = True
    elif fail_verdicts and result.aggregate.error_dominated:
        # An error-dominated run (>25% of cases errored) is not a valid measurement — it can
        # never clear a verdict gate, whichever verdicts were listed.
        print(
            "  ✗ gate: run is error-dominated (>25% of cases errored) — an invalid "
            "measurement cannot pass a --fail-on-verdict gate",
            file=sys.stderr,
        )
        failed = True
    if args.min_delta_pts is not None:
        delta = result.aggregate.pass_rate.get("delta_pts")
        if delta is None:
            # Degenerate or error-dominated run: there IS no honest delta, so a floor can't be
            # proven — fail the gate rather than crash (None < float) or fabricate a 0.
            reason = "error-dominated" if result.aggregate.error_dominated else "nothing aggregated"
            print(
                f"  ✗ gate: pass-rate delta is n/a ({reason}) — cannot clear "
                f"--min-delta-pts {args.min_delta_pts:+.1f}",
                file=sys.stderr,
            )
            failed = True
        elif delta < args.min_delta_pts:
            print(
                f"  ✗ gate: pass-rate delta {delta:+.1f} pts < --min-delta-pts "
                f"{args.min_delta_pts:+.1f}",
                file=sys.stderr,
            )
            failed = True
    return EXIT_GATE_FAILED if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "scan":
        return cmd_scan(args)
    return cmd_run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
