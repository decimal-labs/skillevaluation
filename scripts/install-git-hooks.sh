#!/usr/bin/env bash
# Install the CI gate as a git pre-push hook so nothing pushes a commit that fails the fast CI
# checks. Re-run any time. Emergency bypass: git push --no-verify.
#
# The hook validates the WORKING TREE, which equals what's being pushed only when the tree is
# CLEAN — so it SKIPS (with a warning) on a dirty tree rather than false-blocking a push over
# unrelated local drift. A commit-then-push flow lands here with a clean tree, so the common path
# IS gated. For belt-and-suspenders, also call `bash scripts/ci-gate.sh` from your commit step.
set -euo pipefail
cd "$(dirname "$0")/.."
hook=".git/hooks/pre-push"
marker="installed by scripts/install-git-hooks.sh"

# Never clobber a pre-push hook this script didn't write — another gate (a secret scanner, say)
# may already own pre-push, and silently replacing it stops that gate with nothing to say so.
# The convention such wrappers follow is to end by exec'ing .git/hooks/pre-push.pre-guard, so
# installing there chains beneath them and keeps BOTH gates live. Our own hook carries the marker
# above, so re-running this script overwrites it in place and stays idempotent.
if [ -f "$hook" ] && ! grep -q "$marker" "$hook"; then
  hook=".git/hooks/pre-push.pre-guard"
  echo "note: another pre-push hook is already installed — chaining beneath it at $hook."
  echo "      it only runs if that hook execs pre-push.pre-guard; check it if the gate never fires."
fi

cat > "$hook" <<HOOK
#!/usr/bin/env bash
# $marker
HOOK
cat >> "$hook" <<'HOOK'
root="$(git rev-parse --show-toplevel)"
if [ -n "$(git -C "$root" status --porcelain)" ]; then
  echo "[pre-push] working tree dirty — skipping the CI gate (can't trust tree==pushed commit)." >&2
  echo "[pre-push] run 'bash scripts/ci-gate.sh' yourself, or commit/stash first to enable the gate." >&2
  exit 0
fi
exec "$root/scripts/ci-gate.sh"
HOOK
chmod +x "$hook"
echo "installed $hook -> scripts/ci-gate.sh (skips on a dirty tree)"
# plain `[ ] && echo` would exit 1 here under `set -e` when the test is false
if [ "$hook" = ".git/hooks/pre-push.pre-guard" ]; then
  echo "  (chained beneath the existing pre-push hook, which stays the outer gate)"
fi
