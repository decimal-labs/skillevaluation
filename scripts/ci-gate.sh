#!/usr/bin/env bash
# Fast pre-push CI gate — runs the BLOCKING checks from .github/workflows/ci.yml's "Lint + types +
# schema" job (ruff + mypy strict + schema), which are fast (~15s). The point: a loop / agent must not
# push lint- or type-red commits to main, which is the most common way CI goes red. The full
# pytest suite stays in CI; this gate is just the quick "does it even lint/type/load" check.
#
# Wire-in: installed as .git/hooks/pre-push by scripts/install-git-hooks.sh; also runnable directly
# (`bash scripts/ci-gate.sh`) and from any loop's commit step.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
RUFF=".venv/bin/ruff";  [ -x "$RUFF" ] || RUFF="$PY -m ruff"
MYPY=".venv/bin/mypy";  [ -x "$MYPY" ] || MYPY="$PY -m mypy"

echo "[ci-gate] ruff…"
$RUFF check skillevaluation tests
echo "[ci-gate] mypy (strict)…"
$MYPY skillevaluation
echo "[ci-gate] eval.yaml schema…"
$PY -c "import json, jsonschema; jsonschema.Draft202012Validator.check_schema(json.load(open('schemas/eval-yaml.schema.json')))"
echo "[ci-gate] OK — safe to push"
