"""BaselineCache — scoping, round-trips, corruption tolerance."""

from __future__ import annotations

from skillevaluation.baseline import baseline_cache_key
from skillevaluation.runner.cache import BaselineCache, default_cache_dir


def _cache(tmp_path, scope="llm:test-model"):
    return BaselineCache(scope, base_dir=tmp_path)


def test_miss_returns_none(tmp_path):
    assert _cache(tmp_path).get("0" * 16) is None


def test_round_trip(tmp_path):
    cache = _cache(tmp_path)
    key = baseline_cache_key(skill_id="s", case_id="c", prompt="p")
    cache.put(key, {"final_output": "cached text", "total_tokens": 42})
    got = cache.get(key)
    assert got["final_output"] == "cached text"
    assert got["total_tokens"] == 42
    assert "cached_at" in got  # stamped on write


def test_different_scopes_are_isolated(tmp_path):
    """Same key in a different scope (model change) must miss — the runner
    contract requires invalidation when the agent configuration changes."""
    key = baseline_cache_key(skill_id="s", case_id="c", prompt="p")
    a = BaselineCache("llm:model-a", base_dir=tmp_path)
    b = BaselineCache("llm:model-b", base_dir=tmp_path)
    a.put(key, {"final_output": "from a"})
    assert b.get(key) is None
    assert a.get(key)["final_output"] == "from a"


def test_scope_marker_written_for_debuggability(tmp_path):
    cache = _cache(tmp_path, scope="llm:claude-haiku-4-5")
    cache.put("k" * 16, {"final_output": "x"})
    marker = cache.dir / "_scope.txt"
    assert marker.read_text() == "llm:claude-haiku-4-5"


def test_corrupt_cache_file_degrades_to_miss(tmp_path):
    cache = _cache(tmp_path)
    cache.put("deadbeefdeadbeef", {"final_output": "x"})
    (cache.dir / "deadbeefdeadbeef.json").write_text("{not json")
    assert cache.get("deadbeefdeadbeef") is None


def test_non_dict_payload_degrades_to_miss(tmp_path):
    cache = _cache(tmp_path)
    cache.dir.mkdir(parents=True, exist_ok=True)
    (cache.dir / ("a" * 16 + ".json")).write_text('["list", "not", "dict"]')
    assert cache.get("a" * 16) is None


def test_clear_removes_only_this_scope(tmp_path):
    key = baseline_cache_key(skill_id="s", case_id="c", prompt="p")
    a = BaselineCache("scope-a", base_dir=tmp_path)
    b = BaselineCache("scope-b", base_dir=tmp_path)
    a.put(key, {"final_output": "a"})
    b.put(key, {"final_output": "b"})
    assert a.clear() == 1
    assert a.get(key) is None
    assert b.get(key)["final_output"] == "b"


def test_default_cache_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLEVAL_CACHE_DIR", str(tmp_path / "custom"))
    assert default_cache_dir() == tmp_path / "custom"
    monkeypatch.delenv("SKILLEVAL_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_dir() == tmp_path / "xdg" / "skillevaluation"
