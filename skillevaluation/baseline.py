"""Baseline-trace cache key derivation.

The without-skill arm of an A/B run is expensive: it's a full agent
execution that has nothing to do with the skill being tested. A
conforming runner SHOULD cache the without-skill trajectory per
(skill, case) pair so repeat runs on the same manifest reuse it.

This module derives the cache key.

See ``spec/runner-contract.md`` for the canonical specification.
"""

from __future__ import annotations

import hashlib
import json


def baseline_cache_key(
    *, skill_id: str, case_id: str, prompt: str, trial: int = 0
) -> str:
    """Compute the cache key for a without-skill baseline trajectory.

    Args:
        skill_id: stable ID of the skill being benchmarked.
        case_id: stable ID of the test case.
        prompt: the case's user prompt (changing the prompt should
            invalidate the cache).
        trial: 0-based run index within a multi-run case (runner-level --runs;
            the wire name 'trial' is kept for cache-key stability).
            Trials are i.i.d. rollouts, so each must cache (and re-run)
            independently — sharing one key across trials freezes the
            without-arm to run 0's output and understates its variance. Default
            0 keeps the single-run key byte-identical to the old one.

    Returns:
        A 16-character hex digest. Short enough to fit in URLs / log
        lines; long enough that collisions are effectively impossible
        for any realistic catalog size.
    """
    payload: dict[str, str | int] = {"skill": skill_id, "prompt": prompt, "case": case_id}
    # Only widen the key for multi-trial runs so trial 0 (and every
    # single-trial case) keeps the historical digest — no cache churn.
    if trial:
        payload["trial"] = trial
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
