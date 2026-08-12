"""Load a skill directory: SKILL.md (+frontmatter) and eval.yaml.

A "skill" on disk is the agentskills.io-style convention::

    my-skill/
    ├── SKILL.md      # frontmatter (name, description) + markdown body
    ├── eval.yaml     # the benchmark suite (this spec)
    └── scripts/…     # optional bundled files, staged into the sandbox

The frontmatter parser is intentionally minimal (PyYAML on the leading
``--- … ---`` block) — enough to read ``name`` without depending on any
particular SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from skillevaluation.parser import ParsedTestCase, parse_eval_yaml

# Bundled-file staging guardrails: text files under these subdirectories
# are materialized into each case workspace.
BUNDLED_DIRS = ("scripts", "references", "templates", "assets")
_BUNDLED_FILE_MAX_BYTES = 256 * 1024


class SkillDirError(ValueError):
    """The directory does not contain a runnable skill + suite."""


@dataclass
class LoadedSkill:
    """Everything the runner needs from a skill directory."""

    name: str
    body: str
    frontmatter: dict[str, Any]
    cases: list[ParsedTestCase]
    files: dict[str, str] = field(default_factory=dict)
    path: Path | None = None


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``---`` YAML frontmatter block from the body.

    Returns ``({}, text)`` when there is no frontmatter or it fails to
    parse as a mapping — a malformed block reads as body, never an error.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, parts[2].lstrip("\n")


def load_skill_dir(path: str | Path) -> LoadedSkill:
    """Load and validate a skill directory.

    Raises:
        SkillDirError: missing SKILL.md, empty body, or missing/invalid
            eval.yaml. The message says exactly what to fix.
    """
    base = Path(path).resolve()
    if not base.is_dir():
        raise SkillDirError(f"{base} is not a directory")

    skill_md = base / "SKILL.md"
    if not skill_md.is_file():
        raise SkillDirError(f"{skill_md} not found — a skill directory needs a SKILL.md")
    frontmatter, body = split_frontmatter(skill_md.read_text(encoding="utf-8"))
    if not body.strip():
        raise SkillDirError(f"{skill_md} has an empty body")

    name = str(frontmatter.get("name") or "").strip() or base.name

    eval_yaml = base / "eval.yaml"
    if not eval_yaml.is_file():
        raise SkillDirError(
            f"{eval_yaml} not found — write an eval.yaml next to SKILL.md "
            "(see spec/eval-yaml.md, packaged with this library)"
        )
    cases = parse_eval_yaml(eval_yaml.read_text(encoding="utf-8"))

    files: dict[str, str] = {}
    for sub in BUNDLED_DIRS:
        subdir = base / sub
        if not subdir.is_dir():
            continue
        for f in sorted(subdir.rglob("*")):
            if not f.is_file() or f.stat().st_size > _BUNDLED_FILE_MAX_BYTES:
                continue
            try:
                files[str(f.relative_to(base))] = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable — skip silently

    return LoadedSkill(
        name=name, body=body, frontmatter=frontmatter, cases=cases,
        files=files, path=base,
    )
