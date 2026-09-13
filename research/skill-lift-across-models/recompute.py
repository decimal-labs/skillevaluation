#!/usr/bin/env python3
"""Recompute every figure on docs.decimal.ai/research/skill-lift-across-models from the
rows shipped next to this file. No database, no network, no dependencies beyond the
standard library.

    python3 recompute.py            # prints the figures and rewrites summary.json
    python3 recompute.py --no-write # prints only

WHAT IT READS
  skill_lift_pairs.csv   one row per (skill, runner model): the lift the registry published
                         for that run, both pass rates, the case count the headline was
                         computed over, and a hash over the case set.
  skill_lift_cases.csv   one row per case per run: the A/B outcome that the pass rates are
                         computed from. Used here to re-derive every lift independently of
                         the published number.

WHAT IT COMPUTES
  A pair is a skill with a published lift on both models. The population figures on the
  page are over pairs whose BOTH runs headline at least --min-cases cases (default 8, the
  registry's own floor). Pairs below the floor are shipped in the CSV so the exclusion is
  checkable, and are listed in the output rather than silently dropped.

  "Same case set" means the two runs' case_set_hash values are equal: the same case rows
  by id, not merely the same count.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD, NEW = "gemini-3.5-flash", "gemini-3.6-flash"


def read_pairs(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    by_skill: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            by_skill[row["skill_slug"]][row["runner_model"]] = row
    return {slug: runs for slug, runs in by_skill.items() if OLD in runs and NEW in runs}


def derived_lift(cases: list[dict[str, str]]) -> float | None:
    """Lift re-derived from per-case outcomes alone: pass rate with minus without, in points."""
    if not cases:
        return None
    with_ = sum(1 for c in cases if c["with_skill_passed"] == "True")
    without = sum(1 for c in cases if c["without_skill_passed"] == "True")
    return round((with_ - without) / len(cases) * 100, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=HERE,
                    help="directory holding the CSVs (default: this file's)")
    ap.add_argument("--min-cases", type=int, default=8,
                    help="headline floor applied to both runs of a pair")
    ap.add_argument("--no-write", action="store_true",
                    help="print the figures without rewriting summary.json")
    args = ap.parse_args()

    pairs = read_pairs(args.dir / "skill_lift_pairs.csv")
    cases_by_run: dict[str, list[dict[str, str]]] = defaultdict(list)
    with (args.dir / "skill_lift_cases.csv").open(newline="") as fh:
        for row in csv.DictReader(fh):
            cases_by_run[row["run_id"]].append(row)

    def meets_floor(runs: dict[str, dict[str, str]]) -> bool:
        return all(int(runs[m]["headline_cases"]) >= args.min_cases for m in (OLD, NEW))

    population = {slug: runs for slug, runs in pairs.items() if meets_floor(runs)}
    below = {slug: runs for slug, runs in pairs.items() if slug not in population}

    deltas = {slug: float(r[NEW]["lift_pts"]) - float(r[OLD]["lift_pts"])
              for slug, r in population.items()}
    same_set = sorted(slug for slug, r in population.items()
                      if r[OLD]["case_set_hash"] == r[NEW]["case_set_hash"])

    disagreements = []
    for slug, runs in pairs.items():
        for model in (OLD, NEW):
            row = runs[model]
            d = derived_lift(cases_by_run.get(row["run_id"], []))
            if d is not None and abs(d - float(row["lift_pts"])) > 0.05:
                disagreements.append({"skill": slug, "model": model,
                                      "published_pts": float(row["lift_pts"]), "derived_pts": d})

    values = list(deltas.values())
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "recompute.py over skill_lift_pairs.csv + skill_lift_cases.csv",
        "models": [OLD, NEW],
        "headline_floor_cases": args.min_cases,
        "pairs_shipped": len(pairs),
        "pairs_in_population": len(population),
        "pairs_below_floor": [
            {"skill": s, "headline_cases": {m: int(r[m]["headline_cases"]) for m in (OLD, NEW)}}
            for s, r in sorted(below.items())],
        "fell": sum(1 for v in values if v < 0),
        "rose": sum(1 for v in values if v > 0),
        "held": sum(1 for v in values if v == 0),
        "mean_change_pts": round(statistics.mean(values), 1) if values else None,
        "median_change_pts": round(statistics.median(values), 1) if values else None,
        "largest_drop": min(deltas.items(), key=lambda kv: kv[1]) if deltas else None,
        "largest_rise": max(deltas.items(), key=lambda kv: kv[1]) if deltas else None,
        "same_case_set_pairs": same_set,
        "same_case_set_values": {
            s: {OLD: float(population[s][OLD]["lift_pts"]),
                NEW: float(population[s][NEW]["lift_pts"]),
                "cases": int(population[s][OLD]["headline_cases"])} for s in same_set},
        "pairs_whose_case_sets_differ": len(population) - len(same_set),
        "rows_where_derived_lift_differs_from_published": len(disagreements),
        "derived_disagreements": disagreements,
        "case_rows_read": sum(len(v) for v in cases_by_run.values()),
    }
    print(json.dumps(summary, indent=2))
    if not args.no_write:
        (args.dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\nwrote {args.dir / 'summary.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
