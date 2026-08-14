"""skillevaluation — A/B benchmarking for skills.

Public API:

    from skillevaluation.parser import parse_eval_yaml, ParsedTestCase, EvalYamlParseError
    from skillevaluation.outcomes import Outcome, classify_outcome, Verdict, compute_verdict
    from skillevaluation.aggregation import compute_run_aggregates, delta_pct
    from skillevaluation.baseline import baseline_cache_key
    from skillevaluation.trajectory.format_v1 import build_transcript_v1, FORMAT_VERSION
    from skillevaluation.resources import load_schema, read_spec

Reference runner (install the ``runner`` extra for the LLM-backed parts):

    from skillevaluation.runner.orchestrator import run_suite
    from skillevaluation.runner.judge import judge_expectations, try_structural_assertion
    from skillevaluation.runner.validators import run_validators
    from skillevaluation.runner.workspace import prepare_workspace
    from skillevaluation.runner.cache import BaselineCache

Or from a shell::

    skillevaluation run ./my-skill --model claude-haiku-4-5

See https://github.com/decimal-labs/skillevaluation for the spec.
"""

__version__ = "0.7.1"
