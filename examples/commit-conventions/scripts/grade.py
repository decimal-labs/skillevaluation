#!/usr/bin/env python3
"""Reusable text grader for registry skill eval cases.

Reads the model output from the staged `response.txt`, looks up the per-case
expectation in a bundled spec JSON, and exits 0 (pass) / 1 (fail) / 2 (bad spec).
Stdlib only — runs under the sandbox's `python3`.

    python3 scripts/grade.py scripts/grading_spec.json <case-name>

grading_spec.json:
    {"mode": "label|verdict|yesno|json|contains",
     "labels": [...],                  # label mode only (the full label space)
     "cases": {"<name>": <expected>}}
where <expected> is a label/verdict/yesno string; a {field: value} object for json
(value "<PRESENT>" = key present & non-empty; {"__abstain__": true} = must signal
error/abstain); or {"forbid":[],"require":[],"require_any":[]} for contains.
"""
import json
import os
import re
import sys

PRESENT = "<PRESENT>"


def label_grade(labels, expected, out):
    labels = [l.lower() for l in labels]
    low = out.lower()
    found = [l for l in labels if l in low]
    return found == [expected.lower()] or low.strip().strip(".") == expected.lower()


def verdict_grade(expected, out):
    low = out.lower()
    said = "INVALID" if "invalid" in low else ("VALID" if "valid" in low else "?")
    return said == expected


def yesno_grade(expected, out):
    m = re.search(r"\b(yes|no)\b", out.lower())
    return (m.group(1).upper() if m else "?") == expected


def _parse_json_obj(out):
    t = out.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL | re.I)
    if m:
        t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def _row_matches(exp, obj):
    if not isinstance(obj, dict):
        return False
    for k, v in exp.items():
        pv = obj.get(k)
        if v == PRESENT:
            if pv in (None, "", [], {}):
                return False
        elif isinstance(v, (int, float)) and isinstance(pv, (int, float)) and not isinstance(pv, bool):
            if float(pv) != float(v):
                return False
        elif str(pv) != str(v):
            return False
    return True


def json_grade(exp, out):
    if isinstance(exp, dict) and exp.get("__abstain__"):
        low = out.lower()
        return any(s in low for s in ('"error"', "cannot", "missing", "abstain", "unable"))
    return _row_matches(exp, _parse_json_obj(out))


def _parse_json_array(out):
    t = out.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL | re.I)
    if m:
        t = m.group(1).strip()
    i, j = t.find("["), t.rfind("]")
    if i == -1 or j == -1:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def json_array_grade(exp, out):
    """exp = list of row dicts; output must be a JSON array, each reference row
    matched IN ORDER (per-field rules as _row_matches)."""
    arr = _parse_json_array(out)
    if not isinstance(arr, list) or len(arr) != len(exp):
        return False
    return all(_row_matches(er, gr) for er, gr in zip(exp, arr))


def json_set_grade(exp, out):
    """Order-independent array match: each reference row has a distinct matching
    output row, no extras (for entities in prose, which have no fixed order)."""
    arr = _parse_json_array(out)
    if not isinstance(arr, list) or len(arr) != len(exp):
        return False
    used = [False] * len(arr)
    for er in exp:
        for i, gr in enumerate(arr):
            if not used[i] and _row_matches(er, gr):
                used[i] = True
                break
        else:
            return False
    return True


def contains_grade(exp, out):
    low = out.lower()
    if any(t.lower() in low for t in exp.get("forbid", [])):
        return False
    if any(t.lower() not in low for t in exp.get("require", [])):
        return False
    ra = exp.get("require_any", [])
    if ra and not any(t.lower() in low for t in ra):
        return False
    return True


def main():
    if len(sys.argv) < 3:
        print("usage: grade.py <grading_spec.json> <case-name>", file=sys.stderr)
        return 2
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    name = sys.argv[2]
    try:
        out = open("response.txt", encoding="utf-8").read()
    except OSError:
        out = os.environ.get("RESPONSE_TEXT", "")
    if name not in spec.get("cases", {}):
        print(f"no spec for case {name}", file=sys.stderr)
        return 2
    exp = spec["cases"][name]
    mode = spec["mode"]
    if mode == "label":
        ok = label_grade(spec["labels"], exp, out)
    elif mode == "verdict":
        ok = verdict_grade(exp, out)
    elif mode == "yesno":
        ok = yesno_grade(exp, out)
    elif mode == "json":
        ok = json_grade(exp, out)
    elif mode == "contains":
        ok = contains_grade(exp, out)
    elif mode == "json_array":
        ok = json_array_grade(exp, out)
    elif mode == "json_set":
        ok = json_set_grade(exp, out)
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
