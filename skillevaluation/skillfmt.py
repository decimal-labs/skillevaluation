"""The on-disk skill format: one pure module for names, bytes, validation and hashes.

A skill on disk is a folder ``<install-name>/`` holding ``SKILL.md`` and optional files
under ``scripts/``, ``references/``, ``templates/`` and ``assets/``. This module decides
every byte of that folder, so a server that renders it, a client that checks it and a test
that replays it all agree by construction. It needs only PyYAML and the standard library.

What lives here:

- **Install names** — :func:`install_name_base`, :func:`install_name_disambiguated`,
  :func:`install_name_for`, :func:`choose_install_name`. The folder name, the frontmatter
  ``name`` and the lock key are the same string.
- **Rendering, format 1** — :class:`RenderFields` in, ``SKILL.md`` text out
  (:func:`render_skill_md`), or a whole folder with hashes and modes
  (:func:`render_bundle`). :func:`yaml_quote` writes every quoted value, and
  :func:`allowed_tools_for` applies the ``allowed-tools`` policy.
- **Identity** — :func:`trigger_fields` and :func:`render_hash` name one rendering of one
  version; :func:`body_sha256`, :func:`description_sha256` and :func:`lock_hash` are the
  other hashes a lock records.
- **Reading and checking** — :func:`split_frontmatter`, :func:`parse_skill_md_text` (a
  SafeLoader that refuses duplicate keys), :func:`validate_skill_md` and
  :func:`validate_attachment_path`, which report :class:`Problem` objects carrying a rule
  id from :data:`RULES`; :func:`taxonomy` reads category, type, scope and invocation.
- **Vectors** — ``python -m skillevaluation.skillfmt --write-vectors`` regenerates
  ``tests/vectors/skillfmt_v1.json``: inputs and expected outputs that an implementation
  in any language can replay.

Format 1 is frozen. A change to any rendered byte is a new format number, and every
supported format keeps rendering exactly as it did, because locks name their format and a
frozen install compares bytes.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import yaml

__all__ = [
    "ATTACHMENT_DIRS",
    "DESCRIPTION_MAX",
    "COMPATIBILITY_MAX",
    "FORMAT",
    "FOREIGN_TOOLS",
    "META_OPTIONAL",
    "META_REQUIRED",
    "NAME_MAX",
    "NAME_RE",
    "RESERVED",
    "RULES",
    "SUPPORTED_FORMATS",
    "TOP_LEVEL_KEYS",
    "VARIANTS",
    "Attachment",
    "Bundle",
    "BundleFile",
    "InstallNameCollision",
    "Problem",
    "RenderError",
    "RenderFields",
    "SkillFmtError",
    "SkillMdParseError",
    "ToolsPolicy",
    "Unavailable",
    "allowed_tools_for",
    "body_sha256",
    "canonical_json",
    "choose_install_name",
    "description_sha256",
    "install_name_base",
    "install_name_disambiguated",
    "install_name_for",
    "install_name_forms",
    "is_bare_bash",
    "is_error",
    "is_valid_install_name",
    "lock_hash",
    "parse_skill_md_text",
    "render_bundle",
    "render_hash",
    "render_skill_md",
    "split_frontmatter",
    "taxonomy",
    "tokenize_allowed_tools",
    "trigger_fields",
    "validate_attachment_path",
    "validate_skill_md",
    "yaml_quote",
]

# ── Constants ─────────────────────────────────────────────────────────────────────────

FORMAT = 1
SUPPORTED_FORMATS = frozenset({1})

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX = 64
_STEM_MAX = 57  # 57 + "-" + 6 hex = 64

# Names a skill may not take as its own. `install`, `search` and `check` are the
# marketplace plugin's commands; `decimalai-skills` is the plugin name a project's managed
# root loads under, and the folder name of its opt-in `.claude/skills/` link.
RESERVED = frozenset({"install", "search", "check", "decimalai-skills"})

DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500

TOP_LEVEL_KEYS = (
    "name",
    "description",
    "license",
    "compatibility",
    "allowed-tools",
    "disable-model-invocation",
    "metadata",
)
META_REQUIRED = (
    "decimalai-skill-id",
    "decimalai-version-id",
    "decimalai-version",
    "decimalai-body-sha256",
    "decimalai-name",
    "decimalai-format",
    "decimalai-invocation",
)
META_OPTIONAL = (
    "decimalai-category",
    "decimalai-skill-scope",
    "decimalai-skill-type",
    "decimalai-source",
)
INVOCATION_MODES = ("model", "user", "any")

ATTACHMENT_DIRS = ("scripts", "references", "templates", "assets")
VARIANTS = ("authored", "foreign")
FOREIGN_TOOLS = ("Read", "Grep", "Glob")

# Top-level keys that older writers used, and where format 1 keeps them instead.
_MOVED_KEYS = {
    "category": "metadata.decimalai-category",
    "skill-type": "metadata.decimalai-skill-type",
    "skill-scope": "metadata.decimalai-skill-scope",
    "invocation": "metadata.decimalai-invocation (and disable-model-invocation: true)",
    "source": "metadata.decimalai-source",
    "source_sha256": "metadata.decimalai-body-sha256",
}

_REASON_BINARY = "binary attachments are not stored"
_REASON_NESTED_SKILL_MD = (
    "a file or folder named SKILL.md inside a skill would load as a second skill, so it is "
    "never written"
)

# rule id -> (scope, what it checks). Scope "loader": at least one of the eight native
# skill loaders rejects, drops or renames a skill with this defect. Scope "sdk": this
# format's own rule. F11-F13 apply to managed folders only.
RULES: dict[str, tuple[str, str]] = {
    "F1": ("loader", "UTF-8; the file starts with a line exactly '---' and a later line "
                     "exactly '---' closes the frontmatter; no '---' inside the frontmatter; "
                     "a YAML mapping with no duplicate keys"),
    "F2": ("sdk", "top-level keys are a subset of " + ", ".join(TOP_LEVEL_KEYS)),
    "F3": ("loader", "name is a string matching ^[a-z0-9]+(-[a-z0-9]+)*$, at most 64 "
                     "characters, equal to the folder name"),
    "F4": ("loader", "description is a non-empty string of at most 1024 characters"),
    "F5": ("loader", "compatibility is a string of at most 500 characters"),
    "F6a": ("loader", "allowed-tools is a string, not a list"),
    "F6b": ("sdk", "allowed-tools has no bare Bash (Bash, Bash(*) or Bash(*:*))"),
    "F7": ("sdk", "disable-model-invocation is absent or exactly true"),
    "F8": ("loader", "metadata is a flat map of strings"),
    "F9": ("loader", "license is a string"),
    "F10": ("loader", "the body is non-empty"),
    "F11": ("sdk", "managed: the decimalai-* metadata keys are present, well-formed and "
                   "equal the lock entry"),
    "F12": ("sdk", "managed: sha256 of the body (line endings read as LF, then stripped) "
                   "equals decimalai-body-sha256 and the lock"),
    "F13": ("sdk", "managed: LF line endings, and the file ends with exactly one newline"),
    "F14": ("sdk", "an attachment path is relative and under scripts/, references/, "
                   "templates/ or assets/, with no '..', '.' or empty segment, backslash or "
                   "control character"),
    "F15": ("loader", "no attachment path has a file or folder named SKILL.md (any case, "
                      "any depth): a loader would read it as another skill"),
}


# ── Errors ────────────────────────────────────────────────────────────────────────────


class SkillFmtError(ValueError):
    """Base class for every error this module raises on bad data."""


class InstallNameCollision(SkillFmtError):  # noqa: N818 - the name the design and callers use
    """Both forms of a skill's install name are already taken in this project."""

    def __init__(self, install_name: str) -> None:
        super().__init__(
            f"install name {install_name!r} is already taken by another skill in this project"
        )
        self.install_name = install_name


