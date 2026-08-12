"""Local file-based baseline cache.

The without-skill arm is independent of the skill being tested, so the
runner contract says a conforming runner SHOULD cache it per
``(skill, case)`` and reuse it across repeat runs. This is the local
filesystem implementation: one JSON file per baseline under

    ~/.cache/skillevaluation/baselines/<scope>/<key>.json

* ``key`` comes from :func:`skillevaluation.baseline.baseline_cache_key`
  (the spec'd derivation — skill id + case id + prompt).
* ``scope`` isolates baselines per agent configuration. The contract
  requires invalidation when the manifest changes; for the local runner
  the "manifest" is the adapter + model, so the scope string is derived
  from the adapter's identity (e.g. ``llm:claude-haiku-4-5``).

Only the *execution* is cached (final output + metrics) — grading always
runs fresh, so editing expectations/validators re-grades a cached
baseline without re-running the agent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("skillevaluation.runner.cache")


def default_cache_dir() -> Path:
    """``$SKILLEVAL_CACHE_DIR``, else ``$XDG_CACHE_HOME``/skillevaluation, else ~/.cache."""
    env = os.environ.get("SKILLEVAL_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "skillevaluation"


class BaselineCache:
    """Read/write cached without-skill arm executions.

    Failure-tolerant by design: any I/O or decode problem degrades to a
    cache miss (with a debug log), never an exception — a broken cache
    must not break a benchmark run.
    """

    def __init__(self, scope: str, *, base_dir: Path | None = None):
        scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
        self.scope = scope
        self.dir = (base_dir or default_cache_dir()) / "baselines" / scope_digest
        self._scope_marker = self.dir / "_scope.txt"

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """Return the cached payload for ``key``, or None on miss."""
        path = self._path(key)
        try:
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            logger.debug("baseline cache read failed for %s", path, exc_info=True)
            return None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        """Persist ``payload`` for ``key`` (best-effort)."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if not self._scope_marker.exists():
                # Human-readable breadcrumb: which adapter/model this
                # scope hash corresponds to.
                self._scope_marker.write_text(self.scope, encoding="utf-8")
            record = dict(payload)
            record.setdefault("cached_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            self._path(key).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.debug("baseline cache write failed for key %s", key, exc_info=True)

    def clear(self) -> int:
        """Delete this scope's cached baselines. Returns the count removed."""
        removed = 0
        try:
            if self.dir.is_dir():
                for f in self.dir.glob("*.json"):
                    f.unlink(missing_ok=True)
                    removed += 1
        except Exception:
            logger.debug("baseline cache clear failed", exc_info=True)
        return removed
