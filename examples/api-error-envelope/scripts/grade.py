#!/usr/bin/env python3
"""Grade an API error-response body against the Acme house envelope contract.

Reads the model's output (the runner stages it as ``response.txt`` and ``$RESPONSE_TEXT``), extracts
the first JSON object, and checks the envelope the skill teaches:

    {"error": {"code": "<SCREAMING_SNAKE>", "message": "<non-empty>", "request_id": "<non-empty>"}}

Exit 0 = pass, 1 = the body breaks the contract (a model failure), 2 = grader misuse (bad args).
Stdlib only, so it runs under the sandbox's ``python3``.

    python3 scripts/grade.py <EXPECTED_CODE>

The single argument is the SCREAMING_SNAKE code this case must return (e.g. ``NOT_FOUND``).
"""
import json
import os
import re
import sys

_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]*$")


def _extract_json_object(text: str):
    """Best-effort parse of the first JSON object in the model output (tolerates a ```json fence)."""
    t = (text or "").strip()
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


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: grade.py <EXPECTED_CODE>", file=sys.stderr)
        return 2
    expected_code = sys.argv[1]

    try:
        out = open("response.txt", encoding="utf-8").read()
    except OSError:
        out = os.environ.get("RESPONSE_TEXT", "")

    obj = _extract_json_object(out)
    if not isinstance(obj, dict):
        print("FAIL: output is not a JSON object", file=sys.stderr)
        return 1

    err = obj.get("error")
    if not isinstance(err, dict):
        print("FAIL: no nested `error` object — the house envelope wraps code/message/request_id",
              file=sys.stderr)
        return 1

    code = err.get("code")
    if not (isinstance(code, str) and _CODE_RE.fullmatch(code)):
        print(f"FAIL: error.code missing or not SCREAMING_SNAKE_CASE: {code!r}", file=sys.stderr)
        return 1
    if code != expected_code:
        print(f"FAIL: error.code {code!r} != expected {expected_code!r}", file=sys.stderr)
        return 1
    if not (isinstance(err.get("message"), str) and err["message"].strip()):
        print("FAIL: error.message missing or empty", file=sys.stderr)
        return 1
    # request_id is the discriminator most hand-written error bodies omit.
    if not (isinstance(err.get("request_id"), str) and err["request_id"].strip()):
        print("FAIL: error.request_id missing or empty (required on every Acme error)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
