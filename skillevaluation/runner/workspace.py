"""Per-case workspace preparation.

Each test case gets a fresh temporary directory. ``setup`` steps from
eval.yaml run inside it; optional bundled files (a skill's ``scripts/``
attachments, fixtures) and the case's own ``setup.files`` mapping (spec
0.3.0) are materialized first so setup steps and validators can
reference them.

Two strictness modes:

* ``strict=True`` (spec behavior, used by the reference CLI runner):
  a setup command exiting non-zero raises :class:`SetupStepError`; the
  runner contract says the case outcome MUST be ``error`` and the case
  MUST NOT proceed.
* ``strict=False`` (lenient): failures are logged and ignored. This
  preserves the behavior of runners that predate the strict rule and is
  useful when setup steps are best-effort hints.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("skillevaluation.runner.workspace")

SETUP_STEP_TIMEOUT_S = 10

# Setup steps run UNTRUSTED shell from a case's eval.yaml.
# Do NOT inherit the parent environment — a server-side grader's process carries
# provider API keys and database credentials, which a malicious `setup:` step could
# exfiltrate. Start from a minimal allowlist, mirroring
# runner/validators.py::run_validators. HOME and TMPDIR are NOT
# inherited — they are re-pointed at the workspace below so a $HOME-relative
# read (~/.aws/credentials, ~/.config/gcloud) can't reach the real home dir.
_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


class SetupStepError(RuntimeError):
    """A setup command failed (non-zero exit, timeout, or spawn failure)."""

    def __init__(self, cmd: str, detail: str):
        super().__init__(f"setup step failed: {cmd!r} — {detail}")
        self.cmd = cmd
        self.detail = detail


def prepare_workspace(
    setup_steps: list[str],
    files: dict[str, str] | None = None,
    *,
    case_files: dict[str, str] | None = None,
    strict: bool = False,
    prefix: str = "skilleval-",
    timeout_s: int = SETUP_STEP_TIMEOUT_S,
) -> Path:
    """Create a tmpdir, materialize bundled files, run setup commands in it.

    Args:
        setup_steps: shell commands from the case's ``setup`` list, run
            sequentially with ``cwd=<workspace>``.
        files: optional ``{relative_path: content}`` to write before any
            setup step runs (e.g. a skill's bundled ``scripts/``).
        case_files: optional ``{relative_path: content}`` from the case's
            ``setup.files`` mapping (spec 0.3.0) — staged before any setup
            command runs, with the same path-escape rejection as ``files``;
            a case entry wins over a bundled file at the same path.
        strict: raise :class:`SetupStepError` on the first failing step
            (spec-conforming) instead of logging and continuing.
        prefix: tmpdir name prefix.
        timeout_s: per-step timeout.

    Returns:
        Path of the prepared workspace.

    Raises:
        SetupStepError: in strict mode, when a step exits non-zero,
            times out, or cannot be spawned.
    """
    workspace = Path(tempfile.mkdtemp(prefix=prefix))

    workspace_resolved = workspace.resolve()
    for rel_path, content in {**(files or {}), **(case_files or {})}.items():
        # Bundled + case file keys are untrusted (the runner treats case content
        # as untrusted shell). Reject absolute keys or ``..`` escapes that would
        # land outside the workspace.
        dest = workspace / rel_path
        try:
            dest_resolved = dest.resolve()
        except OSError:
            logger.warning("could not resolve bundled file path %s", rel_path)
            continue
        if dest_resolved != workspace_resolved and workspace_resolved not in dest_resolved.parents:
            logger.warning("refusing to stage bundled file outside workspace: %s", rel_path)
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content or "", encoding="utf-8")
        except OSError:
            logger.warning("could not stage bundled file %s", rel_path, exc_info=True)

    # Scrubbed env, built once — secrets never reach setup steps. HOME/TMPDIR point INTO
    # the workspace so a $HOME-relative read can't escape to the real home directory.
    safe_env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    safe_env["HOME"] = str(workspace_resolved)
    safe_env["TMPDIR"] = str(workspace_resolved)
    for cmd in setup_steps:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(workspace),
                capture_output=True,
                timeout=timeout_s,
                check=False,
                env=safe_env,
            )
        except subprocess.TimeoutExpired:
            if strict:
                raise SetupStepError(cmd, f"timed out after {timeout_s}s") from None
            logger.warning("setup step timed out: %s", cmd)
            continue
        except Exception as exc:
            if strict:
                raise SetupStepError(cmd, str(exc)) from exc
            logger.warning("setup step failed to spawn: %s", cmd, exc_info=True)
            continue

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:300]
            if strict:
                raise SetupStepError(cmd, f"exit {proc.returncode}: {stderr}")
            logger.warning("setup step exited %s: %s", proc.returncode, cmd)

    return workspace
