"""Shell validators — deterministic, sandboxed assertions.

Each validator is a shell command run with ``cwd=<workspace>``; it passes
when its exit code equals ``expect_exit_code`` (default 0).

The agent's text output is staged two ways before any validator runs, so
a validator can grade the *artifact the model emitted* (run the SQL,
compile the regex, schema-check the JSON):

* written to ``<workspace>/response.txt`` (full text, always)
* exported as the ``RESPONSE_TEXT`` environment variable (truncated to
  :data:`ENV_RESPONSE_MAX_CHARS` — single env entries have a hard OS
  size ceiling; read ``response.txt`` for the full output)

Result dicts conform to the ``validatorResult`` shape in
``schemas/test-case-result.schema.json``.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:  # POSIX only — used for the resource-limit sandbox layer below.
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

logger = logging.getLogger("skillevaluation.runner.validators")

VALIDATOR_TIMEOUT_S = 30
_CAPTURE_LIMIT = 500
# A single environment string has a hard OS ceiling (~128KB per entry on
# Linux/MAX_ARG_STRLEN); past it every spawn fails with E2BIG. Cap the env
# copy of the response — response.txt always carries the full text.
ENV_RESPONSE_MAX_CHARS = 64_000

# ── Validator sandbox ───────────────────────────────────────────────────────────
# A validator is author-controlled shell, and running a suite executes it on YOUR machine —
# including suites for skills you did not write. The env-scrub (see run_validators) already
# removes the secret-exfil path; these two layers harden the *execution* itself:
#
#   1. Resource limits (always on, POSIX): cap CPU seconds, max file size, and core
#      dumps in a preexec_fn so a validator can't peg a core, fill the disk, or dump
#      core. We deliberately do NOT set RLIMIT_AS (breaks Python validators, which
#      reserve large virtual memory) or RLIMIT_NPROC (counts the parent's threads in a
#      shared-UID container) — memory + fork-bomb belong to the container cgroup.
#   2. Network isolation (DEFAULT-ON, probe-gated): when the host can create a user+net
#      namespace, wrap the command in `unshare -rn` so the validator has NO network —
#      closing SSRF to the cloud metadata server (which would otherwise mint a
#      service-account token). The safe path is now the default; it fails OPEN (no
#      isolation) only on hosts where unshare is unavailable/blocked, so the published
#      runner keeps working everywhere. Opt OUT with DECIMAL_VALIDATOR_SANDBOX_NET in
#      {off,0,false,no}. Even so, network isolation alone is NOT a full sandbox (no fs
#      jail) — for untrusted third-party skills, run the whole benchmark inside a
#      de-privileged, network-isolated container that is thrown away after the run.
_SANDBOX_CPU_S = int(os.environ.get("DECIMAL_VALIDATOR_CPU_S", "60"))
_SANDBOX_FSIZE_BYTES = int(os.environ.get("DECIMAL_VALIDATOR_FSIZE_MB", "256")) * 1024 * 1024


def _sandbox_preexec() -> None:
    """Child preexec (POSIX): clamp resource limits before the validator execs.

    Best-effort per limit — an unsupported/lower hard limit is left untouched, never
    fatal. Async-signal-safe enough for fork+exec (thin syscall wrappers only, no logging).
    """
    if resource is None:
        return
    for res, soft in (
        (resource.RLIMIT_CPU, _SANDBOX_CPU_S),
        (resource.RLIMIT_FSIZE, _SANDBOX_FSIZE_BYTES),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            hard = resource.getrlimit(res)[1]
            cap = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
            resource.setrlimit(res, (cap, hard))
        except (ValueError, OSError):
            pass


_PREEXEC = _sandbox_preexec if os.name == "posix" else None


@functools.lru_cache(maxsize=1)
def _net_sandbox_enabled() -> bool:
    """Whether to wrap validators in `unshare -rn` (no network namespace → no egress, closing
    metadata-server SSRF). DEFAULT-ON + self-probing: enabled whenever the host can actually create
    a user+net namespace, so the safe path is the default; fail-OPEN to today's no-isolation
    behavior only where unshare is unavailable/blocked (never spuriously fail validators). Opt OUT
    with DECIMAL_VALIDATOR_SANDBOX_NET in {off,0,false,no}."""
    mode = os.environ.get("DECIMAL_VALIDATOR_SANDBOX_NET", "").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return False
    if os.name != "posix" or not shutil.which("unshare"):
        return False
    try:
        probe = subprocess.run(
            ["unshare", "-rn", "--", "true"], capture_output=True, timeout=5
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _build_invocation(cmd: str) -> tuple[Any, bool]:
    """Return ``(args, shell)`` for subprocess.run, applying network isolation when enabled."""
    if _net_sandbox_enabled():
        # No network namespace → no egress (blocks metadata-server SSRF). Run the
        # author command under a fresh shell inside the namespace.
        return (["unshare", "-rn", "--", "/bin/sh", "-c", cmd], False)
    return (cmd, True)


def run_validators(
    validators: list[dict[str, Any]],
    workspace: Path,
    *,
    response_text: str = "",
    timeout_s: int = VALIDATOR_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Execute each validator command and capture pass/fail + output.

    Args:
        validators: parsed validator dicts (``cmd``, ``expect_exit_code``,
            ``label``) — the shape :func:`skillevaluation.parser.parse_eval_yaml`
            produces.
        workspace: directory the commands run in.
        response_text: the arm's final output, staged as ``response.txt``
            and ``$RESPONSE_TEXT``.
        timeout_s: per-validator timeout; a timed-out validator is recorded
            ``exit_code=-1``, ``passed=False``, ``errored=True`` (UNGRADED — see the
            exit-code contract below).

    Returns:
        One result dict per validator: ``{label, cmd, exit_code,
        expect_exit_code, stdout, stderr, passed, errored}``.

    Exit-code contract (published-OSS): a script validator is a *grader*, and a
    grader speaks a binary verdict — ``0`` means pass, ``1`` means fail. Any
    other exit code (and one the author did NOT explicitly declare via
    ``expect_exit_code``) means the grader itself broke: a bad spec, a missing
    fixture/case, an unknown mode, an unhandled exception. That is an AUTHOR
    error, not a model failure, so it is flagged ``errored=True`` (and
    ``passed=False``). Callers must EXCLUDE an errored validator from lift —
    never count it as a model fail — so a broken grader can't masquerade as
    honest no-lift. When the author opts into a non-binary code via
    ``expect_exit_code`` (e.g. ``3``), that exact code is a clean pass and is
    NOT treated as errored.

    A grader that never returns a verdict at all is the same category (0.7.0): a
    wall-clock TIMEOUT and a SPAWN FAILURE both record ``errored=True``. They used to
    record ``errored=False`` — a model fail — even though the timeout branch's own
    comment called it infrastructure. That was both dishonest (a fail asserts a verdict
    nobody produced) and inconsistent: the identical grader killed by ``RLIMIT_CPU``
    exits with a negative returncode and already errored. Timeouts are measured PER ARM,
    so the old behavior let a grader that is merely slower on one arm's longer output
    manufacture a flip.
    """
    results: list[dict[str, Any]] = []
    if not validators:
        return results

    # Do NOT inherit the full parent environment. A validator is author-controlled shell,
    # so inheriting os.environ would hand it whatever credentials the calling process
    # happens to hold. Start from a minimal allowlist; the sandbox note above covers the
    # resource clamp, the network namespace, and (its last paragraph) the throwaway-container
    # recommendation for untrusted third-party skills.
    safe_env_keys = (
        "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "DECIMAL_VALIDATOR_PY",
    )
    env = {k: os.environ[k] for k in safe_env_keys if k in os.environ}
    # HOME/TMPDIR point INTO the workspace, not the real home dir — so an author validator can't
    # read ~/.aws/credentials / ~/.config/gcloud via a $HOME-relative path.
    env["HOME"] = str(workspace)
    env["TMPDIR"] = str(workspace)
    env["RESPONSE_TEXT"] = (response_text or "")[:ENV_RESPONSE_MAX_CHARS]
    try:
        (workspace / "response.txt").write_text(response_text or "", encoding="utf-8")
    except OSError:
        logger.warning("could not stage response.txt for validators", exc_info=True)

    for v in validators:
        cmd = v.get("cmd") or ""
        expect_exit = int(v.get("expect_exit_code", 0))
        label = v.get("label") or cmd[:80]
        try:
            _args, _use_shell = _build_invocation(cmd)
            proc = subprocess.run(
                _args,
                shell=_use_shell,
                cwd=str(workspace),
                capture_output=True,
                timeout=timeout_s,
                check=False,
                env=env,
                preexec_fn=_PREEXEC,  # clamp CPU/file-size/core in the child
            )
            # A grader exit code that is neither a clean pass (0) nor a clean
            # fail (1) — and that the author did not explicitly opt into via
            # expect_exit_code — means the grader itself broke (bad spec,
            # missing case, unknown mode, unhandled exception). Flag it errored
            # so the caller excludes it from lift instead of scoring a model
            # fail. expect_exit_code==2 keeps an author's declared code clean.
            errored = proc.returncode not in (0, 1) and proc.returncode != expect_exit
            results.append(
                {
                    "label": label,
                    "cmd": cmd,
                    "exit_code": proc.returncode,
                    "expect_exit_code": expect_exit,
                    "stdout": proc.stdout.decode("utf-8", errors="replace")[:_CAPTURE_LIMIT],
                    "stderr": proc.stderr.decode("utf-8", errors="replace")[:_CAPTURE_LIMIT],
                    "passed": proc.returncode == expect_exit,
                    "errored": errored,
                }
            )
        except subprocess.TimeoutExpired:
            results.append(
                {
                    "label": label,
                    "cmd": cmd,
                    "exit_code": -1,
                    "expect_exit_code": expect_exit,
                    "stdout": "",
                    "stderr": f"validator timed out after {timeout_s}s",
                    "passed": False,
                    # UNGRADED, not a model fail (0.7.0). A wall-clock timeout means the
                    # grader never returned a verdict — recording "fail" asserts knowledge
                    # we don't have, and timing is PER ARM, so a grader that is slow on one
                    # arm's longer output manufactures a flip in whichever direction it
                    # lands. The same grader killed by RLIMIT_CPU already errored (negative
                    # returncode ∉ {0,1}), so this also makes two flavours of the same
                    # slowness classify the same way. Same rule the judge transport uses.
                    "errored": True,
                }
            )
        except (OSError, ValueError) as exc:
            # Spawn failure (E2BIG, ENOENT for the shell, NUL bytes in the
            # command…) — fail THIS validator, never the whole suite: the
            # arms already executed and cost real tokens.
            results.append(
                {
                    "label": label,
                    "cmd": cmd,
                    "exit_code": -1,
                    "expect_exit_code": expect_exit,
                    "stdout": "",
                    "stderr": f"validator could not be spawned: {exc}"[:_CAPTURE_LIMIT],
                    "passed": False,
                    # UNGRADED (0.7.0): the grader never ran, so it returned no verdict
                    # about the model. Keeps the "fail this validator, never the whole
                    # suite" contract — the case is excluded from lift, not aborted.
                    "errored": True,
                }
            )
    return results
