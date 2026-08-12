#!/usr/bin/env bash
# Release skillevaluation to PyPI.
#
# Usage:  ./scripts/release.sh   (run from anywhere inside the repo)
#
# Builds the package, validates it, smoke-tests the built wheel in a clean
# environment, refuses to proceed if the version is unsafe, and only then —
# after a typed confirmation — uploads to PyPI.
#
# This is the LOCAL fallback path. The normal release is a published GitHub Release,
# which uploads via PyPI Trusted Publishing and needs no stored credential.
#
# Prerequisites: `uv` installed, and PyPI upload credentials that `twine` can find.
# See RELEASING.md for the full runbook.
set -euo pipefail

NAME="skillevaluation"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- resolve the version, and require the two copies to agree --------------
# skillevaluation hardcodes __version__, so it must match pyproject.toml.
PYPROJECT_VERSION="$(grep -m1 '^version = ' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')"
INIT_VERSION="$(grep -m1 '^__version__' "$NAME/__init__.py" | sed -E 's/.*"([^"]+)".*/\1/')"

if [[ -z "$PYPROJECT_VERSION" ]]; then
  echo "ERROR: could not read version from pyproject.toml" >&2; exit 1
fi
if [[ "$PYPROJECT_VERSION" != "$INIT_VERSION" ]]; then
  echo "ERROR: version mismatch — pyproject.toml=$PYPROJECT_VERSION but $NAME/__init__.py=$INIT_VERSION" >&2
  echo "       bump both to the same value before releasing." >&2
  exit 1
fi
VERSION="$PYPROJECT_VERSION"
echo "==> Releasing $NAME $VERSION"

# --- refuse to clobber an existing release (PyPI is append-only) -----------
HTTP="$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$NAME/$VERSION/json" || echo 000)"
if [[ "$HTTP" == "200" ]]; then
  echo "ERROR: $NAME $VERSION already exists on PyPI — a version can never be reused." >&2
  echo "       bump the version and try again." >&2
  exit 1
fi

# --- changelog reminder (advisory only) ------------------------------------
if ! grep -q "$VERSION" CHANGELOG.md; then
  echo "WARNING: no '$VERSION' entry found in CHANGELOG.md" >&2
fi

# --- build + validate ------------------------------------------------------
rm -rf dist
uv build
uvx twine check dist/*

# --- smoke-test the built wheel in a throwaway environment -----------------
wheels=(dist/*.whl); WHEEL="${wheels[0]}"
echo "==> Smoke-testing $WHEEL"
uv run --no-project --with "$WHEEL" -- python -c "
import skillevaluation as m
assert m.__version__ == '$VERSION', f'wheel reports {m.__version__}, expected $VERSION'
import skillevaluation.parser, skillevaluation.outcomes, skillevaluation.aggregation, skillevaluation.baseline, skillevaluation.trajectory.format_v1
print('  import OK, __version__ =', m.__version__)
"

# --- confirm, then the one irreversible step -------------------------------
echo
echo "Built and verified $NAME $VERSION."
echo "Uploading to PyPI is PERMANENT — the version can never be replaced or reused."
read -r -p "Type 'yes' to upload: " ANS
[[ "$ANS" == "yes" ]] || { echo "Aborted — nothing uploaded."; exit 1; }

uvx twine upload dist/*

# --- verify it went live (the version endpoint updates fastest) ------------
echo "==> Verifying on PyPI (cache may lag a minute)"
curl -s -o /dev/null -w "  https://pypi.org/pypi/$NAME/$VERSION/json -> HTTP %{http_code}\n" \
  "https://pypi.org/pypi/$NAME/$VERSION/json" || true
echo "Done: https://pypi.org/project/$NAME/$VERSION/"