class RenderError(SkillFmtError):
    """The fields cannot be rendered into a valid format-1 folder. Never serve a partial one."""

    def __init__(self, message: str, *, code: str = "render_invalid",
                 problems: Sequence[Problem] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.problems = tuple(problems)


class SkillMdParseError(SkillFmtError):
    """The text has no line-exact frontmatter, or the frontmatter is not a YAML mapping."""


# ── Install names ─────────────────────────────────────────────────────────────────────────


def _hash6(skill_id: str) -> str:
    return hashlib.sha256(skill_id.encode("utf-8")).hexdigest()[:6]


def install_name_base(name: str) -> str:
    """Project a registry name onto ``[a-z0-9-]``. Returns ``""`` when nothing Latin survives.

    ``"Acme/Refund_Policy"`` -> ``"acme-refund-policy"``; diacritics decompose and drop
    (``"Ünïcödé"`` -> ``"unicode"``); every other character becomes a separator.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def install_name_disambiguated(name: str, skill_id: str) -> str:
    """The hashed form: up to 57 characters of the base, ``-``, 6 hex of sha256(skill_id)."""
    stem = (install_name_base(name) or "skill")[:_STEM_MAX].rstrip("-")
    return f"{stem}-{_hash6(skill_id)}"


def _is_lossy(name: str) -> bool:
    """True when the base drops characters that are not Latin letters with marks."""
    s = unicodedata.normalize("NFKD", name)
    return any(ord(c) > 127 for c in s if unicodedata.category(c) != "Mn")


def _is_namespace_only(name: str, base: str) -> bool:
    """``owner/<only punctuation or non-Latin>``: the base is the namespace alone."""
    return "/" in name and base == install_name_base(name.split("/", 1)[0])


def _plain_scalar_is_itself(s: str) -> bool:
    """Whether ``key: <s>`` written plain reads back as the string ``s`` under YAML 1.1.

    ``404`` reads as an int, ``yes`` as a bool, ``2020-01-01`` as a date, ``0x1f`` as 31;
    a loader then sees a non-string name and refuses the skill.
    """
    try:
        value = yaml.load(s, Loader=_UniqueKeyLoader)
    except Exception:  # an invalid date raises ValueError from the constructor
        return False
    return isinstance(value, str) and value == s


def is_valid_install_name(s: object) -> bool:
    """A string a folder, a frontmatter ``name`` and a lock key can all be."""
    return (isinstance(s, str) and len(s) <= NAME_MAX and bool(NAME_RE.match(s))
            and _plain_scalar_is_itself(s))


def install_name_for(name: str, skill_id: str) -> str:
    """The install name with no project context (the no-project doors use this one).

    The base, unless it is empty, over 64 characters, reserved, lossy (non-Latin
    characters were dropped), only the namespace of ``owner/…``, or a plain YAML scalar
    that would not read back as itself; then the disambiguated form.
    """
    base = install_name_base(name)
    if (not base or len(base) > NAME_MAX or base in RESERVED or _is_lossy(name)
            or _is_namespace_only(name, base) or not _plain_scalar_is_itself(base)):
        return install_name_disambiguated(name, skill_id)
    return base


def install_name_forms(name: str, skill_id: str) -> tuple[str, str]:
    """``(install_name_for, install_name_disambiguated)``: the only names a skill may have."""
    return install_name_for(name, skill_id), install_name_disambiguated(name, skill_id)


def choose_install_name(name: str, skill_id: str, taken: Mapping[str, str | None]) -> str:
    """Pick the install name inside one project.

    ``taken`` maps each install name already used in the project to its skill id, or to
    ``None`` for a folder the lock does not manage. A skill already in the lock keeps its
    folder forever, under the key it was recorded with; otherwise the base form, then the
    disambiguated form. Names are compared case-insensitively, because on a macOS or
    Windows checkout ``Acme-Refund-Policy/`` and ``acme-refund-policy/`` are one folder.

    Raises:
        InstallNameCollision: the disambiguated form is taken too (two ids sharing 6 hex in
            one project, or a hand-made folder with that name in any case).
    """
    for existing, sid in taken.items():
        if sid == skill_id:
            return existing
    folded = {k.casefold() for k in taken}
    base = install_name_for(name, skill_id)
    if base.casefold() not in folded:
        return base
    alt = install_name_disambiguated(name, skill_id)
    if alt.casefold() in folded:
        raise InstallNameCollision(alt)
    return alt


# ── YAML quoting ──────────────────────────────────────────────────────────────────────────


def _needs_escape(o: int) -> bool:
    # C0 and C1 controls (C1 includes U+0085 NEL, which YAML 1.1 reads as a line break),
    # DEL, the two Unicode line separators, the BOM, the two noncharacters PyYAML's reader
    # refuses, and surrogates.
    return (o < 0x20 or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF)
            or 0xD800 <= o <= 0xDFFF)


def yaml_quote(s: str) -> str:
    """A YAML double-quoted scalar that reads back as exactly ``s`` and stays on one line.

    ``\\`` and ``"`` are backslash-escaped; control characters, U+2028, U+2029, U+FEFF and
    the characters PyYAML refuses become ``\\uXXXX``. Every third ``-`` in a run of hyphens
    becomes ``\\u002d``, so ``---`` never appears inside a frontmatter: ADK splits the whole
    file on its first ``---``. Everything else, non-ASCII included, is written literally.
    """
    out = ['"']
    dashes = 0
    for ch in s:
        if ch == "-":
            if dashes == 2:
                out.append("\\u002d")
                dashes = 0
            else:
                out.append("-")
                dashes += 1
            continue
        dashes = 0
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif _needs_escape(o):
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


# ── allowed-tools ─────────────────────────────────────────────────────────────────────────

_BARE_BASH = re.compile(r"Bash(?:\(\s*\*\s*(?::\s*\*\s*)?\))?")
# `Read(*)`, `Grep(**)`, `Glob( * )`: a read-only tool with a wildcard scope, which is the
# bare tool. Upstream lists wrote every tool this way ("Read(*), Grep(*), Bash(*)").
_FOREIGN_WILDCARD = re.compile(r"(" + "|".join(FOREIGN_TOOLS) + r")\(\s*\*\*?\s*\)")
_SCOPABLE_PATH = re.compile(r"[A-Za-z0-9._+/-]+")
_LANG_INTERP = {"python": "python3", "python3": "python3", "bash": "bash", "sh": "bash",
                "shell": "bash", "javascript": "node", "js": "node", "node": "node"}
_EXT_INTERP = {".py": "python3", ".sh": "bash", ".bash": "bash", ".js": "node"}


def _is_sep(c: str) -> bool:
    return c.isspace() or c == ","


def tokenize_allowed_tools(raw: object) -> list[str]:
    """The tool tokens of a stored ``allowed_tools`` value (a list, a string, or None).

    Repairs what importers did to comma-separated upstream strings: elements are joined
    with spaces and re-split on whitespace and commas **outside** parentheses, so
    ``["Read,", "Grep,"]`` gives ``Read``, ``Grep`` and ``["Bash(git", "log", "*)"]`` gives
    ``Bash(git log *)``. A scope that never closes is cut at its first separator. Non-string
    elements are ignored, duplicates dropped, order kept. Safe to apply twice.
    """
    if isinstance(raw, str):
        elements = [raw]
    elif isinstance(raw, (list, tuple)):
        elements = [e for e in raw if isinstance(e, str)]
    else:
        return []
    text = " ".join(elements)
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and _is_sep(text[i]):
            i += 1
        if i >= n:
            break
        start, depth = i, 0
        while i < n:
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth = max(0, depth - 1)
            elif depth == 0 and _is_sep(c):
                break
            i += 1
        if depth > 0:  # unclosed scope: keep its first word, rescan the rest
            i = start
            while i < n and not _is_sep(text[i]):
                i += 1
        tokens.append(text[start:i])
    seen: set[str] = set()
    out = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def is_bare_bash(token: str) -> bool:
    """``Bash``, ``Bash(*)`` and ``Bash(*:*)``: Bash with no real scope."""
    return bool(_BARE_BASH.fullmatch(token))


class ToolsPolicy(NamedTuple):
    """The ``allowed-tools`` a variant may write: the value (None = omit the key)."""

    value: str | None
    tokens: tuple[str, ...]
    notes: tuple[str, ...]


def _script_path_and_interp(script: Attachment | str) -> tuple[str, str | None]:
    if isinstance(script, Attachment):
        path, lang = script.path, script.execution_language
    else:
        path, lang = script, None
    interp = _LANG_INTERP.get((lang or "").strip().lower())
    if interp is None:
        dot = path.rfind(".")
        interp = _EXT_INTERP.get(path[dot:].lower()) if dot > path.rfind("/") else None
    return path, interp


def allowed_tools_for(raw: object, *, variant: str,
                      scripts: Iterable[Attachment | str] = ()) -> ToolsPolicy:
    """Apply the allowed-tools policy to a stored value.

    ``foreign`` (anything the caller's organization did not author): only ``Read``,
    ``Grep`` and ``Glob`` survive, because Claude pre-approves every listed tool. A
    wildcard scope on one of them (``Read(*)``, ``Grep(**)``) is the bare tool and is
    written bare; any other scope (``Read(./references/**)``) is dropped with the rest.
    ``authored``: every token survives as written, except a bare Bash, which becomes one
    ``Bash(<interp> ${CLAUDE_SKILL_DIR}/<path> *)`` per bundled executable script in
    ``scripts``, or is dropped when there is none. Every dropped or rewritten token is
    named in ``notes``.
    """
    _check_variant(variant)
    tokens = tokenize_allowed_tools(raw)
    kept: list[str] = []
    notes: list[str] = []
    if variant == "foreign":
        for t in tokens:
            wildcard = _FOREIGN_WILDCARD.fullmatch(t)
            if t in FOREIGN_TOOLS:
                kept.append(t)
            elif wildcard:
                kept.append(wildcard.group(1))
                notes.append(f"allowed-tools: rewrote '{t}' to '{wildcard.group(1)}' (a "
                             "wildcard scope is the bare tool)")
            else:
                notes.append(f"allowed-tools: dropped '{t}' (not authored by your organization)")
    else:
        scoped: list[str] = []
        unscoped: list[str] = []
        for path, interp in sorted((_script_path_and_interp(s) for s in scripts),
                                   key=lambda pi: pi[0]):
            if not _SCOPABLE_PATH.fullmatch(path):
                unscoped.append(path)
                continue
            prefix = f"{interp} " if interp else ""
            scoped.append(f"Bash({prefix}${{CLAUDE_SKILL_DIR}}/{path} *)")
        for t in tokens:
            if not is_bare_bash(t):
                kept.append(t)
            elif scoped:
                kept.extend(scoped)
                n = len(scoped)
                notes.append(f"allowed-tools: rewrote '{t}' to {n} scoped "
                             f"entr{'y' if n == 1 else 'ies'} for the bundled scripts")
            else:
                notes.append(f"allowed-tools: dropped '{t}' (no bundled script to scope it to)")
            if is_bare_bash(t):
                notes.extend(f"allowed-tools: did not scope '{p}' (its path has characters a "
                             "Bash rule cannot match)" for p in unscoped)
    out = list(dict.fromkeys(kept))
    return ToolsPolicy(" ".join(out) or None, tuple(out), tuple(dict.fromkeys(notes)))


def _check_variant(variant: str) -> None:
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, not {variant!r}")


def _check_format(format: int) -> None:
    if format not in SUPPORTED_FORMATS:
        raise ValueError(f"format {format!r} is not supported (supported: "
                         f"{sorted(SUPPORTED_FORMATS)})")


# ── Render inputs and outputs ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RenderFields:
    """Everything one rendering of one version reads, gathered by the caller.

    Trigger fields (their change is a new rendering, :func:`render_hash`): description,
    license (``license_spdx`` wins over ``license``), compatibility, allowed_tools,
    invocation_mode. Informational fields (copied, never part of the identity): category,
    skill_type, skill_scope, source. ``body`` is the stored body; it is written with LF
    line endings, stripped.
    """

    skill_id: str
    version_id: str
    version: int
    registry_name: str
    body: str
    description: str | None
    license: str | None = None
    license_spdx: str | None = None
    compatibility: str | Sequence[str] | None = None
    allowed_tools: str | Sequence[str] | None = None
    invocation_mode: str | None = "model"
    category: str | None = None
    skill_type: str | None = None
    skill_scope: str | None = None
    source: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RenderFields:
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown RenderFields keys: {sorted(unknown)}")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        for k in ("compatibility", "allowed_tools"):
            if isinstance(d[k], tuple):
                d[k] = list(d[k])
        return d


@dataclass(frozen=True)
class Attachment:
    """One stored bundle file. ``content`` None means binary: its bytes are not stored."""

    path: str
    content: str | None
    executable: bool = False
    execution_language: str | None = None


@dataclass(frozen=True)
class BundleFile:
    """One file to write: exact text (UTF-8), its sha256 and size in bytes, and its mode."""

    path: str
    content: str
    sha256: str
    size: int
    mode: str  # "0644" or "0755"

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size,
                "mode": self.mode, "encoding": "utf-8", "content": self.content}


@dataclass(frozen=True)
class Unavailable:
    """A file of the skill that is listed but not written, and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class Bundle:
    """A rendered skill folder: SKILL.md first, then attachments by path."""

    skill_id: str
    version_id: str
    install_name: str
    variant: str
    format: int
    render_hash: str
    body_sha256: str
    description_sha256: str
    invocation: str  # "model" or "user", as a lock records it
    files: tuple[BundleFile, ...]
    unavailable: tuple[Unavailable, ...]
    policy_notes: tuple[str, ...]

    @property
    def skill_md(self) -> str:
        return self.files[0].content

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version_id": self.version_id,
            "install_name": self.install_name,
            "render": {"format": self.format, "hash": self.render_hash,
                       "variant": self.variant},
            "body_sha256": self.body_sha256,
            "description_sha256": self.description_sha256,
            "invocation": self.invocation,
            "files": [f.to_dict() for f in self.files],
            "unavailable": [{"path": u.path, "reason": u.reason} for u in self.unavailable],
            "policy_notes": list(self.policy_notes),
        }


# ── Field normalization ───────────────────────────────────────────────────────────────

_WS_RUN = re.compile(r"\s+")


def _one_line(s: str) -> str:
    """Strip, and turn every run of whitespace that contains a line break into one space."""
    return _WS_RUN.sub(lambda m: " " if ("\n" in m.group() or "\r" in m.group())
                       else m.group(), s.strip())


def _clip(s: str, limit: int) -> str:
    """Cut to ``limit`` characters on a word boundary (a hard cut when there is none)."""
    if len(s) <= limit:
        return s
    if s[limit].isspace():
        return s[:limit].rstrip()
    cut = s[:limit]
    idx = max((i for i, c in enumerate(cut) if c.isspace()), default=-1)
    return cut[:idx].rstrip() if idx > 0 else cut


def _description(fields: RenderFields) -> tuple[str, str | None]:
    raw = fields.description
    if raw is not None and not isinstance(raw, str):
        raise RenderError(f"description must be a string, not {type(raw).__name__}")
    s = _one_line(raw or "")
    clipped = _clip(s, DESCRIPTION_MAX)
    note = (f"description: clipped from {len(s)} to {len(clipped)} characters"
            if clipped != s else None)
    return clipped, note


def _license(fields: RenderFields) -> str | None:
    for v in (fields.license_spdx, fields.license):
        if v is not None and not isinstance(v, str):
            raise RenderError(f"license must be a string, not {type(v).__name__}")
        if v and v.strip():
            return v.strip()
    return None


def _compatibility(fields: RenderFields) -> tuple[str | None, str | None]:
    raw = fields.compatibility
    if raw is None:
        return None, None
    if isinstance(raw, str):
        s = _one_line(raw)
    elif isinstance(raw, (list, tuple)) and all(isinstance(e, str) for e in raw):
        s = "; ".join(_one_line(e) for e in raw if e.strip())
    else:
        raise RenderError("compatibility must be a string or a list of strings, not "
                          f"{type(raw).__name__}")
    if not s:
        return None, None
    clipped = _clip(s, COMPATIBILITY_MAX)
    note = (f"compatibility: clipped from {len(s)} to {len(clipped)} characters"
            if clipped != s else None)
    return clipped, note


def _invocation(fields: RenderFields) -> str:
    v = (fields.invocation_mode or "").strip().lower()
    return v if v in INVOCATION_MODES else "model"


def _info(v: object, name: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise RenderError(f"{name} must be a string, not {type(v).__name__}")
    return v.strip() or None


def trigger_fields(fields: RenderFields) -> dict[str, Any]:
    """The fields whose change makes a new rendering of the same version, as written.

    Values are normalized and clipped first, so the identity covers exactly what reaches
    the file; ``allowed_tools`` is the tokenized stored list (before any variant policy).
    """
    desc, _ = _description(fields)
    compat, _ = _compatibility(fields)
    return {
        "allowed_tools": tokenize_allowed_tools(fields.allowed_tools),
        "compatibility": compat,
        "description": desc,
        "invocation_mode": _invocation(fields),
        "license": _license(fields),
    }


# ── Hashes ────────────────────────────────────────────────────────────────────────────


def _sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, non-ASCII literal, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def render_hash(fields: RenderFields) -> str:
    """sha256 of the canonical JSON of :func:`trigger_fields`."""
    return _sha256(canonical_json(trigger_fields(fields)))


def _lf(text: str) -> str:
    """CRLF, then any lone CR, as LF: the translation of Python's universal newlines."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def body_sha256(body: str) -> str:
    """sha256 of the body as format 1 writes it: line endings as LF, then ``str.strip()``.

    A body with no CR hashes as ``sha256(body.strip())``. Python's strip also strips
    U+2028 and friends; never Postgres ``btrim``. The stored body, the written body and a
    CRLF checkout of the written file all give the same value.
    """
    return _sha256(_lf(body).strip())


def description_sha256(description: str) -> str:
    """sha256 of the rendered description string."""
    return _sha256(description)


def lock_hash(root: str, skills: Mapping[str, Mapping[str, Any]]) -> str:
    """The lock's own hash: every entry except ``installed_at``, with the root.

    Two machines installing the same lock get the same value, so the plugin version
    derived from it does not churn.
    """
    payload = {"root": root,
               "skills": {k: {kk: vv for kk, vv in e.items() if kk != "installed_at"}
                          for k, e in skills.items()}}
    return _sha256(canonical_json(payload))


# ── Rendering ─────────────────────────────────────────────────────────────────────────────


def _render(fields: RenderFields, *, install_name: str, variant: str,
            scripts: Iterable[Attachment | str], format: int) -> tuple[str, list[str]]:
    _check_format(format)
    _check_variant(variant)
    for attr in ("skill_id", "version_id", "registry_name"):
        v = getattr(fields, attr)
        if not isinstance(v, str) or not v.strip():
            raise RenderError(f"{attr} must be a non-empty string")
    if not isinstance(fields.version, int) or isinstance(fields.version, bool) \
            or fields.version < 1:
        raise RenderError(f"version must be a positive integer, not {fields.version!r}")
    if not is_valid_install_name(install_name):
        raise RenderError(f"install name {install_name!r} is not a valid folder name")
    if not isinstance(fields.body, str) or not fields.body.strip():
        raise RenderError("the body is empty; a skill with no body is never written")

    desc, desc_note = _description(fields)
    if not desc:
        raise RenderError("the description is empty; every loader requires one")
    lic = _license(fields)
    compat, compat_note = _compatibility(fields)
    tools = allowed_tools_for(fields.allowed_tools, variant=variant, scripts=scripts)
    inv = _invocation(fields)
    body = _lf(fields.body).strip()
    body_note = ("body: wrote CR and CRLF line endings as LF"
                 if "\r" in fields.body.strip() else None)

    meta: dict[str, str] = {
        "decimalai-skill-id": fields.skill_id,
        "decimalai-version-id": fields.version_id,
        "decimalai-version": str(fields.version),
        "decimalai-body-sha256": body_sha256(fields.body),
        "decimalai-name": fields.registry_name,
        "decimalai-format": str(format),
        "decimalai-invocation": inv,
    }
    for key, attr in (("decimalai-category", "category"),
                      ("decimalai-skill-scope", "skill_scope"),
                      ("decimalai-skill-type", "skill_type"),
                      ("decimalai-source", "source")):
        v = _info(getattr(fields, attr), attr)
        if v:
            meta[key] = v

    expected: dict[str, Any] = {"name": install_name, "description": desc}
    lines = ["---", f"name: {install_name}", f"description: {yaml_quote(desc)}"]
    if lic:
        expected["license"] = lic
        lines.append(f"license: {yaml_quote(lic)}")
    if compat:
        expected["compatibility"] = compat
        lines.append(f"compatibility: {yaml_quote(compat)}")
    if tools.value:
        expected["allowed-tools"] = tools.value
        lines.append(f"allowed-tools: {yaml_quote(tools.value)}")
    if inv == "user":
        expected["disable-model-invocation"] = True
        lines.append("disable-model-invocation: true")
    expected["metadata"] = meta
    lines.append("metadata:")
    lines += [f"  {k}: {yaml_quote(v)}" for k, v in meta.items()]
    lines.append("---")
    text = "\n".join(lines) + "\n" + body + "\n"

    _self_check(text, expected=expected, body=body, install_name=install_name,
                lock={"name": fields.registry_name, "skill_id": fields.skill_id,
                      "version_id": fields.version_id, "version": fields.version,
                      "render": {"format": format}, "body_sha256": meta["decimalai-body-sha256"],
                      "invocation": "user" if inv == "user" else "model"})
    notes = [n for n in (desc_note, compat_note, body_note) if n] + list(tools.notes)
    return text, notes


def _self_check(text: str, *, expected: Mapping[str, Any], body: str, install_name: str,
                lock: Mapping[str, Any]) -> None:
    """Read the rendered text back and refuse it unless every value is what went in."""
    try:
        fm, parsed_body = parse_skill_md_text(text)
    except SkillMdParseError as exc:
        raise RenderError(f"rendered SKILL.md does not parse: {exc}") from exc
    if fm != expected or list(fm) != list(expected) \
            or list(fm.get("metadata") or {}) != list(expected["metadata"]):
        raise RenderError("rendered SKILL.md does not read back equal to its fields")
    if parsed_body.strip() != body:
        raise RenderError("rendered SKILL.md body does not read back equal")
    problems = validate_skill_md(text, folder_name=install_name, managed=True, expect=lock)
    if problems:
        raise RenderError("rendered SKILL.md fails validation: "
                          + "; ".join(f"{p.rule}: {p.message}" for p in problems),
                          problems=problems)


def render_skill_md(fields: RenderFields, *, install_name: str, variant: str,
                    scripts: Iterable[Attachment | str] = (), format: int = FORMAT) -> str:
    """The SKILL.md text for one rendering. ``scripts`` scopes an authored bare Bash.

    Raises:
        RenderError: the fields cannot make a valid file (empty description or body, a bad
            install name, a value that would not read back). Nothing partial is returned.
        ValueError: an unknown variant or an unsupported format.
    """
    text, _ = _render(fields, install_name=install_name, variant=variant, scripts=scripts,
                      format=format)
    return text


def _coerce_attachment(a: Attachment | Mapping[str, Any]) -> Attachment:
    return a if isinstance(a, Attachment) else Attachment(**dict(a))


def render_bundle(fields: RenderFields, files: Iterable[Attachment | Mapping[str, Any]], *,
                  install_name: str, variant: str, format: int = FORMAT) -> Bundle:
    """Render a whole folder: SKILL.md plus attachments, with sha256, size and mode.

    ``files`` is the version's final attachment set (the caller has already let a
    version-pinned row win over a shared one). An unsafe path (F14), or two paths that
    would land on the same file, is a :class:`RenderError`, whatever the file is named.
    A safe path through a file or folder named SKILL.md (F15) is listed in
    ``unavailable`` and never written; so is a binary file.
    """
    unavailable: list[Unavailable] = []
    written: list[Attachment] = []
    seen: dict[str, str] = {}
    for raw in files:
        att = _coerce_attachment(raw)
        path = att.path
        problems = validate_attachment_path(path)
        unsafe = [p for p in problems if p.rule == "F14"]
        if unsafe:
            raise RenderError(f"attachment path {path!r} is unsafe: {unsafe[0].message}",
                              problems=problems)
        if problems:  # F15 alone: a safe path that a loader would read as another skill
            unavailable.append(Unavailable(path, _REASON_NESTED_SKILL_MD))
            continue
        key = path.casefold()
        if key in seen:
            raise RenderError(f"attachment path {path!r} duplicates {seen[key]!r}")
        seen[key] = path
        if att.content is None:
            unavailable.append(Unavailable(path, _REASON_BINARY))
        elif not isinstance(att.content, str):
            raise RenderError(f"attachment {path!r} content must be text or None")
        else:
            written.append(att)
    for key, path in seen.items():
        parts = key.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent in seen:
                raise RenderError(f"attachment {seen[parent]!r} is a file, but {path!r} "
                                  "needs it to be a directory")

    scripts = [a for a in written if a.executable and a.path.startswith("scripts/")]
    text, notes = _render(fields, install_name=install_name, variant=variant,
                          scripts=scripts, format=format)
    out = [_bundle_file("SKILL.md", text, executable=False)]
    out += [_bundle_file(a.path, a.content or "", executable=a.executable)
            for a in sorted(written, key=lambda a: a.path)]
    desc, _ = _description(fields)
    return Bundle(
        skill_id=fields.skill_id,
        version_id=fields.version_id,
        install_name=install_name,
        variant=variant,
        format=format,
        render_hash=render_hash(fields),
        body_sha256=body_sha256(fields.body),
        description_sha256=description_sha256(desc),
        invocation="user" if _invocation(fields) == "user" else "model",
        files=tuple(out),
        unavailable=tuple(sorted(unavailable, key=lambda u: u.path)),
        policy_notes=tuple(notes),
    )


def _bundle_file(path: str, content: str, *, executable: bool) -> BundleFile:
    data = content.encode("utf-8")
    return BundleFile(path=path, content=content, sha256=_sha256(data), size=len(data),
                      mode="0755" if executable else "0644")


# ── Parsing ───────────────────────────────────────────────────────────────────────────


class _DuplicateKeyError(yaml.constructor.ConstructorError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """PyYAML's SafeLoader, except that a mapping may not repeat a key."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            hash(key)
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found an unhashable key", key_node.start_mark) from exc
        if key in mapping:
            raise _DuplicateKeyError("while constructing a mapping", node.start_mark,
                                     f"found duplicate key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                                 _construct_unique_mapping)


def _is_fence(line: str) -> bool:
    return line == "---" or line == "---\r"


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """``(frontmatter_text, body)``, or None when there is no line-exact frontmatter.

    The first line must be exactly ``---`` and the frontmatter ends at the next line that
    is exactly ``---`` (a trailing CR is tolerated on both). A ``---`` inside a value is
    not a fence.
    """
    nl = text.find("\n")
    if nl == -1 or not _is_fence(text[:nl]):
        return None
    start = pos = nl + 1
    while pos <= len(text):
        end = text.find("\n", pos)
        line = text[pos:] if end == -1 else text[pos:end]
        if _is_fence(line):
            return text[start:pos], ("" if end == -1 else text[end + 1:])
        if end == -1:
            return None
        pos = end + 1
    return None


def parse_skill_md_text(text: str) -> tuple[dict[str, Any], str]:
    """Parse ``SKILL.md`` text into ``(frontmatter, body)``.

    Raises:
        SkillMdParseError: no line-exact frontmatter; invalid YAML; a duplicate key; or a
            frontmatter that is not a mapping.
    """
    parts = split_frontmatter(text)
    if parts is None:
        raise SkillMdParseError("SKILL.md must start with a line '---', and a later line "
                                "'---' must close the frontmatter")
    fm_text, body = parts
    try:
        data = yaml.load(fm_text, Loader=_UniqueKeyLoader)
    except _DuplicateKeyError as exc:
        raise SkillMdParseError(f"the frontmatter has a {exc.problem}") from exc
    except Exception as exc:  # YAMLError, or a constructor ValueError (an invalid date)
        raise SkillMdParseError(f"the frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillMdParseError("the frontmatter is not a YAML mapping")
    return data, body


# ── Validation ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Problem:
    """One defect. ``scope`` is ``"loader"`` or ``"sdk"`` (see :data:`RULES`)."""

    rule: str
    scope: str
    message: str


def _p(rule: str, message: str) -> Problem:
    return Problem(rule, RULES[rule][0], message)


def is_error(problem: Problem, *, managed: bool, strict: bool = False) -> bool:
    """Managed folders: every problem is an error. Others: loader rules are errors, sdk
    rules are warnings unless ``strict``."""
    return managed or strict or problem.scope == "loader"


def _type_name(v: object) -> str:
    return "null" if v is None else type(v).__name__


def validate_skill_md(text: str | bytes, *, folder_name: str | None = None,
                      managed: bool = False,
                      expect: Mapping[str, Any] | None = None) -> list[Problem]:
    """Every problem in one SKILL.md, by rule (see :data:`RULES`).

    ``folder_name`` is the name of the folder that holds the file (None skips F3's folder
    comparison). ``managed`` adds the rules for a folder a lock manages (F11-F13), and
    ``expect`` is its lock entry: ``name`` (registry name), ``skill_id``, ``version_id``,
    ``version``, ``render.format``, ``body_sha256`` and ``invocation`` are compared when
    present. Returns ``[]`` for a valid file.
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            return [_p("F1", f"SKILL.md is not UTF-8 ({exc.reason} at byte {exc.start})")]
    parts = split_frontmatter(text)
    if parts is None:
        return [_p("F1", "SKILL.md must start with a line exactly '---' (no BOM, no "
                         "indent), and a later line exactly '---' must close the frontmatter")]
    problems: list[Problem] = []
    if "---" in parts[0]:
        problems.append(_p("F1", "the frontmatter contains '---' before its closing fence; "
                                 "a loader that splits on '---' (ADK) cuts it there"))
    try:
        fm, body = parse_skill_md_text(text)
    except SkillMdParseError as exc:
        return problems + [_p("F1", str(exc))]

    for key in fm:
        if key not in TOP_LEVEL_KEYS:
            moved = _MOVED_KEYS.get(key) if isinstance(key, str) else None
            hint = f"; format 1 keeps it as {moved}" if moved else ""
            problems.append(_p("F2", f"unknown top-level key {key!r}{hint}"))

    name = fm.get("name")
    if "name" not in fm:
        problems.append(_p("F3", "name is missing"))
    elif not isinstance(name, str):
        problems.append(_p("F3", f"name must be a string; YAML read {_type_name(name)} "
                                 f"{name!r} (quote it, or pick another name)"))
    else:
        if not NAME_RE.match(name) or len(name) > NAME_MAX:
            problems.append(_p("F3", f"name {name!r} must be lowercase letters, digits and "
                                     "single hyphens, at most 64 characters"))
        if folder_name is not None and name != folder_name:
            problems.append(_p("F3", f"name {name!r} differs from its folder "
                                     f"{folder_name!r}"))

    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        problems.append(_p("F4", "description must be a non-empty string"
                                 + ("" if "description" in fm else " (it is missing)")))
    elif len(desc) > DESCRIPTION_MAX:
        problems.append(_p("F4", f"description is {len(desc)} characters; the limit is "
                                 f"{DESCRIPTION_MAX}"))

    if "compatibility" in fm:
        compat = fm["compatibility"]
        if not isinstance(compat, str):
            problems.append(_p("F5", f"compatibility must be a string, not "
                                     f"{_type_name(compat)} (join a list with '; ')"))
        elif len(compat) > COMPATIBILITY_MAX:
            problems.append(_p("F5", f"compatibility is {len(compat)} characters; the limit "
                                     f"is {COMPATIBILITY_MAX}"))

    if "allowed-tools" in fm:
        tools = fm["allowed-tools"]
        if not isinstance(tools, str):
            problems.append(_p("F6a", "allowed-tools must be one space-separated string, "
                                      f"not {_type_name(tools)}"))
        else:
            bare = [t for t in tokenize_allowed_tools(tools) if is_bare_bash(t)]
            if bare:
                problems.append(_p("F6b", f"allowed-tools pre-approves unrestricted Bash "
                                          f"({', '.join(bare)}); scope it to a script"))

    if "disable-model-invocation" in fm and fm["disable-model-invocation"] is not True:
        problems.append(_p("F7", "disable-model-invocation must be exactly true, or absent"))

    meta = fm.get("metadata")
    if "metadata" in fm:
        if not isinstance(meta, dict):
            problems.append(_p("F8", f"metadata must be a map, not {_type_name(meta)}"))
        else:
            for k, v in meta.items():
                if not isinstance(k, str):
                    problems.append(_p("F8", f"metadata key {k!r} must be a string"))
                elif isinstance(v, dict):
                    problems.append(_p("F8", f"metadata.{k} is a map; metadata must be flat"))
                elif not isinstance(v, str):
                    problems.append(_p("F8", f"metadata.{k} is {_type_name(v)}; quote it as "
                                             "a string"))

    if "license" in fm and not isinstance(fm["license"], str):
        problems.append(_p("F9", f"license must be a string, not {_type_name(fm['license'])}"))

    if not body.strip():
        problems.append(_p("F10", "the body is empty"))

    if managed:
        problems += _managed_problems(text, fm, body, meta, expect)
    return sorted(problems, key=lambda p: _RULE_ORDER[p.rule])


_RULE_ORDER = {r: i for i, r in enumerate(RULES)}
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _managed_problems(text: str, fm: Mapping[Any, Any], body: str, meta: object,
                      expect: Mapping[str, Any] | None) -> list[Problem]:
    out: list[Problem] = []
    m: Mapping[Any, Any] = meta if isinstance(meta, dict) else {}
    missing = [k for k in META_REQUIRED if not isinstance(m.get(k), str) or not m.get(k)]
    if missing:
        out.append(_p("F11", f"metadata lacks {', '.join(missing)}"))
    fmt = m.get("decimalai-format")
    if isinstance(fmt, str) and fmt and fmt not in {str(f) for f in SUPPORTED_FORMATS}:
        out.append(_p("F11", f"decimalai-format {fmt!r} is not a supported format"))
    ver = m.get("decimalai-version")
    if isinstance(ver, str) and ver and not re.fullmatch(r"[1-9][0-9]*", ver):
        out.append(_p("F11", f"decimalai-version {ver!r} is not a positive integer"))
    stamp = m.get("decimalai-body-sha256")
    if isinstance(stamp, str) and stamp and not _HEX64.fullmatch(stamp):
        out.append(_p("F11", "decimalai-body-sha256 is not 64 lowercase hex"))
    inv = m.get("decimalai-invocation")
    if isinstance(inv, str) and inv:
        if inv not in INVOCATION_MODES:
            out.append(_p("F11", f"decimalai-invocation {inv!r} is not one of "
                                 f"{', '.join(INVOCATION_MODES)}"))
        elif (inv == "user") != (fm.get("disable-model-invocation") is True):
            out.append(_p("F11", "decimalai-invocation and disable-model-invocation disagree"))
    if expect is not None:
        render = expect.get("render")
        checks = [
            ("decimalai-skill-id", expect.get("skill_id")),
            ("decimalai-version-id", expect.get("version_id")),
            ("decimalai-version", expect.get("version")),
            ("decimalai-name", expect.get("name")),
            ("decimalai-format", render.get("format") if isinstance(render, Mapping) else None),
        ]
        for key, want in checks:
            if want is not None and m.get(key) != str(want):
                out.append(_p("F11", f"metadata.{key} is {m.get(key)!r}; the lock says "
                                     f"{str(want)!r}"))
        want_inv = expect.get("invocation")
        if want_inv is not None and isinstance(inv, str) and inv:
            got_inv = "user" if inv == "user" else "model"
            if got_inv != want_inv:
                out.append(_p("F11", f"the file is invocation {got_inv!r}; the lock says "
                                     f"{want_inv!r}"))
    actual = body_sha256(body)
    if isinstance(stamp, str) and stamp and stamp != actual:
        out.append(_p("F12", "the body does not hash to metadata.decimalai-body-sha256 "
                             "(edited after install?)"))
    want_body = expect.get("body_sha256") if expect is not None else None
    if want_body is not None and want_body != actual:
        out.append(_p("F12", "the body does not hash to the lock's body_sha256"))
    if "\r" in text:
        out.append(_p("F13", "the file has CR characters; managed files use LF only"))
    if not text.endswith("\n") or text.endswith("\n\n"):
        out.append(_p("F13", "the file must end with exactly one newline"))
    return out


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_attachment_path(path: object) -> list[Problem]:
    """Problems with one bundle file path (F14 unsafe, F15 a SKILL.md by another name)."""
    if not isinstance(path, str) or not path:
        return [_p("F14", f"attachment path must be a non-empty string, not {path!r}")]
    out: list[Problem] = []
    if any(seg.casefold() == "skill.md" for seg in path.replace("\\", "/").split("/")):
        out.append(_p("F15", f"attachment {path!r} has a file or folder named SKILL.md; a "
                             "loader that searches for SKILL.md would read it as a second "
                             "skill"))
    segments = path.split("/")
    if "\\" in path or _CONTROL.search(path):
        out.append(_p("F14", f"attachment path {path!r} contains a backslash or a control "
                             "character"))
    elif path.startswith("/"):
        out.append(_p("F14", f"attachment path {path!r} is absolute"))
    elif any(s in ("", ".", "..") for s in segments):
        out.append(_p("F14", f"attachment path {path!r} has an empty, '.' or '..' segment"))
    elif len(segments) < 2 or segments[0] not in ATTACHMENT_DIRS:
        out.append(_p("F14", f"attachment path {path!r} must sit under one of "
                             f"{', '.join(d + '/' for d in ATTACHMENT_DIRS)}"))
    return out


# ── Taxonomy ──────────────────────────────────────────────────────────────────────────


def taxonomy(fm: Mapping[str, Any]) -> dict[str, str | None]:
    """Category, skill type, skill scope and invocation from a frontmatter dict.

    ``metadata.decimalai-*`` is read first, then the legacy top-level keys (``category``,
    ``skill-type``, ``skill-scope``, ``invocation``); invocation finally falls back to
    ``disable-model-invocation: true`` -> ``user``. Values are stripped strings, or None
    when absent, empty or not a string. Invocation is lowercased and must be one of
    model/user/any; type and scope are returned as written, for the caller to normalize.
    """
    meta_raw = fm.get("metadata")
    meta: Mapping[str, Any] = meta_raw if isinstance(meta_raw, Mapping) else {}

    def pick(meta_key: str, legacy_key: str,
             accept: tuple[str, ...] | None = None) -> str | None:
        for v in (meta.get(meta_key), fm.get(legacy_key)):
            if isinstance(v, str) and v.strip():
                s = v.strip()
                if accept is None:
                    return s
                if s.lower() in accept:
                    return s.lower()
        return None

    invocation = pick("decimalai-invocation", "invocation", INVOCATION_MODES)
    if invocation is None and fm.get("disable-model-invocation") is True:
        invocation = "user"
    return {
        "category": pick("decimalai-category", "category"),
        "skill_type": pick("decimalai-skill-type", "skill-type"),
        "skill_scope": pick("decimalai-skill-scope", "skill-scope"),
        "invocation": invocation,
    }


# ── Vectors ───────────────────────────────────────────────────────────────────────────

VECTORS_DESCRIPTION = (
    "Golden vectors for SKILL.md format 1, generated by `python -m skillevaluation.skillfmt "
    "--write-vectors`. Each section lists inputs and the outputs an implementation must "
    "produce. Replay: install_names (install_name_base / _disambiguated / _for), "
    "choose_install_name (taken = install name -> skill id or null), yaml_quote, "
    "allowed_tools (tokens, value, notes), render (fields + attachments -> bundle, with every "
    "file's exact content, sha256 and mode, plus trigger_fields), render_errors (must be "
    "refused), validate (sorted [rule, scope] pairs), attachment_paths, lock_hash, taxonomy "
    "and body_sha256. Rule ids and scopes are in `rules`."
)


def _ref_body(title: str) -> str:
    return (
        f"\n# {title}\n\nSentinel: NATIVE-SENTINEL-7f3a\n\n"
        "- Returned within 30 days: full refund.\n- 31-60 days: store credit.\n"
        "- After 60 days: none.\n\n"
        "For fees read references/fees.md. To compute a refund, run "
        "scripts/compute_refund.py <amount> <days>.\n\n"
    )


_REF_FEES = ("# Restocking fees\n\n| category | fee |\n|---|---|\n| electronics | 15% |\n"
             "| apparel | 0% |\n")
_REF_SCRIPT = (
    "#!/usr/bin/env python3\n"
    '"""compute_refund.py <amount> <days>"""\n'
    "import sys\n"
    "amount, days = float(sys.argv[1]), int(sys.argv[2])\n"
    "print('REFUND full refund: %.2f' % amount if days <= 30 else\n"
    "      'REFUND store credit: %.2f' % amount if days <= 60 else 'REFUND none')\n"
)
_REF_LONG = "some-github-owner/an-extremely-long-imported-skill-name-that-exceeds-the-limit"


def _ref_uuid(prefix: int, i: int) -> str:
    h = f"{prefix + i:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _reference_cases() -> list[dict[str, Any]]:
    specs = [
        ("reference-acme-refund-policy", "acme/refund-policy", "Acme refund policy", False,
         ["Read", "Bash"], True, "authored"),
        ("reference-acme-returns-desk", "acme/returns-desk", "Acme returns desk", True,
         None, False, "authored"),
        ("reference-long-imported-name", _REF_LONG, "Imported refund policy", False,
         None, True, "foreign"),
    ]
    cases = []
    for i, (cid, registry, title, user_only, tools, has_files, variant) in enumerate(specs):
        sid, vid = _ref_uuid(0x5EED0000, i), _ref_uuid(0xF00D0000, i)
        fields = RenderFields(
            skill_id=sid, version_id=vid, version=3, registry_name=registry,
            body=_ref_body(title),
            description=(f"Apply {title} rules when a customer asks for a refund, a return or "
                         "store credit."),
            license="Apache-2.0", compatibility="Requires python3", allowed_tools=tools,
            invocation_mode="user" if user_only else "model", category="support")
        atts = ([Attachment("references/fees.md", _REF_FEES),
                 Attachment("scripts/compute_refund.py", _REF_SCRIPT, executable=True,
                            execution_language="python")] if has_files else [])
        cases.append({"id": cid, "fields": fields, "attachments": atts,
                      "install_name": install_name_for(registry, sid), "variant": variant})
    return cases


def _extra_render_cases() -> list[dict[str, Any]]:
    sid = "9a7b0000-0000-4000-8000-00000000000a"
    base = RenderFields(skill_id=sid, version_id="9a7b0000-0000-4000-8000-0000000000b1",
                        version=7, registry_name="acme/tricky — “quotes”", body="# Body\n\nText.",
                        description="x")
    rep = dataclasses.replace
    return [
        {"id": "tricky-description-public", "variant": "foreign", "attachments": [],
         "install_name": install_name_for(base.registry_name, sid),
         "fields": rep(base, description=(
             '  - Use when "quotes", back\\slash, a: b, # hash,\n  line\u2028sep, '
             "nel\u0085, bom\ufeff, tab\t, emoji 🎉, 中文, and a --- rule ----  "),
             license="Apache-2.0 OR MIT", category="security", skill_type="capability",
             skill_scope="public", invocation_mode="any",
             source="https://app.decimal.ai/s/acme-tricky@7/SKILL.md")},
        {"id": "clipped-long-values", "variant": "foreign", "attachments": [],
         "install_name": "clipped-long-values",
         "fields": rep(base, description=("word " * 300).strip(), compatibility=["c " * 300,
                                                                                 "uv"])},
        {"id": "foreign-damaged-allowed-tools", "variant": "foreign",
         "install_name": "damaged-tools",
         "attachments": [Attachment("scripts/run.py", "print(1)\n", executable=True)],
         "fields": rep(base, allowed_tools=["Read,", "Grep,", "Bash(git", "log", "*)", "Write",
                                            "mcp__github__create_issue"])},
        {"id": "authored-bare-bash-scoped", "variant": "authored",
         "install_name": "authored-tools",
         "attachments": [Attachment("scripts/a.py", "print(1)\n", executable=True),
                         Attachment("scripts/b.sh", "echo b\n", executable=True,
                                    execution_language="bash"),
                         Attachment("scripts/notes.txt", "not executable\n"),
                         Attachment("references/r.md", "# R\n")],
         "fields": rep(base, allowed_tools=["Read", "Bash(*)", "Write"], license_spdx="MIT",
                       license="Some long license text")},
        {"id": "attachments-binary-and-nested-skill-md", "variant": "foreign",
         "install_name": "attachments",
         "attachments": [Attachment("assets/logo.png", None),
                         Attachment("templates/SKILL.md", "---\nname: x\n---\nx\n"),
                         Attachment("templates/t.md", "line\r\nline\r\n"),
                         Attachment("references/deep/er/n.md", "ñ\n"),
                         Attachment("references/SKILL.md/notes.md", "under a folder\n")],
         "fields": rep(base, invocation_mode="user")},
        # CRLF and a lone CR in the stored body: written as LF, hashed as written.
        {"id": "crlf-body", "variant": "foreign", "install_name": "crlf-body",
         "attachments": [],
         "fields": rep(base, body="\r\n# Body\r\n\r\nLine one\r\nline two\rline three\r\n")},
    ]


def _render_error_cases() -> list[dict[str, Any]]:
    base = _reference_cases()[0]
    rep = dataclasses.replace
    out = []
    for cid, change, atts, name in [
        ("empty-description", {"description": " \n "}, [], None),
        ("empty-body", {"body": "\n\n"}, [], None),
        ("unsafe-path", {}, [Attachment("../escape.md", "x")], None),
        # Path safety comes before the SKILL.md skip, so these are refused, never listed.
        ("unsafe-path-named-skill-md", {}, [Attachment("../../SKILL.md", "x")], None),
        ("absolute-path-named-skill-md", {}, [Attachment("/etc/SKILL.md", "x")], None),
        ("path-outside-bundle-dirs", {}, [Attachment("eval.yaml", "x")], None),
        ("case-insensitive-duplicate", {}, [Attachment("references/A.md", "a"),
                                            Attachment("references/a.md", "b")], None),
        ("file-where-a-directory-goes", {}, [Attachment("scripts/tool", "a"),
                                             Attachment("scripts/tool/x.py", "b")], None),
        ("install-name-not-a-folder-name", {}, [], "acme/refund-policy"),
        ("install-name-read-as-a-number", {}, [], "404"),
    ]:
        out.append({"id": cid, "fields": rep(base["fields"], **change), "attachments": atts,
                    "install_name": name or base["install_name"], "variant": "authored"})
    return out


def _validate_cases() -> list[dict[str, Any]]:
    ref = _reference_cases()[0]
    text = render_skill_md(ref["fields"], install_name=ref["install_name"],
                           variant=ref["variant"], scripts=ref["attachments"][1:])
    desc_line = f"description: {yaml_quote(_description(ref['fields'])[0])}"
    allowed = ('allowed-tools: "Read Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/'
               'compute_refund.py *)"')
    lock = {"name": ref["fields"].registry_name, "skill_id": ref["fields"].skill_id,
            "version_id": ref["fields"].version_id, "version": 3, "render": {"format": 1},
            "body_sha256": body_sha256(ref["fields"].body), "invocation": "model"}

    def mut(old: str, new: str) -> str:
        if old not in text:  # pragma: no cover - guards the generator itself
            raise AssertionError(old)
        return text.replace(old, new, 1)

    folder = "acme-refund-policy"
    cases: list[tuple[str, str, str | None, bool, Mapping[str, Any] | None]] = [
        ("baseline", text, folder, False, None),
        ("baseline-managed", text, folder, True, lock),
        ("list allowed-tools", mut(allowed, "allowed-tools:\n  - Read\n  - Bash"), folder,
         False, None),
        ("list compatibility", mut('compatibility: "Requires python3"',
                                   'compatibility:\n  - "python>=3.10"'), folder, False, None),
        ("nested metadata", mut("metadata:\n", 'metadata:\n  decimalai:\n    version: "3"\n'),
         folder, False, None),
        ("unquoted number in metadata", mut('decimalai-version: "3"', "decimalai-version: 3"),
         folder, False, None),
        ("namespaced name", mut("name: acme-refund-policy", "name: acme/refund-policy"),
         folder, False, None),
        ("folder != name", text, "acme-refund-policy-v3", False, None),
        ("unknown top-level key", mut("metadata:\n", 'decimalai-version-id: "x"\nmetadata:\n'),
         folder, False, None),
        ("uppercase name", mut("name: acme-refund-policy", "name: Acme-Refund-Policy"), folder,
         False, None),
        ("name over 64", mut("name: acme-refund-policy", "name: " + "a" * 65), "a" * 65,
         False, None),
        ("double hyphen", mut("name: acme-refund-policy", "name: acme--refund"),
         "acme--refund", False, None),
        ("name read as a number", mut("name: acme-refund-policy", "name: 404"), "404", False,
         None),
        ("description over 1024", mut(desc_line, 'description: "' + "d" * 1025 + '"'), folder,
         False, None),
        ("empty description", mut(desc_line, 'description: ""'), folder, False, None),
        ("compatibility over 500", mut('compatibility: "Requires python3"',
                                       'compatibility: "' + "c" * 501 + '"'), folder, False,
         None),
        ("license list", mut('license: "Apache-2.0"', "license:\n  - MIT"), folder, False, None),
        ("metadata null value", mut('decimalai-category: "support"', "decimalai-category:"),
         folder, False, None),
        ("empty body", text.split("---\n# ")[0] + "---\n", folder, False, None),
        ("'---' inside a value (ADK splits on it)", mut(desc_line, 'description: "a --- b"'),
         folder, False, None),
        ("duplicate key", mut("metadata:\n", "name: again\nmetadata:\n"), folder, False, None),
        ("no closing fence", text.replace("---\n# Acme", "# Acme"), folder, False, None),
        ("byte-order mark", "\ufeff" + text, folder, False, None),
        ("bare Bash", mut(allowed, "allowed-tools: Read Bash"), folder, False, None),
        ("Bash(*)", mut(allowed, 'allowed-tools: "Read Bash(*)"'), folder, False, None),
        ("disable-model-invocation false",
         mut("metadata:\n", "disable-model-invocation: false\nmetadata:\n"), folder, False,
         None),
        ("managed: body edited after install", text.replace("Sentinel:", "Edited sentinel:"),
         folder, True, lock),
        ("managed: two trailing newlines", text + "\n", folder, True, None),
        ("managed: CRLF", text.replace("\n", "\r\n"), folder, True, None),
        ("managed: lock names another version", text, folder, True,
         {**lock, "version_id": "other", "version": 4}),
        ("managed: user-only stamp without disable-model-invocation",
         mut('decimalai-invocation: "model"', 'decimalai-invocation: "user"'), folder, True,
         None),
        ("sdk 0.13.9 export",
         "---\nname: acme/refund-policy\n"
         "description: \"Apply Acme's refund rules when a customer asks for a refund.\"\n"
         "invocation: user\ndisable-model-invocation: true\nlicense: Apache-2.0\n"
         "allowed-tools:\n  - Read\n  - Bash\nsource: https://app.decimal.ai/s/x\n---\n\n"
         "# Refund policy\n\nBody.", folder, False, None),
    ]
    out = []
    for cid, t, fname, managed, expect in cases:
        problems = validate_skill_md(t, folder_name=fname, managed=managed, expect=expect)
        out.append({"id": cid, "text": t, "folder_name": fname, "managed": managed,
                    "expect": dict(expect) if expect is not None else None,
                    "problems": sorted([p.rule, p.scope] for p in problems)})
    return out


_NAME_VECTOR_INPUTS = [
    "pdf-tools", "acme/refund-policy", "Acme/Refund-Policy", "my_skill.v2",
    "Docker Hub  Automation", "--weird--name--", "Ünïcödé Tools", "café/crème-brûlée",
    "日本語スキル", "example-owner/中文标题", "pdf 中文 tools", "acme/!!!", "a" * 64, "a" * 65,
    "x-" * 100, "2slides-ppt-generator", "pdf-v2", "install", "search", "check",
    "decimalai-skills", "decimalai/skills", "404", "yes", "null", "0x1f", "2020-01-01",
    "2020-13-45", _REF_LONG,
]


def build_vectors() -> dict[str, Any]:
    """Every vector section, computed by this module from its fixed inputs."""
    sid = "6f1c0000-0000-4000-8000-000000000001"
    other = "11aa0000-0000-4000-8000-000000000002"
    alt = install_name_disambiguated("acme/refund-policy", sid)
    choose_inputs: list[tuple[str, dict[str, str | None]]] = [
        ("acme/refund-policy", {}),
        ("acme/refund-policy", {"acme-refund-policy": other}),
        ("acme/refund-policy", {"acme-refund-policy": None}),
        ("acme/refund-policy", {"old-name": sid, "acme-refund-policy": None}),
        ("acme/refund-policy", {"acme-refund-policy": other, alt: "22bb"}),
        ("日本語スキル", {install_name_for("日本語スキル", sid): other}),
        # Case-insensitive: one folder on macOS and Windows.
        ("acme/refund-policy", {"Acme-Refund-Policy": None}),
        ("acme/refund-policy", {"Acme-Refund-Policy": None, alt.upper(): None}),
        ("acme/refund-policy", {"Old-Name": sid, "acme-refund-policy": None}),
    ]
    choose_cases: list[dict[str, Any]] = []
    for name, taken in choose_inputs:
        c: dict[str, Any] = {"name": name, "skill_id": sid, "taken": taken}
        try:
            c["install_name"] = choose_install_name(name, sid, taken)
        except InstallNameCollision:
            c["error"] = "install_name_collision"
        choose_cases.append(c)

    quote_inputs = [
        "plain words", "", 'say "hi"', "back\\slash", "a: b", "- leading", "# leading",
        "line\u2028sep", "para\u2029sep", "nel\u0085", "bom\ufeff", "tab\t", "nl\n", "cr\r",
        "nul\x00", "del\x7f", "c1\x80\x9f", "nonchar\ufffe\uffff", "emoji 🎉 中文 ñ", "---",
        "------", "a --- b", "--x---y----", "yes", "123",
    ]
    script = Attachment("scripts/run.py", "print(1)\n", executable=True)
    tools_cases = []
    for raw, variant, scripts in [
        (["Read", "Bash", "Bash(git log *)", "Write", "mcp__x"], "authored", [script]),
        (["Read", "Bash", "Bash(git log *)", "Write", "mcp__x"], "authored", []),
        (["Read", "Bash", "Bash(git log *)", "Write", "mcp__x"], "foreign", [script]),
        (["Read,", "Grep,", "Glob,", "Bash,"], "foreign", []),
        # A stored registry shape (MCP server renamed): a wildcard scope on a read-only
        # tool is the bare tool; a real scope is dropped for foreign skills.
        (["mcp__acme-tools__core_memory(*),", "Read(*),", "AskUserQuestion"], "foreign", []),
        (["mcp__acme-tools__core_memory(*),", "Read(*),", "AskUserQuestion"], "authored", []),
        (["Agent(*),", "Read(*),", "Write(*),", "Bash(*),", "Glob(*),", "Grep( ** )"],
         "foreign", []),
        (["Read(./references/**)"], "foreign", []),
        (["Bash(git", "log", "*)", "Read,"], "authored", []),
        ("Read, Bash(git diff, git log)", "authored", []),
        (["Bash(*:*)"], "authored", [script, Attachment("scripts/b.sh", "x", executable=True),
                                     Attachment("scripts/c", "x", executable=True,
                                                execution_language="javascript")]),
        (None, "foreign", []),
    ]:
        pol = allowed_tools_for(raw, variant=variant, scripts=scripts)
        tools_cases.append({"raw": raw, "variant": variant,
                            "scripts": [dataclasses.asdict(s) for s in scripts],
                            "tokens": list(pol.tokens), "value": pol.value,
                            "notes": list(pol.notes)})

    def render_case(c: dict[str, Any]) -> dict[str, Any]:
        b = render_bundle(c["fields"], c["attachments"], install_name=c["install_name"],
                          variant=c["variant"])
        return {"id": c["id"], "fields": c["fields"].to_dict(),
                "attachments": [dataclasses.asdict(a) for a in c["attachments"]],
                "install_name": c["install_name"], "variant": c["variant"],
                "trigger_fields": trigger_fields(c["fields"]), "bundle": b.to_dict()}

    lock_skills = {
        "acme-refund-policy": {
            "name": "acme/refund-policy", "skill_id": sid, "version_id": "9b2e", "version": 3,
            "render": {"format": 1, "hash": "a" * 64, "variant": "foreign"},
            "body_sha256": "b" * 64, "description_sha256": "c" * 64,
            "path": ".agents/skills/acme-refund-policy",
            "files": {"SKILL.md": "d" * 64}, "executable": [], "unavailable": [],
            "source": {"base_url": "https://api.decimal.ai", "org": None},
            "pin": "latest", "invocation": "model", "installed_at": "2026-10-20T10:00:00Z"},
        install_name_for("日本語スキル", other): {
            "name": "日本語スキル", "skill_id": other, "version_id": "f00d", "version": 1,
            "render": {"format": 1, "hash": "e" * 64, "variant": "foreign"},
            "body_sha256": "1" * 64, "description_sha256": "2" * 64,
            "path": f".agents/skills/{install_name_for('日本語スキル', other)}",
            "files": {"SKILL.md": "3" * 64}, "executable": [],
            "unavailable": [{"path": "assets/x.png", "reason": _REASON_BINARY}],
            "source": {"base_url": "https://api.decimal.ai", "org": "org-1"}, "pin": "exact",
            "invocation": "user", "installed_at": "2026-10-21T00:00:00Z"},
    }
    taxonomy_inputs: list[dict[str, Any]] = [
        {"metadata": {"decimalai-category": "security"}, "category": "general"},
        {"category": " general ", "skill-type": "model-gap", "skill-scope": "private"},
        {"metadata": {"decimalai-invocation": "bogus"}, "invocation": "Any"},
        {"disable-model-invocation": True},
        {"metadata": "not a map", "category": 3},
        {},
    ]
    return {
        "description": VECTORS_DESCRIPTION,
        "format": FORMAT,
        "vectors_version": 1,
        "rules": {r: [s, d] for r, (s, d) in RULES.items()},
        "install_names": [
            {"name": n, "skill_id": sid, "base": install_name_base(n),
             "disambiguated": install_name_disambiguated(n, sid),
             "install_name": install_name_for(n, sid)} for n in _NAME_VECTOR_INPUTS],
        "choose_install_name": choose_cases,
        "yaml_quote": [{"input": s, "output": yaml_quote(s)} for s in quote_inputs],
        "allowed_tools": tools_cases,
        "render": [render_case(c) for c in _reference_cases() + _extra_render_cases()],
        "render_errors": [
            {"id": c["id"], "fields": c["fields"].to_dict(),
             "attachments": [dataclasses.asdict(a) for a in c["attachments"]],
             "install_name": c["install_name"], "variant": c["variant"],
             "error": "render_invalid"} for c in _render_error_cases()],
        "validate": _validate_cases(),
        "attachment_paths": [
            {"path": p, "problems": sorted([q.rule, q.scope] for q in
                                           validate_attachment_path(p))}
            for p in ("scripts/a.py", "references/sub/b.md", "templates/SKILL.md",
                      "references/skill.md", "SKILL.md", "../x", "/etc/passwd", "scripts/./x",
                      "scripts\\x", "eval.yaml", "notes/x.md", "scripts",
                      "references/SKILL.md/notes.md", "../../SKILL.md")],
        "lock_hash": [{"root": ".agents/skills", "skills": lock_skills,
                       "lock_hash": lock_hash(".agents/skills", lock_skills)}],
        "taxonomy": [{"frontmatter": fm, "taxonomy": taxonomy(fm)} for fm in taxonomy_inputs],
        "body_sha256": [{"body": b, "body_sha256": body_sha256(b)}
                        for b in ("# T\n\nText", "\n\n  # T\n\nText  \n\n", "x\u2028",
                                  "# T\r\n\r\nText\r\n", "a\rb\r\n")],
    }


def vectors_text() -> str:
    """The vector file's exact text: ASCII JSON, sorted keys, two-space indent, LF."""
    return json.dumps(build_vectors(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def default_vectors_path() -> Path:
    """``tests/vectors/skillfmt_v1.json`` in a source checkout."""
    return Path(__file__).resolve().parent.parent / "tests" / "vectors" / "skillfmt_v1.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m skillevaluation.skillfmt",
        description="Write or check the SKILL.md format-1 golden vectors.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-vectors", nargs="?", const="", metavar="PATH",
                       help="write the vectors (default: tests/vectors/skillfmt_v1.json)")
    group.add_argument("--check-vectors", nargs="?", const="", metavar="PATH",
                       help="exit 1 unless PATH holds exactly what --write-vectors writes")
    args = parser.parse_args(argv)
    given = args.write_vectors if args.write_vectors is not None else args.check_vectors
    if not given and not (default_vectors_path().parents[2] / "pyproject.toml").is_file():
        parser.error("not running from a source checkout: pass PATH")
    text = vectors_text().encode("utf-8")
    if args.write_vectors is not None:
        path = Path(args.write_vectors) if args.write_vectors else default_vectors_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text)
        print(f"wrote {path}", file=sys.stderr)
        return 0
    path = Path(args.check_vectors) if args.check_vectors else default_vectors_path()
    if not path.is_file() or path.read_bytes() != text:
        print(f"{path} is stale: run python -m skillevaluation.skillfmt --write-vectors",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
