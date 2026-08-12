"""Packaged spec + schema resources are loadable in both layouts.

These tests run against the source tree (repo-root ``schemas/`` and
``spec/``); test_wheel_layout simulates the installed-wheel layout the
force-include mapping produces.
"""

from __future__ import annotations

import json

import pytest

from skillevaluation.resources import (
    list_schemas,
    list_specs,
    load_schema,
    read_spec,
)

EXPECTED_SCHEMAS = {
    "eval-yaml",
    "judge-result",
    "test-case-result",
    "test-run-result",
}

EXPECTED_SPECS = {
    "eval-yaml",
    "llm-judge",
    "runner-contract",
    "trajectory-format",
    "versioning-policy",
}


def test_list_schemas_contains_all_canonical_schemas():
    assert EXPECTED_SCHEMAS.issubset(set(list_schemas()))


def test_list_specs_contains_all_canonical_specs():
    assert EXPECTED_SPECS.issubset(set(list_specs()))


@pytest.mark.parametrize("name", sorted(EXPECTED_SCHEMAS))
def test_every_schema_loads_as_valid_json_schema(name):
    schema = load_schema(name)
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert "properties" in schema


def test_load_schema_accepts_full_filename():
    assert load_schema("test-run-result.schema.json")["title"]


@pytest.mark.parametrize("name", sorted(EXPECTED_SPECS))
def test_every_spec_reads_as_markdown(name):
    text = read_spec(name)
    assert text.lstrip().startswith("#")


def test_missing_resource_raises_with_searched_paths():
    with pytest.raises(FileNotFoundError, match="searched"):
        load_schema("no-such-schema")


def test_wheel_layout(tmp_path, monkeypatch):
    """Simulate the installed layout: schemas/ + spec/ INSIDE the package."""
    pkg = tmp_path / "skillevaluation"
    (pkg / "schemas").mkdir(parents=True)
    (pkg / "spec").mkdir()
    (pkg / "schemas" / "demo.schema.json").write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "properties": {}})
    )
    (pkg / "spec" / "demo.md").write_text("# Demo spec\n")

    import skillevaluation.resources as res

    monkeypatch.setattr(
        res, "_candidate_dirs", lambda kind: [pkg / kind]
    )
    assert res.load_schema("demo")["properties"] == {}
    assert res.read_spec("demo").startswith("# Demo")
    assert res.list_schemas() == ["demo"]
    assert res.list_specs() == ["demo"]
