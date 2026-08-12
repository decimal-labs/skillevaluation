"""Tests for skillevaluation.baseline."""

from __future__ import annotations

from skillevaluation.baseline import baseline_cache_key


def test_key_is_deterministic():
    k1 = baseline_cache_key(skill_id="s1", case_id="c1", prompt="hello")
    k2 = baseline_cache_key(skill_id="s1", case_id="c1", prompt="hello")
    assert k1 == k2


def test_key_is_16_hex_chars():
    k = baseline_cache_key(skill_id="s1", case_id="c1", prompt="hello")
    assert len(k) == 16
    assert all(c in "0123456789abcdef" for c in k)


def test_key_changes_with_prompt():
    k1 = baseline_cache_key(skill_id="s1", case_id="c1", prompt="hello")
    k2 = baseline_cache_key(skill_id="s1", case_id="c1", prompt="goodbye")
    assert k1 != k2


def test_key_changes_with_skill_id():
    k1 = baseline_cache_key(skill_id="s1", case_id="c1", prompt="hello")
    k2 = baseline_cache_key(skill_id="s2", case_id="c1", prompt="hello")
    assert k1 != k2


def test_key_changes_with_case_id():
    k1 = baseline_cache_key(skill_id="s1", case_id="c1", prompt="hello")
    k2 = baseline_cache_key(skill_id="s1", case_id="c2", prompt="hello")
    assert k1 != k2
