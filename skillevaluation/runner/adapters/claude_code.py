"""EXPERIMENTAL — drive a locally installed Claude Code CLI as the agent.

Unlike the single-shot :class:`~.llm.LLMAdapter`, this exercises the
skill in a *real agent runtime*: multi-turn, with tools, against the
case workspace. The with-skill arm stages the skill at
``<workspace>/.claude/skills/<name>/SKILL.md`` (Claude Code's project-
skill convention); the without-skill arm removes it. Turn counts and
durations come from the CLI's JSON result, so those deltas are real.

Caveats (why this is experimental):

* Requires ``claude`` on PATH, authenticated with the user's own
  subscription/key. Runs can be slow and are billed to that account.
* The CLI's JSON output shape is not a stable public contract; fields
  are read defensively and missing ones degrade to zeros.
* Headless runs execute with the user's permission settings; point it
  only at eval suites you trust.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

from .base import AdapterError, AgentAdapter, ArmExecution

logger = logging.getLogger("skillevaluation.runner.adapters.claude_code")

DEFAULT_TIMEOUT_S = 600
DEFAULT_MAX_TURNS = 12


class ClaudeCodeAdapter(AgentAdapter):
    """Invoke ``claude -p`` once per arm, inside the case workspace."""

    name = "claude-code"

    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str | None = None,
        skill_name: str = "skill-under-test",
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self.binary = binary
        self.model = model
        self.skill_name = skill_name
        self.max_turns = max_turns
        self.timeout_s = timeout_s

    @property
    def identity(self) -> str:
        # Fold every baseline-affecting knob into the cache scope: a model swap OR a --max-turns
        # change alters the (skill-independent) without-arm output, so both must invalidate the
        # cached baseline. 'default' is a coarse bucket — pass --model explicitly for a precise
        # scope if the CLI's default model could silently upgrade under you.
        return f"claude-code:{self.model or 'default'}:mt{self.max_turns}"

    def validate(self) -> None:
        if shutil.which(self.binary) is None:
            raise AdapterError(
                f"claude-code adapter: {self.binary!r} not found on PATH — install "
                "Claude Code (https://claude.com/claude-code) or pass --adapter llm"
            )

    # ── skill staging ────────────────────────────────────────────────

    def _skill_dir(self, workspace: Path) -> Path:
        return workspace / ".claude" / "skills" / self.skill_name

    def _stage_skill(self, workspace: Path, skill_body: str) -> None:
        """Write the skill as a project skill, ensuring loadable frontmatter."""
        body = skill_body.strip()
        if not body.startswith("---"):
            # Claude Code requires name/description frontmatter to list the
            # skill; synthesize a minimal block when the body ships bare.
            first_line = next(
                (ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()),
                self.skill_name,
            )
            # json.dumps makes the scalar YAML-safe — a first line with a
            # colon or quote would otherwise corrupt the frontmatter.
            body = (
                f"---\nname: {self.skill_name}\n"
                f"description: {json.dumps(first_line[:200])}\n---\n\n{body}"
            )
        dest = self._skill_dir(workspace)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(body, encoding="utf-8")

    def _unstage_skill(self, workspace: Path) -> None:
        """Remove any previously staged skill from this workspace, if present."""
        skills_root = workspace / ".claude"
        if skills_root.is_dir():
            shutil.rmtree(skills_root, ignore_errors=True)

    # ── execution ────────────────────────────────────────────────────

    def run(
        self, *, prompt: str, skill_body: str | None, workspace: Path
    ) -> ArmExecution:
        if skill_body is not None:
            self._stage_skill(workspace, skill_body)
        else:
            self._unstage_skill(workspace)

        cmd = [self.binary, "-p", prompt, "--output-format", "json",
               "--max-turns", str(self.max_turns)]
        if self.model:
            cmd += ["--model", self.model]

        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ArmExecution(
                errored=True,
                error=f"claude CLI timed out after {self.timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except OSError as exc:
            return ArmExecution(errored=True, error=f"could not spawn {self.binary}: {exc}")

        wall_ms = int((time.monotonic() - started) * 1000)
        stdout = proc.stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:300]
            return ArmExecution(
                errored=True,
                error=f"claude CLI exited {proc.returncode}: {stderr}",
                duration_ms=wall_ms,
            )

        # The CLI's JSON shape is not a stable contract — read defensively.
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            logger.debug("claude CLI emitted non-JSON despite --output-format json")
            return ArmExecution(
                final_output=stdout.strip(),
                duration_ms=wall_ms,
                turns=1,
                extra={"raw_output": True},
            )

        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        extra = {"model": self.model or "default"}
        if data.get("total_cost_usd") is not None:
            extra["total_cost_usd"] = data["total_cost_usd"]

        return ArmExecution(
            final_output=str(data.get("result") or "").strip(),
            duration_ms=int(data.get("duration_ms") or wall_ms),
            turns=int(data.get("num_turns") or 1),
            total_tokens=tokens,
            # The summary JSON doesn't itemize tool calls; leave at 0 rather
            # than fabricate. (Stream-JSON parsing could recover this later.)
            tool_call_count=0,
            errored=bool(data.get("is_error")),
            error=(str(data.get("result"))[:300] if data.get("is_error") else None),
            extra=extra,
        )
