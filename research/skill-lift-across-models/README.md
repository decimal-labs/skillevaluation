# Skill lift across models — the dataset

The rows behind [*Skill lift does not survive a model change*](https://docs.decimal.ai/research/skill-lift-across-models):
every public skill in the DecimalAI registry whose lift was measured on both `gemini-3.5-flash`
and `gemini-3.6-flash`, with the per-case A/B outcomes and an `agentversion` manifest per run.

Recompute every figure on that page, with no database and no dependencies:

```bash
python3 recompute.py
```

## Files

| file | rows | what it is |
| --- | ---: | --- |
| `skill_lift_pairs.csv` | 96 | one row per (skill, runner model): the lift the registry published for that run, both pass rates, the case count the headline was computed over (`headline_cases`), a hash over the case set, the run id, and the lift re-derived from the case rows |
| `skill_lift_cases.csv` | 2,038 | one row per case per run: the A/B outcome (`pass_kept`, `flip_to_pass`, `flip_to_fail`, `fail_kept`) that the pass rates are computed from |
| `skill_lift_manifests.jsonl` | 96 | one `agentversion` manifest per run, pinning skill version, runner model, judge model, harness version, case set and grading method |
| `summary.json` | — | the figures on the page, as `recompute.py` emitted them |
| `recompute.py` | — | the reproduction |

## What the figures are

A **pair** is a skill with a published lift on both models — 48 in the file. A pair enters the
population figures when **both** runs headline at least 8 cases, which is the registry's own floor
before it prints a lift; 45 do. The three below it are shipped and named in `summary.json` rather
than dropped, because they happen to be the two most extreme movements in the file, and a reader
should be able to see that the floor is what excludes them.

**Same case set** means the two runs' `case_set_hash` values are equal — the same case rows by id,
not merely the same count. Four pairs qualify, and they are the evidence the page leans on; the
41 others graded suites that were re-cut between runs, so their movement mixes a model change with
a case-set change.

## Provenance

- Pulled from the production registry, read-only, by `skill_lift_pairs.py` (a DecimalAI-internal
  script that opens a read-only transaction and issues SELECTs). Pulled 2026-08-28, re-pulled
  2026-09-05 and 2026-09-13; the 96 rows were identical each time.
- The runs completed in June–July 2026 under the DecimalAI evaluation fleet — synthetic, pinned
  conditions, not third-party production traffic.
- Model outputs were nulled by the platform's payload sweep before this dataset was cut
  (`outputs_pruned` is `True` on every row). What survives per case is the outcome, which is what
  every pass rate is computed from, so the numbers reproduce exactly; the text each arm produced
  does not.
- One row's published lift differs from the lift re-derived from its cases
  (`progressive-tax-bracket-math` on `gemini-3.6-flash`: 80.0 published, 81.8 derived) because its
  headline was computed over a calibration-gated subset of its cases; `headline_cases` &lt;
  `total_cases` on that row says so.

Licensed under the same Apache 2.0 license as this repository.
