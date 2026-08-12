"""Access the packaged spec documents and JSON Schemas.

The repo keeps ``spec/`` and ``schemas/`` at the repository root (so they
read naturally on GitHub); the wheel maps them inside the package (see
``[tool.hatch.build.targets.wheel.force-include]`` in pyproject.toml).
This module papers over the two layouts:

    from skillevaluation.resources import load_schema, read_spec, list_schemas

    schema = load_schema("test-run-result")     # -> dict
    text = read_spec("runner-contract")         # -> str

Why ship them at all: a results consumer should be able to validate a
``results.json`` against the canonical schema without a network fetch,
and a runner implementer should get the spec with ``pip install`` —
the GitHub links are not a substitute for the artifact being present.
"""

from __future__ import annotations

import json
from importlib import resources as _ilr
from pathlib import Path
from typing import Any

_SCHEMA_SUFFIX = ".schema.json"


def _candidate_dirs(kind: str) -> list[Path]:
    """Ordered locations to look for ``schemas``/``spec`` content.

    1. Inside the installed package (wheel layout, via force-include).
    2. Repository root next to the package (source/editable layout).
    """
    dirs: list[Path] = []
    try:
        pkg_root = Path(str(_ilr.files("skillevaluation")))
        dirs.append(pkg_root / kind)
        dirs.append(pkg_root.parent / kind)
    except Exception:  # pragma: no cover — importlib.resources failure
        here = Path(__file__).resolve().parent
        dirs.append(here / kind)
        dirs.append(here.parent / kind)
    return dirs


def _resolve(kind: str, filename: str) -> Path:
    for base in _candidate_dirs(kind):
        candidate = base / filename
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(d) for d in _candidate_dirs(kind))
    raise FileNotFoundError(
        f"skillevaluation resource {kind}/{filename} not found (searched: {searched})"
    )


def load_schema(name: str) -> dict[str, Any]:
    """Load a JSON Schema by short name (``"eval-yaml"``) or filename.

    Returns the parsed schema dict.
    """
    filename = name if name.endswith(_SCHEMA_SUFFIX) else f"{name}{_SCHEMA_SUFFIX}"
    path = _resolve("schemas", filename)
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def list_schemas() -> list[str]:
    """Short names of every packaged schema, sorted."""
    for base in _candidate_dirs("schemas"):
        if base.is_dir():
            return sorted(
                p.name[: -len(_SCHEMA_SUFFIX)]
                for p in base.iterdir()
                if p.name.endswith(_SCHEMA_SUFFIX)
            )
    return []


def read_spec(name: str) -> str:
    """Read a spec document by short name (``"runner-contract"``) or filename."""
    filename = name if name.endswith(".md") else f"{name}.md"
    path = _resolve("spec", filename)
    return path.read_text(encoding="utf-8")


def list_specs() -> list[str]:
    """Short names of every packaged spec document, sorted."""
    for base in _candidate_dirs("spec"):
        if base.is_dir():
            return sorted(p.stem for p in base.iterdir() if p.suffix == ".md")
    return []


def example_path(name: str) -> Path:
    """Absolute path of a packaged example skill directory (e.g. ``"commit-conventions"``).

    Lets ``skillevaluation run <name>`` fall back to a bundled example when the argument is a bare
    name rather than a local path — so the README's headline command works post-install from any
    working directory (the wheel force-includes ``examples/``).
    """
    for base in _candidate_dirs("examples"):
        candidate = base / name
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(d / name) for d in _candidate_dirs("examples"))
    raise FileNotFoundError(f"skillevaluation example {name!r} not found (searched: {searched})")


def list_examples() -> list[str]:
    """Names of every packaged example skill directory (those carrying an ``eval.yaml``), sorted."""
    for base in _candidate_dirs("examples"):
        if base.is_dir():
            return sorted(p.name for p in base.iterdir() if (p / "eval.yaml").is_file())
    return []


def build_registry() -> Any:
    """A jsonschema ``referencing.Registry`` holding every packaged schema keyed by its ``$id``.

    So a document with a cross-schema ``$ref`` validates OFFLINE, against the schemas in THIS
    install — no network fetch of the ``$id`` URLs, which would otherwise resolve to whatever
    is on the default branch rather than the version you are running. Needs ``referencing``
    (ships with ``jsonschema>=4.18``); install the ``[dev]`` extra. Pair with
    :func:`load_validator`.
    """
    try:
        from referencing import Registry, Resource
    except ImportError as exc:  # pragma: no cover — dev-only helper
        raise RuntimeError(
            "build_registry needs the 'referencing' package (jsonschema>=4.18) — "
            "pip install 'skillevaluation[dev]'"
        ) from exc
    resources = []
    for short in list_schemas():
        schema = load_schema(short)
        resources.append((schema.get("$id") or short, Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def load_validator(name: str) -> Any:
    """A ``Draft202012Validator`` for the named packaged schema, bound to :func:`build_registry` so
    its ``$ref``s resolve offline. ``load_validator("test-case-result").iter_errors(doc)`` then
    needs no network. Needs the ``[dev]`` extra (``jsonschema``)."""
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — dev-only helper
        raise RuntimeError(
            "load_validator needs jsonschema — pip install 'skillevaluation[dev]'"
        ) from exc
    return Draft202012Validator(load_schema(name), registry=build_registry())
