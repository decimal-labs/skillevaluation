"""Reference runner — execute an eval.yaml suite A/B, locally.

This subpackage is the reference implementation of the
[runner contract](../../spec/runner-contract.md). It is what powers the
``skillevaluation run`` CLI, and it is intentionally importable piecemeal
so other runners (including the DecimalAI platform's hosted runner) can
reuse the judged/graded pieces without adopting the orchestration:

    from skillevaluation.runner.judge import judge_expectations, try_structural_assertion
    from skillevaluation.runner.validators import run_validators
    from skillevaluation.runner.workspace import prepare_workspace, SetupStepError
    from skillevaluation.runner.cache import BaselineCache
    from skillevaluation.runner.orchestrator import run_suite

Install with the ``runner`` extra to get the HTTP-backed pieces
(the direct-LLM adapter and ``--export-url``)::

    pip install "skillevaluation[runner]"

The core package stays PyYAML-only; ``httpx`` is imported lazily and only
by the parts that need it.
"""

from __future__ import annotations
