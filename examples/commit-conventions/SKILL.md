---
name: commit-conventions
description: Use when writing git commit subject lines in a repo with a custom commit format — applies the org's convention ([TICKET] AREA: summary, with arbitrary AREA codes) the base model won't guess, so commits are consistent and parseable. Template: replace the rules with your own.
category: coding
tags: [coding, git, commit, conventions, template]
stability: stable
---

> **Worked example.** The rules below are a fictional "Acme" convention. They're a
> format a model *cannot* guess from training — which is the whole point: the
> with-skill vs without-skill delta this example measures is real knowledge-lift,
> not a formatting artifact. Fork this skill and drop in your own org's rules.

# Acme Commit Message Convention

Write the commit SUBJECT LINE in OUR exact format: `[TICKET] AREA: imperative summary`

- `[TICKET]` — the ticket id in SQUARE BRACKETS (given in the input), e.g. `[PROJ-101]`.
- `AREA` — our code, mapped from what the change touches:
  | The change touches… | AREA |
  |---|---|
  | HTTP / network / API client / webhook | `NET` |
  | database / SQL / migration / index / connection pool | `DATA` |
  | UI / frontend / CSS / styling / page / nav / form | `UI` |
  | anything else | `CORE` |
- summary — imperative mood, lowercase, no trailing period.

**This is NOT Conventional Commits** — do not use `feat:` / `fix:` / `chore:` / `docs:`.
A model that defaults to Conventional Commits fails every case here — which is exactly
the lift this skill supplies: the convention, not generic competence.

## When to use

Load this skill only when writing a git commit **subject line** for this repo.
Do not apply the format to anything else (PR bodies, changelogs, plain answers) —
see the non-activation case in `eval.yaml`.

## Output

Return **only** the subject line — no body, no quotes, no code fence, no explanation.
