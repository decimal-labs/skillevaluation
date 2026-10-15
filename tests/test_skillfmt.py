"""skillfmt: install names, quoting, the format-1 renderer, the validator, hashes, vectors.

Every expectation here is written independently of the module under test: hashes are
recomputed with hashlib, the three reference skills are rebuilt by a line-for-line port of
the reference generator the eight native loaders were proven on, and loader quirks are
emulated in plain Python. The vector file is checked last, as a replay a client in any
language can run.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from skillevaluation import skillfmt
from skillevaluation.skillfmt import (
    Attachment,
    InstallNameCollision,
    Problem,
    RenderError,
    RenderFields,
)

VECTORS_PATH = Path(__file__).parent / "vectors" / "skillfmt_v1.json"


def h6(skill_id: str) -> str:
    return hashlib.sha256(skill_id.encode("utf-8")).hexdigest()[:6]


def sha(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def rules(problems: list[Problem]) -> set[tuple[str, str]]:
    return {(p.rule, p.scope) for p in problems}


# ── The reference skills: a port of the generator all eight loaders accepted ──────────
#
# The reference wrote three skills in the target shape and every loader listed, loaded and
# ran them. "Upgraded to format 1" is exactly one change: the fixed `decimalai-format: "1"`
# key, in its fixed place after `decimalai-name`.

SENTINEL = "NATIVE-SENTINEL-7f3a"
FEES = ("# Restocking fees\n\n| category | fee |\n|---|---|\n| electronics | 15% |\n"
        "| apparel | 0% |\n")
SCRIPT = (
    "#!/usr/bin/env python3\n"
    '"""compute_refund.py <amount> <days>"""\n'
    "import sys\n"
    "amount, days = float(sys.argv[1]), int(sys.argv[2])\n"
    "print('REFUND full refund: %.2f' % amount if days <= 30 else\n"
    "      'REFUND store credit: %.2f' % amount if days <= 60 else 'REFUND none')\n"
)
LONG_NAME = "some-github-owner/an-extremely-long-imported-skill-name-that-exceeds-the-limit"
ORACLE_SPECS = [
    dict(registry="acme/refund-policy", title="Acme refund policy", user_only=False,
         allowed="Read Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/compute_refund.py *)",
         files=True, variant="authored"),
    dict(registry="acme/returns-desk", title="Acme returns desk", user_only=True,
         allowed=None, files=False, variant="authored"),
    dict(registry=LONG_NAME, title="Imported refund policy", user_only=False,
         allowed=None, files=True, variant="foreign"),
]


def oracle_body(title: str) -> str:
    return (
        f"\n# {title}\n\nSentinel: {SENTINEL}\n\n"
        "- Returned within 30 days: full refund.\n- 31-60 days: store credit.\n"
        "- After 60 days: none.\n\n"
        "For fees read references/fees.md. To compute a refund, run "
        "scripts/compute_refund.py <amount> <days>.\n\n"
    )


def oracle_install_name(registry_name: str, skill_id: str) -> str:
    n = re.sub(r"[^a-z0-9]+", "-", registry_name.lower()).strip("-")
    n = re.sub(r"-{2,}", "-", n)
    if len(n) > 64:
        n = n[:57].rstrip("-") + "-" + h6(skill_id)
    return n


def oracle_skill_md(name, description, meta, *, allowed=None, user_only=False, body):
    q = json.dumps  # every reference value is ASCII, where JSON and YAML quoting agree
    lines = ["---", f"name: {name}", f"description: {q(description)}",
             f"license: {q('Apache-2.0')}", f"compatibility: {q('Requires python3')}"]
    if allowed:
        lines.append(f"allowed-tools: {q(allowed)}")
    if user_only:
        lines.append("disable-model-invocation: true")
    lines.append("metadata:")
    lines += [f"  {k}: {q(v)}" for k, v in meta.items()]
    lines.append("---")
    return "\n".join(lines) + "\n" + body.strip() + "\n"


def oracle_case(i: int) -> dict[str, Any]:
    sp = ORACLE_SPECS[i]
    sid = str(uuid.UUID(int=0x5EED0000 + i))
    vid = str(uuid.UUID(int=0xF00D0000 + i))
    name = oracle_install_name(sp["registry"], sid)
    body = oracle_body(sp["title"])
    desc = f"Apply {sp['title']} rules when a customer asks for a refund, a return or store credit."
    meta = {
        "decimalai-skill-id": sid,
        "decimalai-version-id": vid,
        "decimalai-version": "3",
        "decimalai-body-sha256": sha(body.strip()),
        "decimalai-name": sp["registry"],
        "decimalai-format": "1",
        "decimalai-invocation": "user" if sp["user_only"] else "model",
        "decimalai-category": "support",
    }
    fields = RenderFields(
        skill_id=sid, version_id=vid, version=3, registry_name=sp["registry"], body=body,
        description=desc, license="Apache-2.0", compatibility="Requires python3",
        allowed_tools=["Read", "Bash"] if sp["allowed"] else None,
        invocation_mode="user" if sp["user_only"] else "model", category="support",
    )
    attachments = []
    if sp["files"]:
        attachments = [
            Attachment("references/fees.md", FEES),
            Attachment("scripts/compute_refund.py", SCRIPT, executable=True,
                       execution_language="python"),
        ]
    return {
        "fields": fields, "install_name": name, "variant": sp["variant"],
        "attachments": attachments,
        "text": oracle_skill_md(name, desc, meta, allowed=sp["allowed"],
                                user_only=sp["user_only"], body=body),
        "lock": {"name": sp["registry"], "skill_id": sid, "version_id": vid, "version": 3,
                 "render": {"format": 1}, "body_sha256": sha(body.strip()),
                 "invocation": "user" if sp["user_only"] else "model"},
    }


@pytest.mark.parametrize("i", range(3))
def test_renders_the_reference_skills_byte_equal(i):
    case = oracle_case(i)
    assert skillfmt.install_name_for(case["fields"].registry_name,
                                     case["fields"].skill_id) == case["install_name"]
    bundle = skillfmt.render_bundle(case["fields"], case["attachments"],
                                    install_name=case["install_name"], variant=case["variant"])
    assert bundle.skill_md == case["text"]
    assert bundle.files[0].path == "SKILL.md"
    assert bundle.files[0].sha256 == sha(case["text"]) and bundle.files[0].mode == "0644"
    want = {"SKILL.md": (case["text"], "0644")}
    if ORACLE_SPECS[i]["files"]:
        want["references/fees.md"] = (FEES, "0644")
        want["scripts/compute_refund.py"] = (SCRIPT, "0755")
    got = {f.path: (f.content, f.mode) for f in bundle.files}
    assert got == want
    for f in bundle.files:
        assert f.sha256 == sha(f.content) and f.size == len(f.content.encode("utf-8"))
    assert bundle.unavailable == ()
    # The authored skill's stored list is ["Read", "Bash"]; the rewrite of bare Bash is noted.
    assert bundle.policy_notes == (
        ("allowed-tools: rewrote 'Bash' to 1 scoped entry for the bundled scripts",)
        if i == 0 else ())
    assert skillfmt.validate_skill_md(case["text"], folder_name=case["install_name"],
                                      managed=True, expect=case["lock"]) == []


def test_reference_long_name_is_the_disambiguated_form():
    case = oracle_case(2)
    assert case["install_name"] == (
        "some-github-owner-an-extremely-long-imported-skill-name-t-" + h6(case["fields"].skill_id))
    assert len(case["install_name"]) == 64


def test_render_skill_md_matches_render_bundle():
    case = oracle_case(0)
    scripts = [a for a in case["attachments"] if a.path.startswith("scripts/")]
    text = skillfmt.render_skill_md(case["fields"], install_name=case["install_name"],
                                    variant="authored", scripts=scripts)
    assert text == case["text"]


# ── Install names ─────────────────────────────────────────────────────────────────────────

SID = "6f1c0000-0000-4000-8000-000000000001"

NAME_CASES = [
    ("pdf-tools", "pdf-tools"),
    ("acme/refund-policy", "acme-refund-policy"),
    ("Acme/Refund-Policy", "acme-refund-policy"),
    ("my_skill.v2", "my-skill-v2"),
    ("Docker Hub  Automation", "docker-hub-automation"),
    ("--weird--name--", "weird-name"),
    ("Ünïcödé Tools", "unicode-tools"),          # diacritics decompose: nothing is lost
    ("café/crème-brûlée", "cafe-creme-brulee"),
    ("日本語スキル", f"skill-{h6(SID)}"),           # nothing Latin survives
    ("example-owner/中文标题", f"example-owner-{h6(SID)}"),  # lossy; only the namespace left
    ("pdf 中文 tools", f"pdf-tools-{h6(SID)}"),     # a lossy base goes straight to the hash
    ("acme/!!!", f"acme-{h6(SID)}"),               # namespace-only (ASCII)
    ("a" * 64, "a" * 64),
    ("a" * 65, "a" * 57 + f"-{h6(SID)}"),
    ("x-" * 100, ("x-" * 29)[:57].rstrip("-") + f"-{h6(SID)}"),
    ("2slides-ppt-generator", "2slides-ppt-generator"),   # a leading digit is fine
    ("pdf-v2", "pdf-v2"),                                 # version-looking tokens pass through
    ("install", f"install-{h6(SID)}"),
    ("search", f"search-{h6(SID)}"),
    ("check", f"check-{h6(SID)}"),
    ("decimalai-skills", f"decimalai-skills-{h6(SID)}"),  # the project plugin's name
    ("decimalai/skills", f"decimalai-skills-{h6(SID)}"),
    # A plain YAML scalar that would not read back as the same string: written as
    # `name: 404` a loader gets the integer 404, and ADK, Strands and CrewAI reject it.
    ("404", f"404-{h6(SID)}"),
    ("yes", f"yes-{h6(SID)}"),
    ("null", f"null-{h6(SID)}"),
    ("0x1f", f"0x1f-{h6(SID)}"),
    ("2020-01-01", f"2020-01-01-{h6(SID)}"),
    ("2020-13-45", f"2020-13-45-{h6(SID)}"),             # an invalid date makes PyYAML raise
]


@pytest.mark.parametrize(("name", "expected"), NAME_CASES)
def test_install_name_for(name, expected):
    got = skillfmt.install_name_for(name, SID)
    assert got == expected
    assert skillfmt.NAME_RE.match(got) and len(got) <= 64
    assert yaml.safe_load(f"name: {got}")["name"] == got  # safe to write plain


def test_install_name_base_and_disambiguated():
    assert skillfmt.install_name_base("日本語スキル") == ""
    assert skillfmt.install_name_base("Acme/Refund_Policy.v2") == "acme-refund-policy-v2"
    assert skillfmt.install_name_disambiguated("acme/refund-policy", SID) == (
        f"acme-refund-policy-{h6(SID)}")
    assert skillfmt.install_name_disambiguated("日本語", SID) == f"skill-{h6(SID)}"
    assert skillfmt.install_name_forms("acme/refund-policy", SID) == (
        "acme-refund-policy", f"acme-refund-policy-{h6(SID)}")
    # When the base itself is refused, both forms are the hashed one.
    assert skillfmt.install_name_forms("日本語", SID) == (f"skill-{h6(SID)}",) * 2
    assert "decimalai-skills" in skillfmt.RESERVED
    assert {"install", "search", "check"} <= skillfmt.RESERVED


def test_disambiguated_form_is_always_a_valid_plain_name():
    names = [n for n, _ in NAME_CASES] + ["2024-01-02", "0b101", "0777", "true", "on", "off"]
    for n in names:
        for sid in (SID, "other-id", str(uuid.UUID(int=7))):
            got = skillfmt.install_name_disambiguated(n, sid)
            assert skillfmt.NAME_RE.match(got) and len(got) <= 64, got
            assert yaml.safe_load(f"name: {got}")["name"] == got, got


def test_choose_install_name():
    other = "11aa0000-0000-4000-8000-000000000002"
    base, alt = "acme-refund-policy", f"acme-refund-policy-{h6(SID)}"
    choose = skillfmt.choose_install_name
    assert choose("acme/refund-policy", SID, {}) == base
    assert choose("acme/refund-policy", SID, {"pdf-tools": other}) == base
    assert choose("acme/refund-policy", SID, {base: other}) == alt          # base collision
    assert choose("acme/refund-policy", SID, {base: None}) == alt           # an unmanaged folder
    assert choose("acme/refund-policy", SID, {"old-name": SID, base: None}) == "old-name"  # kept
    with pytest.raises(InstallNameCollision) as exc:
        choose("acme/refund-policy", SID, {base: other, alt: "22bb"})       # double collision
    assert exc.value.install_name == alt
    with pytest.raises(InstallNameCollision):
        choose("日本語スキル", SID, {f"skill-{h6(SID)}": other})             # hashed form taken


def test_choose_install_name_is_case_insensitive():
    # macOS and Windows checkouts are case-insensitive: `Acme-Refund-Policy/` and
    # `acme-refund-policy/` are one directory, so a hand-made folder that differs only in
    # case takes the name.
    other = "11aa0000-0000-4000-8000-000000000002"
    base, alt = "acme-refund-policy", f"acme-refund-policy-{h6(SID)}"
    choose = skillfmt.choose_install_name
    assert choose("acme/refund-policy", SID, {"Acme-Refund-Policy": None}) == alt
    assert choose("acme/refund-policy", SID, {"ACME-REFUND-POLICY": other}) == alt
    with pytest.raises(InstallNameCollision) as exc:
        choose("acme/refund-policy", SID, {"Acme-Refund-Policy": None, alt.upper(): None})
    assert exc.value.install_name == alt
    # A locked skill still keeps the key it was recorded under, exactly as written.
    assert choose("acme/refund-policy", SID, {"Old-Name": SID, base: None}) == "Old-Name"
    assert choose("acme/refund-policy", SID, {"pdf-tools": other}) == base


# ── YAML quoting ──────────────────────────────────────────────────────────────────────────

TRICKY = [
    "plain words", "", "  padded  ", 'say "hi"', "back\\slash", "a: b", "key: value: more",
    "- leading dash", "# leading hash", "? question", "[list]", "{map: 1}", "&anchor", "*alias",
    "!tag", "%directive", "@at", "`tick`", "'single'", "yes", "no", "null", "~", "123", "1.5",
    "line\u2028sep", "para\u2029sep", "nel\u0085here", "bom\ufeffhere", "tab\there",
    "nl\nhere", "cr\rhere", "nul\x00", "bell\x07", "esc\x1b[0m", "del\x7f", "c1\x80\x9f",
    "nonchar\ufffe\uffff", "emoji 🎉 and 中文 and ñ", "---", "----", "a --- b", "-- -- --",
    "--x---y----", "...", "end with dash-", "\u00a0nbsp",
]


@pytest.mark.parametrize("s", TRICKY)
def test_yaml_quote_round_trips_through_pyyaml(s):
    q = skillfmt.yaml_quote(s)
    assert q.startswith('"') and q.endswith('"')
    for ch in ("\n", "\r", "\x85", "\u2028", "\u2029", "\ufeff"):
        assert ch not in q  # a single physical line, whatever the input
    assert "---" not in q   # ADK splits the whole file on the first '---'
    assert yaml.safe_load(f"k: {q}") == {"k": s}


def test_yaml_quote_exact_escapes():
    assert skillfmt.yaml_quote('a"b\\c') == '"a\\"b\\\\c"'
    assert skillfmt.yaml_quote("x\u2028y") == '"x\\u2028y"'
    assert skillfmt.yaml_quote("\x00\x1f\x7f\x85\ufeff") == '"\\u0000\\u001f\\u007f\\u0085\\ufeff"'
    assert skillfmt.yaml_quote("ñ 🎉") == '"ñ 🎉"'  # non-ASCII is written literally
    assert skillfmt.yaml_quote("a---b") == '"a--\\u002db"'
    assert skillfmt.yaml_quote("------") == '"--\\u002d--\\u002d"'


def test_json_dumps_is_not_enough():
    # The reason yaml_quote exists. JSON with non-ASCII kept literal writes NEL raw, and
    # PyYAML folds it into a space: the value does not read back.
    nel = "a\u0085b"
    assert yaml.safe_load(f"k: {json.dumps(nel, ensure_ascii=False)}") != {"k": nel}
    # It writes U+2028 and U+2029 raw too. PyYAML keeps them inside quotes, but they end a
    # line for str.splitlines() (the openai-agents frontmatter reader) and for JavaScript.
    for ch in ("\u2028", "\u2029", "\u0085"):
        line = f"k: {json.dumps('a' + ch + 'b', ensure_ascii=False)}"
        assert len(line.splitlines()) == 2
        assert len(f"k: {skillfmt.yaml_quote('a' + ch + 'b')}".splitlines()) == 1


def test_tricky_descriptions_round_trip_through_a_render():
    base = oracle_case(0)["fields"]
    for s in TRICKY:
        desc = f"Use when {s} matters."
        fields = _replace(base, description=desc, registry_name=f"acme/{s}",
                          category=s or None)
        text = skillfmt.render_skill_md(fields, install_name="acme-refund-policy",
                                        variant="foreign")
        fm, body = skillfmt.parse_skill_md_text(text)
        expected_desc = re.sub(r"\s+", lambda m: " " if re.search(r"[\r\n]", m.group())
                               else m.group(), desc.strip())
        assert fm["description"] == expected_desc
        assert fm["metadata"]["decimalai-name"] == f"acme/{s}"
        assert body.strip() == base.body.strip()
        # ADK: content.split("---", 2), then yaml.safe_load(parts[1]).
        parts = text.split("---", 2)
        assert yaml.safe_load(parts[1]) == fm and parts[2].strip() == base.body.strip()


def _replace(fields: RenderFields, **kw: Any) -> RenderFields:
    import dataclasses

    return dataclasses.replace(fields, **kw)


# ── allowed-tools ─────────────────────────────────────────────────────────────────────────

SCRIPT_ATT = Attachment("scripts/run.py", "print(1)\n", executable=True)
TOOL_MATRIX = {
    # (variant, token, scripts?) -> rendered value
    ("authored", "Read", True): "Read",
    ("authored", "Read", False): "Read",
    ("authored", "Bash", True): "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/run.py *)",
    ("authored", "Bash", False): None,
    ("authored", "Bash(git log *)", True): "Bash(git log *)",
    ("authored", "Bash(git log *)", False): "Bash(git log *)",
    ("authored", "Write", True): "Write",
    ("authored", "Write", False): "Write",
    ("authored", "mcp__x", True): "mcp__x",
    ("authored", "mcp__x", False): "mcp__x",
    ("foreign", "Read", True): "Read",
    ("foreign", "Read", False): "Read",
    ("foreign", "Bash", True): None,
    ("foreign", "Bash", False): None,
    ("foreign", "Bash(git log *)", True): None,
    ("foreign", "Bash(git log *)", False): None,
    ("foreign", "Write", True): None,
    ("foreign", "Write", False): None,
    ("foreign", "mcp__x", True): None,
    ("foreign", "mcp__x", False): None,
    # A wildcard scope on a read-only tool is the bare tool. Importers wrote upstream
    # "Read(*), Grep(*), Glob(*)" lists this way; dropping them took every read grant.
    ("authored", "Read(*)", True): "Read(*)",
    ("authored", "Read(*)", False): "Read(*)",
    ("foreign", "Read(*)", True): "Read",
    ("foreign", "Read(*)", False): "Read",
    ("foreign", "Grep(**)", False): "Grep",
    ("foreign", "Glob( * )", False): "Glob",
    # A real scope stays the author's: kept for authored skills, dropped for foreign ones.
    ("authored", "Read(./references/**)", False): "Read(./references/**)",
    ("foreign", "Read(./references/**)", True): None,
    ("foreign", "Read(./references/**)", False): None,
}


@pytest.mark.parametrize(("key", "expected"), sorted(TOOL_MATRIX.items(), key=str))
def test_allowed_tools_matrix(key, expected):
    variant, token, has_scripts = key
    pol = skillfmt.allowed_tools_for([token], variant=variant,
                                     scripts=[SCRIPT_ATT] if has_scripts else [])
    assert pol.value == expected
    changed = expected != token
    assert bool(pol.notes) == changed
    if changed:
        assert all(n.startswith("allowed-tools: ") and f"'{token}'" in n for n in pol.notes)
    if variant == "foreign" and expected is None:
        assert "not authored by your organization" in pol.notes[0]


def test_allowed_tools_authored_interpreters():
    scripts = [
        Attachment("scripts/a.py", "x", executable=True),
        Attachment("scripts/b.sh", "x", executable=True),
        Attachment("scripts/c.js", "x", executable=True),
        Attachment("scripts/d", "x", executable=True, execution_language="bash"),
        Attachment("scripts/e.rb", "x", executable=True, execution_language="ruby"),
        Attachment("scripts/f.txt", "x", executable=True, execution_language="javascript"),
    ]
    pol = skillfmt.allowed_tools_for(["Read", "Bash"], variant="authored", scripts=scripts)
    assert pol.tokens == (
        "Read",
        "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/a.py *)",
        "Bash(bash ${CLAUDE_SKILL_DIR}/scripts/b.sh *)",
        "Bash(node ${CLAUDE_SKILL_DIR}/scripts/c.js *)",
        "Bash(bash ${CLAUDE_SKILL_DIR}/scripts/d *)",
        "Bash(${CLAUDE_SKILL_DIR}/scripts/e.rb *)",
        "Bash(node ${CLAUDE_SKILL_DIR}/scripts/f.txt *)",
    )


def test_allowed_tools_never_scopes_a_path_a_rule_cannot_match():
    pol = skillfmt.allowed_tools_for(
        ["Bash"], variant="authored",
        scripts=[Attachment("scripts/my script.py", "x", executable=True),
                 Attachment("scripts/ok.py", "x", executable=True)])
    assert pol.value == "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ok.py *)"
    assert any("scripts/my script.py" in n for n in pol.notes)


@pytest.mark.parametrize("bare", ["Bash", "Bash(*)", "Bash(*:*)", "Bash( * )"])
def test_unrestricted_bash_counts_as_bare(bare):
    assert skillfmt.is_bare_bash(bare)
    pol = skillfmt.allowed_tools_for([bare], variant="authored", scripts=[SCRIPT_ATT])
    assert pol.value == "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/run.py *)"
    assert skillfmt.allowed_tools_for([bare], variant="authored", scripts=[]).value is None
    assert not skillfmt.is_bare_bash("Bash(git *)")


@pytest.mark.parametrize(("raw", "tokens"), [
    (None, []),
    ("", []),
    ([], []),
    ("Read Grep", ["Read", "Grep"]),
    (["Read Grep", "Glob"], ["Read", "Grep", "Glob"]),
    # what importers did to comma-separated upstream strings
    (["Read,", "Grep,", "Glob"], ["Read", "Grep", "Glob"]),
    ("Read,Grep, Glob", ["Read", "Grep", "Glob"]),
    (["Bash(git", "log", "*)", "Read,"], ["Bash(git log *)", "Read"]),
    (["Bash(git log:*),", "Read"], ["Bash(git log:*)", "Read"]),
    ("Read, Bash(git diff, git log)", ["Read", "Bash(git diff, git log)"]),
    (["Bash(unclosed", "Read"], ["Bash(unclosed", "Read"]),
    (["Bash(git", "Read", "Bash(x y)"], ["Bash(git", "Read", "Bash(x y)"]),
    (["Read", "Read,", " Read "], ["Read"]),
    ([None, 3, "Read", {"x": 1}], ["Read"]),
    ("Read)", ["Read)"]),
])
def test_tokenizer_repairs_import_damage(raw, tokens):
    assert skillfmt.tokenize_allowed_tools(raw) == tokens
    assert skillfmt.tokenize_allowed_tools(tokens) == tokens  # idempotent


def test_foreign_keeps_read_grep_glob_through_damage():
    # Every Read/Grep/Glob token carries a comma: without the repair, nothing survives.
    pol = skillfmt.allowed_tools_for(["Read,", "Grep,", "Glob,", "Bash,", "Write"],
                                     variant="foreign")
    assert pol.value == "Read Grep Glob"
    assert [n for n in pol.notes] == [
        "allowed-tools: dropped 'Bash' (not authored by your organization)",
        "allowed-tools: dropped 'Write' (not authored by your organization)",
    ]


# A stored shape from the registry (MCP server renamed): every token carries a `(*)` scope
# and a trailing comma. Before the wildcard rule, a foreign rendering kept nothing.
PROD_WILDCARD_SHAPE = ["mcp__acme-tools__core_memory(*),", "Read(*),", "AskUserQuestion"]


def test_foreign_keeps_wildcard_read_only_tools():
    pol = skillfmt.allowed_tools_for(PROD_WILDCARD_SHAPE, variant="foreign")
    assert pol.value == "Read"
    assert pol.notes == (
        "allowed-tools: dropped 'mcp__acme-tools__core_memory(*)' (not authored by your "
        "organization)",
        "allowed-tools: rewrote 'Read(*)' to 'Read' (a wildcard scope is the bare tool)",
        "allowed-tools: dropped 'AskUserQuestion' (not authored by your organization)",
    )
    # The team-orchestration shape: all three read-only grants survive, each once.
    pol = skillfmt.allowed_tools_for(
        ["Agent(*),", "Read(*),", "Write(*),", "Bash(*),", "Glob(*),", "Grep(*),", "Read",
         "Grep( ** )"], variant="foreign")
    assert pol.value == "Read Glob Grep"
    # Authored skills keep their own spelling: a wildcard Read is not rewritten.
    assert skillfmt.allowed_tools_for(PROD_WILDCARD_SHAPE, variant="authored").value == (
        "mcp__acme-tools__core_memory(*) Read(*) AskUserQuestion")


@pytest.mark.parametrize("token", ["Read(./references/**)", "Grep(src/*)", "Glob(*.md)",
                                   "Read(*", "Read(*)x", "Read(***)", "Reader(*)", "read(*)"])
def test_foreign_drops_every_other_read_only_form(token):
    # A real scope is narrower than the bare tool, but its meaning is Claude's to define and
    # none is stored today: a foreign skill keeps exactly the three bare names.
    pol = skillfmt.allowed_tools_for([token], variant="foreign")
    assert pol.value is None
    assert pol.notes and all("not authored by your organization" in n for n in pol.notes)


# ── Rendering rules ───────────────────────────────────────────────────────────────────────


def test_metadata_key_order_and_optional_keys():
    fields = _replace(oracle_case(0)["fields"], skill_type="capability", skill_scope="public",
                      source="https://app.decimal.ai/s/acme-refund-policy@3/SKILL.md",
                      invocation_mode="any", license=None, license_spdx="MIT",
                      compatibility=None, allowed_tools=None)
    text = skillfmt.render_skill_md(fields, install_name="acme-refund-policy", variant="foreign")
    fm, _ = skillfmt.parse_skill_md_text(text)
    assert list(fm) == ["name", "description", "license", "metadata"]
    assert fm["license"] == "MIT"
    assert list(fm["metadata"]) == [
        "decimalai-skill-id", "decimalai-version-id", "decimalai-version",
        "decimalai-body-sha256", "decimalai-name", "decimalai-format", "decimalai-invocation",
        "decimalai-category", "decimalai-skill-scope", "decimalai-skill-type",
        "decimalai-source",
    ]
    assert fm["metadata"]["decimalai-invocation"] == "any"
    assert "disable-model-invocation" not in fm
    assert all(isinstance(v, str) for v in fm["metadata"].values())


def test_description_newlines_collapse_and_long_values_clip():
    long_desc = ("word " * 300).strip()  # 1499 characters
    fields = _replace(oracle_case(0)["fields"], description="  First line\r\n\n  second\tline  ",
                      compatibility=["python>=3.10", " ", "uv"])
    fm, _ = skillfmt.parse_skill_md_text(skillfmt.render_skill_md(
        fields, install_name="x", variant="foreign"))
    assert fm["description"] == "First line second\tline"
    assert fm["compatibility"] == "python>=3.10; uv"

    fields = _replace(fields, description=long_desc, compatibility="c " * 400)
    bundle = skillfmt.render_bundle(fields, [], install_name="x", variant="foreign")
    fm, _ = skillfmt.parse_skill_md_text(bundle.skill_md)
    assert len(fm["description"]) <= 1024 and long_desc.startswith(fm["description"])
    assert fm["description"].endswith("word")  # cut on a word boundary
    assert len(fm["compatibility"]) <= 500 and fm["compatibility"].endswith("c")
    assert bundle.policy_notes[:2] == (
        f"description: clipped from 1499 to {len(fm['description'])} characters",
        f"compatibility: clipped from 799 to {len(fm['compatibility'])} characters",
    )
    assert bundle.description_sha256 == sha(fm["description"])


def test_body_bytes_and_hash():
    fields = _replace(oracle_case(0)["fields"], body="\n\n  # Title\n\nText  \n\n\n")
    text = skillfmt.render_skill_md(fields, install_name="x", variant="foreign")
    assert text.endswith("---\n# Title\n\nText\n")
    assert skillfmt.body_sha256(fields.body) == sha("# Title\n\nText")
    assert f'decimalai-body-sha256: "{sha("# Title" + chr(10) + chr(10) + "Text")}"' in text


@pytest.mark.parametrize(("stored", "written"), [
    ("# T\r\n\r\nText\r\n", "# T\n\nText"),                 # a Windows-authored body
    ("# T\n\nOne stray\rCR\n", "# T\n\nOne stray\nCR"),      # a lone CR is a line break
    ("\r\n# T\r\rText\r\n\r\n", "# T\n\nText"),
])
def test_body_line_endings_are_written_as_lf(stored, written):
    # A stored body with CR renders: its line endings are written as LF (CRLF first, then
    # a lone CR), the same translation the platform's SKILL.md parser applies. The hash is
    # over the body as written, so the server hashing the stored body, the stamp, and a
    # client hashing the file it reads all agree.
    fields = _replace(oracle_case(0)["fields"], body=stored)
    bundle = skillfmt.render_bundle(fields, [], install_name="acme-refund-policy",
                                    variant="foreign")
    text = bundle.skill_md
    assert "\r" not in text and text.endswith("---\n" + written + "\n")
    assert skillfmt.body_sha256(stored) == sha(written) == bundle.body_sha256
    assert f'decimalai-body-sha256: "{sha(written)}"' in text
    assert "body: wrote CR and CRLF line endings as LF" in bundle.policy_notes
    assert skillfmt.validate_skill_md(text, folder_name="acme-refund-policy", managed=True,
                                      expect={"body_sha256": sha(written)}) == []


def test_body_without_cr_is_hashed_as_before():
    for body in ("# T\n\nText", "\n\n  # T\n\nText  \n\n", "x\u2028", "a\u0085b"):
        assert skillfmt.body_sha256(body) == sha(body.strip())
    bundle = skillfmt.render_bundle(_replace(oracle_case(0)["fields"], body="# T\n\nText\r\n"),
                                    [], install_name="acme-refund-policy", variant="foreign")
    assert not any(n.startswith("body:") for n in bundle.policy_notes)  # only an edge CR


@pytest.mark.parametrize(("change", "message"), [
    ({"description": "   \n "}, "description"),
    ({"description": None}, "description"),
    ({"body": " \n\t"}, "body"),
    ({"version": 0}, "version"),
    ({"skill_id": ""}, "skill_id"),
    ({"compatibility": {"python": "3"}}, "compatibility"),
])
def test_render_refuses_invalid_fields(change, message):
    fields = _replace(oracle_case(0)["fields"], **change)
    with pytest.raises(RenderError) as exc:
        skillfmt.render_bundle(fields, [], install_name="acme-refund-policy", variant="foreign")
    assert exc.value.code == "render_invalid" and message in str(exc.value)


@pytest.mark.parametrize("install_name", ["acme/refund-policy", "Acme", "a--b", "x" * 65, "404",
                                          ""])
def test_render_refuses_an_invalid_install_name(install_name):
    with pytest.raises(RenderError):
        skillfmt.render_skill_md(oracle_case(0)["fields"], install_name=install_name,
                                 variant="foreign")


def test_render_rejects_unknown_variant_and_format():
    with pytest.raises(ValueError):
        skillfmt.render_skill_md(oracle_case(0)["fields"], install_name="x", variant="owned")
    with pytest.raises(ValueError):
        skillfmt.render_skill_md(oracle_case(0)["fields"], install_name="x", variant="foreign",
                                 format=2)
    assert skillfmt.SUPPORTED_FORMATS == frozenset({1})


def test_attachments_modes_binary_and_order():
    fields = oracle_case(0)["fields"]
    bundle = skillfmt.render_bundle(fields, [
        Attachment("templates/b.md", "b"),
        Attachment("assets/logo.png", None),
        Attachment("scripts/run.sh", "echo hi\r\n", executable=True),
        Attachment("references/a.md", "ä"),
    ], install_name="x", variant="authored")
    assert [f.path for f in bundle.files] == [
        "SKILL.md", "references/a.md", "scripts/run.sh", "templates/b.md"]
    modes = {f.path: f.mode for f in bundle.files}
    assert modes["scripts/run.sh"] == "0755" and modes["references/a.md"] == "0644"
    a = next(f for f in bundle.files if f.path == "references/a.md")
    assert a.size == 2 and a.sha256 == sha("ä".encode())
    run = next(f for f in bundle.files if f.path == "scripts/run.sh")
    assert run.content == "echo hi\r\n"  # attachment bytes are served verbatim
    assert bundle.unavailable == (
        skillfmt.Unavailable("assets/logo.png", "binary attachments are not stored"),)
    # The authored rewrite scopes bare Bash to the scripts that are actually written.
    assert "Bash(bash ${CLAUDE_SKILL_DIR}/scripts/run.sh *)" in bundle.skill_md


@pytest.mark.parametrize("path", ["templates/SKILL.md", "references/skill.md",
                                  "assets/deep/er/Skill.MD", "references/SKILL.md/notes.md",
                                  "scripts/skill.md/x.py"])
def test_nested_skill_md_is_never_served(path):
    # A second SKILL.md in a managed folder is read as another skill, at any depth.
    bundle = skillfmt.render_bundle(oracle_case(0)["fields"], [
        Attachment(path, "---\nname: other\n---\nbody\n"),
        Attachment("references/fees.md", FEES),
    ], install_name="x", variant="foreign")
    assert [f.path for f in bundle.files] == ["SKILL.md", "references/fees.md"]
    assert [u.path for u in bundle.unavailable] == [path]
    assert "second skill" in bundle.unavailable[0].reason
    assert ("F15", "loader") in rules(skillfmt.validate_attachment_path(path))


@pytest.mark.parametrize("path", [
    "/etc/passwd", "../x.md", "scripts/../../x", "scripts/./x", "scripts//x", "scripts\\x.py",
    "eval.yaml", "notes/x.md", "scripts", "scripts/", "C:/x/y", "scripts/x\ny", "",
])
def test_unsafe_attachment_paths_are_refused(path):
    assert ("F14", "sdk") in rules(skillfmt.validate_attachment_path(path))
    with pytest.raises(RenderError) as exc:
        skillfmt.render_bundle(oracle_case(0)["fields"], [Attachment(path, "x")],
                               install_name="x", variant="foreign")
    assert exc.value.code == "render_invalid"


@pytest.mark.parametrize("path", ["../../SKILL.md", "/etc/SKILL.md", "scripts\\..\\..\\SKILL.md",
                                  "SKILL.md", "scripts/../SKILL.md", "notes/SKILL.md",
                                  "references//SKILL.md"])
def test_an_unsafe_path_named_skill_md_is_refused_not_listed(path):
    # Path safety comes first: the skip-and-list for SKILL.md applies only to a path that
    # is already safe, so an unsafe string never reaches `unavailable` (and the lock).
    problems = rules(skillfmt.validate_attachment_path(path))
    assert ("F14", "sdk") in problems and ("F15", "loader") in problems
    with pytest.raises(RenderError) as exc:
        skillfmt.render_bundle(oracle_case(0)["fields"], [Attachment(path, "x")],
                               install_name="x", variant="foreign")
    assert exc.value.code == "render_invalid" and "unsafe" in str(exc.value)


@pytest.mark.parametrize("paths", [
    ["references/a.md", "references/a.md"],
    ["references/A.md", "references/a.md"],      # one folder on macOS and Windows
    ["scripts/tool", "scripts/tool/run.py"],     # a file where a directory must go
])
def test_conflicting_attachment_paths_are_a_render_error(paths):
    with pytest.raises(RenderError) as exc:
        skillfmt.render_bundle(oracle_case(0)["fields"], [Attachment(p, "x") for p in paths],
                               install_name="x", variant="foreign")
    assert paths[0] in str(exc.value) or paths[1] in str(exc.value)


def test_valid_attachment_paths():
    for p in ("scripts/a.py", "references/sub/b.md", "templates/t.txt", "assets/i.svg"):
        assert skillfmt.validate_attachment_path(p) == []


# ── Render identity ───────────────────────────────────────────────────────────────────────


def test_trigger_fields_and_render_hash():
    f = _replace(oracle_case(0)["fields"], license=None, license_spdx="Apache-2.0",
                 allowed_tools=["Read,", "Bash"], description="Apply\n rules")
    tf = skillfmt.trigger_fields(f)
    assert tf == {"allowed_tools": ["Read", "Bash"], "compatibility": "Requires python3",
                  "description": "Apply rules", "invocation_mode": "model",
                  "license": "Apache-2.0"}
    canonical = json.dumps(tf, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert skillfmt.render_hash(f) == sha(canonical)
    # Informational fields never move the hash; trigger fields always do.
    for change in ({"category": "other"}, {"skill_type": "x"}, {"skill_scope": "y"},
                   {"source": "https://elsewhere"}, {"body": "different body"},
                   {"version": 9}, {"allowed_tools": ["Read", "Bash", "Read,"]}):
        assert skillfmt.render_hash(_replace(f, **change)) == skillfmt.render_hash(f), change
    for change in ({"description": "Apply other rules"}, {"license_spdx": "MIT"},
                   {"compatibility": "Requires node"}, {"allowed_tools": ["Read"]},
                   {"invocation_mode": "user"}):
        assert skillfmt.render_hash(_replace(f, **change)) != skillfmt.render_hash(f), change


def test_bundle_carries_its_identity():
    case = oracle_case(1)
    b = skillfmt.render_bundle(case["fields"], [], install_name=case["install_name"],
                               variant="authored")
    assert b.render_hash == skillfmt.render_hash(case["fields"])
    assert b.body_sha256 == sha(case["fields"].body.strip())
    assert b.invocation == "user" and b.variant == "authored" and b.format == 1
    d = b.to_dict()
    assert d["render"] == {"format": 1, "hash": b.render_hash, "variant": "authored"}
    assert d["files"][0] == {"path": "SKILL.md", "sha256": sha(case["text"]),
                             "size": len(case["text"].encode()), "mode": "0644",
                             "encoding": "utf-8", "content": case["text"]}
    assert json.loads(json.dumps(d)) == d


# ── Parsing ───────────────────────────────────────────────────────────────────────────


def test_split_frontmatter_is_line_exact():
    assert skillfmt.split_frontmatter("---\na: 1\n---\nbody\n") == ("a: 1\n", "body\n")
    assert skillfmt.split_frontmatter("---\nd: \"x --- y\"\n---\nb") == ('d: "x --- y"\n', "b")
    assert skillfmt.split_frontmatter("---\na: 1\n----\nb\n---\n") == ("a: 1\n----\nb\n", "")
    assert skillfmt.split_frontmatter("---\r\na: 1\r\n---\r\nb") == ("a: 1\r\n", "b")
    assert skillfmt.split_frontmatter("no fence\n") is None
    assert skillfmt.split_frontmatter("---\na: 1\n") is None
    assert skillfmt.split_frontmatter("\ufeff---\na: 1\n---\n") is None
    assert skillfmt.split_frontmatter(" ---\na: 1\n---\n") is None


def test_parse_rejects_duplicate_keys_and_non_mappings():
    with pytest.raises(skillfmt.SkillMdParseError, match="duplicate"):
        skillfmt.parse_skill_md_text("---\nname: a\nname: b\n---\nbody\n")
    with pytest.raises(skillfmt.SkillMdParseError, match="duplicate"):
        skillfmt.parse_skill_md_text("---\nmetadata:\n  k: '1'\n  k: '2'\n---\nbody\n")
    for bad in ("---\n- a\n---\nbody\n", "---\n---\nbody\n", "---\n: : :\n---\nb\n",
                "---\nname: 2020-13-45\n---\nb\n", "---\nx: !!python/object:os.system ''\n---\n"):
        with pytest.raises(skillfmt.SkillMdParseError):
            skillfmt.parse_skill_md_text(bad)
    fm, body = skillfmt.parse_skill_md_text("---\nname: a\nallowed-tools:\n  - Read\n---\nb\n")
    assert fm == {"name": "a", "allowed-tools": ["Read"]} and body == "b\n"


# ── Validation: every loader defect, with its rule id and scope ───────────────────────────
#
# Each case is one defect in the rendered reference skill. The loader columns are the
# cells where at least one of the eight native loaders raised, dropped or renamed the
# skill; the sdk rows are this format's own rules. Folder-level cells (a symlinked skill
# folder, resource or root; SKILL.md deeper than the skill folder; one name in two roots)
# are graded by the SDK's folder checks, not by this text validator.

REFUND = oracle_case(0)
ALLOWED_LINE = 'allowed-tools: "Read Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/compute_refund.py *)"'


def _mutate(old: str, new: str) -> str:
    assert old in REFUND["text"], old
    return REFUND["text"].replace(old, new, 1)


LEDGER = [
    # (case id, text, folder name, expected (rule, scope) set) — unmanaged grading
    ("list allowed-tools", _mutate(ALLOWED_LINE, "allowed-tools:\n  - Read\n  - Bash"),
     "acme-refund-policy", {("F6a", "loader")}),
    ("list compatibility", _mutate('compatibility: "Requires python3"',
                                   'compatibility:\n  - "python>=3.10"'),
     "acme-refund-policy", {("F5", "loader")}),
    ("nested metadata", _mutate("metadata:\n", 'metadata:\n  decimalai:\n    version: "3"\n'),
     "acme-refund-policy", {("F8", "loader")}),
    ("unquoted number in metadata", _mutate('decimalai-version: "3"', "decimalai-version: 3"),
     "acme-refund-policy", {("F8", "loader")}),
    ("namespaced name", _mutate("name: acme-refund-policy", "name: acme/refund-policy"),
     "acme-refund-policy", {("F3", "loader")}),
    ("folder != name", REFUND["text"], "acme-refund-policy-v3", {("F3", "loader")}),
    ("unknown top-level key", _mutate("metadata:\n", 'decimalai-version-id: "x"\nmetadata:\n'),
     "acme-refund-policy", {("F2", "sdk")}),
    ("uppercase name", _mutate("name: acme-refund-policy", "name: Acme-Refund-Policy"),
     "acme-refund-policy", {("F3", "loader")}),
    ("name over 64", _mutate("name: acme-refund-policy", "name: " + "a" * 65), "a" * 65,
     {("F3", "loader")}),
    ("double hyphen", _mutate("name: acme-refund-policy", "name: acme--refund"), "acme--refund",
     {("F3", "loader")}),
    ("name read as a number", _mutate("name: acme-refund-policy", "name: 404"), "404",
     {("F3", "loader")}),
    ("description over 1024", _mutate(f'description: "{REFUND["fields"].description}"',
                                      'description: "' + "d" * 1025 + '"'),
     "acme-refund-policy", {("F4", "loader")}),
    ("empty description", _mutate(f'description: "{REFUND["fields"].description}"',
                                  'description: ""'), "acme-refund-policy", {("F4", "loader")}),
    ("compatibility over 500", _mutate('compatibility: "Requires python3"',
                                       'compatibility: "' + "c" * 501 + '"'),
     "acme-refund-policy", {("F5", "loader")}),
    ("license list", _mutate('license: "Apache-2.0"', "license:\n  - MIT"),
     "acme-refund-policy", {("F9", "loader")}),
    ("metadata null value", _mutate('decimalai-category: "support"', "decimalai-category:"),
     "acme-refund-policy", {("F8", "loader")}),
    ("empty body", REFUND["text"].split("---\n# ")[0] + "---\n", "acme-refund-policy",
     {("F10", "loader")}),
    ("'---' inside a value (ADK splits on it)",
     _mutate(f'description: "{REFUND["fields"].description}"', 'description: "a --- b"'),
     "acme-refund-policy", {("F1", "loader")}),
    ("duplicate key", _mutate("metadata:\n", 'name: again\nmetadata:\n'), "acme-refund-policy",
     {("F1", "loader")}),
    ("no closing fence", REFUND["text"].replace("---\n# Acme", "# Acme"), "acme-refund-policy",
     {("F1", "loader")}),
    ("byte-order mark", "\ufeff" + REFUND["text"], "acme-refund-policy", {("F1", "loader")}),
    ("bare Bash", _mutate(ALLOWED_LINE, "allowed-tools: Read Bash"), "acme-refund-policy",
     {("F6b", "sdk")}),
    ("Bash(*)", _mutate(ALLOWED_LINE, 'allowed-tools: "Read Bash(*)"'), "acme-refund-policy",
     {("F6b", "sdk")}),
    ("disable-model-invocation false", _mutate("metadata:\n",
                                               "disable-model-invocation: false\nmetadata:\n"),
     "acme-refund-policy", {("F7", "sdk")}),
]


@pytest.mark.parametrize(("case_id", "text", "folder", "expected"), LEDGER,
                         ids=[c[0] for c in LEDGER])
def test_validator_rejects_each_ledger_defect(case_id, text, folder, expected):
    got = skillfmt.validate_skill_md(text, folder_name=folder, managed=False)
    assert rules(got) == expected, [p.message for p in got]
    for p in got:
        assert p.scope == skillfmt.RULES[p.rule][0] and p.message


def test_baseline_passes_and_managed_mode_grades_more():
    text = REFUND["text"]
    assert skillfmt.validate_skill_md(text, folder_name="acme-refund-policy") == []
    assert skillfmt.validate_skill_md(text, folder_name="acme-refund-policy", managed=True,
                                      expect=REFUND["lock"]) == []
    assert skillfmt.validate_skill_md(text.encode("utf-8"), folder_name="acme-refund-policy",
                                      managed=True) == []
    assert rules(skillfmt.validate_skill_md(b"\xff\xfe---", folder_name="x")) == {("F1", "loader")}


@pytest.mark.parametrize(("change", "expected"), [
    ({"version_id": "other"}, {("F11", "sdk")}),
    ({"version": 4}, {("F11", "sdk")}),
    ({"name": "acme/other"}, {("F11", "sdk")}),
    ({"skill_id": "other"}, {("F11", "sdk")}),
    ({"invocation": "user"}, {("F11", "sdk")}),
    ({"render": {"format": 2}}, {("F11", "sdk")}),
    ({"body_sha256": "0" * 64}, {("F12", "sdk")}),
])
def test_managed_mode_compares_against_the_lock(change, expected):
    lock = {**REFUND["lock"], **change}
    got = skillfmt.validate_skill_md(REFUND["text"], folder_name="acme-refund-policy",
                                     managed=True, expect=lock)
    assert rules(got) == expected


@pytest.mark.parametrize(("text", "expected"), [
    (REFUND["text"].replace("Sentinel:", "Edited sentinel:"), {("F12", "sdk")}),
    (REFUND["text"] + "\n", {("F13", "sdk")}),
    (REFUND["text"].rstrip("\n"), {("F13", "sdk")}),
    # A CRLF checkout (git autocrlf) is a line-ending problem, not an edit: the body still
    # hashes to its stamp.
    (REFUND["text"].replace("\n", "\r\n"), {("F13", "sdk")}),
    # A lone CR is a line break, so inserting one is an edit.
    (REFUND["text"].replace("Sentinel: ", "Sentinel:\r"), {("F12", "sdk"), ("F13", "sdk")}),
    (REFUND["text"].replace('  decimalai-format: "1"\n', ""), {("F11", "sdk")}),
    (REFUND["text"].replace('decimalai-format: "1"', 'decimalai-format: "2"'), {("F11", "sdk")}),
    (REFUND["text"].replace('decimalai-version: "3"', 'decimalai-version: "three"'),
     {("F11", "sdk")}),
    (REFUND["text"].replace('decimalai-invocation: "model"', 'decimalai-invocation: "user"'),
     {("F11", "sdk")}),  # says user-only, but disable-model-invocation is absent
])
def test_managed_only_rules(text, expected):
    got = skillfmt.validate_skill_md(text, folder_name="acme-refund-policy", managed=True)
    assert rules(got) == expected, [p.message for p in got]
    # None of these is a loader defect, so an unmanaged reading lets the file through.
    assert skillfmt.validate_skill_md(text, folder_name="acme-refund-policy") == []


def test_what_sdk_0_13_9_wrote_is_rejected():
    v0139 = (
        "---\nname: acme/refund-policy\n"
        'description: "Apply Acme\'s refund rules when a customer asks for a refund."\n'
        "invocation: user\ndisable-model-invocation: true\nlicense: Apache-2.0\n"
        "allowed-tools:\n  - Read\n  - Bash\nsource: https://app.decimal.ai/s/x\n---\n\n"
        "# Refund policy\n\nBody."
    )
    got = skillfmt.validate_skill_md(v0139, folder_name="acme-refund-policy")
    assert rules(got) == {("F2", "sdk"), ("F3", "loader"), ("F6a", "loader")}
    assert any("metadata" in p.message for p in got if p.rule == "F2")  # says where it moved
    managed = rules(skillfmt.validate_skill_md(v0139, folder_name="acme-refund-policy",
                                               managed=True))
    assert {("F11", "sdk"), ("F13", "sdk")} <= managed


def test_severity():
    loader = Problem("F3", "loader", "m")
    sdk = Problem("F2", "sdk", "m")
    assert skillfmt.is_error(loader, managed=False) and skillfmt.is_error(sdk, managed=True)
    assert not skillfmt.is_error(sdk, managed=False)
    assert skillfmt.is_error(sdk, managed=False, strict=True)
    assert set(skillfmt.RULES) == {f"F{i}" for i in range(1, 16) if i != 6} | {"F6a", "F6b"}


def test_render_self_check_refuses_its_own_defects(monkeypatch):
    # If the renderer ever wrote bytes the validator rejects, nothing is returned.
    monkeypatch.setattr(skillfmt, "yaml_quote", lambda s: s if s else '""')
    with pytest.raises(RenderError) as exc:
        skillfmt.render_skill_md(_replace(REFUND["fields"], description="a: b"),
                                 install_name="acme-refund-policy", variant="foreign")
    assert exc.value.code == "render_invalid"


# ── Hashes, lock_hash, taxonomy ──────────────────────────────────────────────────────

LOCK_SKILLS = {
    "acme-refund-policy": {
        "name": "acme/refund-policy", "skill_id": "6f1c", "version_id": "9b2e", "version": 3,
        "render": {"format": 1, "hash": "a" * 64, "variant": "foreign"},
        "body_sha256": "b" * 64, "description_sha256": "c" * 64,
        "path": ".agents/skills/acme-refund-policy",
        "files": {"SKILL.md": "d" * 64, "scripts/compute_refund.py": "e" * 64},
        "executable": ["scripts/compute_refund.py"], "unavailable": [],
        "source": {"base_url": "https://api.decimal.ai", "org": None},
        "pin": "latest", "invocation": "model", "installed_at": "2026-10-20T10:00:00Z",
    },
    "skill-8544a6": {
        "name": "日本語スキル", "skill_id": "5eed", "version_id": "f00d", "version": 1,
        "render": {"format": 1, "hash": "f" * 64, "variant": "foreign"},
        "body_sha256": "1" * 64, "description_sha256": "2" * 64,
        "path": ".agents/skills/skill-8544a6", "files": {"SKILL.md": "3" * 64},
        "executable": [], "unavailable": [{"path": "assets/x.png", "reason": "binary"}],
        "source": {"base_url": "https://api.decimal.ai", "org": "o1"}, "pin": "exact",
        "invocation": "user", "installed_at": "2026-10-21T00:00:00Z",
    },
}


def test_lock_hash():
    stripped = {k: {kk: vv for kk, vv in e.items() if kk != "installed_at"}
                for k, e in LOCK_SKILLS.items()}
    expected = sha(json.dumps({"root": ".agents/skills", "skills": stripped}, sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False))
    got = skillfmt.lock_hash(".agents/skills", LOCK_SKILLS)
    assert got == expected
    moved = {k: {**e, "installed_at": "2030-01-01T00:00:00Z"} for k, e in LOCK_SKILLS.items()}
    assert skillfmt.lock_hash(".agents/skills", moved) == got  # installed_at never counts
    bumped = {**LOCK_SKILLS, "skill-8544a6": {**LOCK_SKILLS["skill-8544a6"], "version": 2}}
    assert skillfmt.lock_hash(".agents/skills", bumped) != got
    assert skillfmt.lock_hash("other", LOCK_SKILLS) != got
    assert "installed_at" in LOCK_SKILLS["acme-refund-policy"]  # the input is not mutated


def test_canonical_json_and_small_hashes():
    assert skillfmt.canonical_json({"b": 1, "a": "ñ"}) == '{"a":"ñ","b":1}'.encode()
    assert skillfmt.body_sha256("  x \n") == sha("x")
    assert skillfmt.body_sha256("x\u2028") == sha("x")  # Python str.strip, not btrim
    assert skillfmt.description_sha256("Apply rules") == sha("Apply rules")


@pytest.mark.parametrize(("fm", "expected"), [
    ({"metadata": {"decimalai-category": "security"}, "category": "general"},
     {"category": "security"}),
    ({"category": " general "}, {"category": "general"}),
    ({"metadata": {"decimalai-category": ""}, "category": "general"}, {"category": "general"}),
    ({"metadata": "not a map", "category": 3}, {"category": None}),
    ({"metadata": {"decimalai-skill-type": "capability", "decimalai-skill-scope": "public"},
      "skill-type": "preference", "skill-scope": "private"},
     {"skill_type": "capability", "skill_scope": "public"}),
    ({"skill-type": "model-gap", "skill-scope": "private"},
     {"skill_type": "model-gap", "skill_scope": "private"}),
    ({"metadata": {"decimalai-invocation": "user"}, "invocation": "model"}, {"invocation": "user"}),
    ({"metadata": {"decimalai-invocation": "bogus"}, "invocation": "Any"}, {"invocation": "any"}),
    ({"disable-model-invocation": True}, {"invocation": "user"}),
    ({"disable-model-invocation": "true"}, {"invocation": None}),
    ({}, {"category": None, "skill_type": None, "skill_scope": None, "invocation": None}),
])
def test_taxonomy_reads_metadata_first(fm, expected):
    got = skillfmt.taxonomy(fm)
    assert set(got) == {"category", "skill_type", "skill_scope", "invocation"}
    for k, v in expected.items():
        assert got[k] == v, k


def test_taxonomy_reads_a_rendered_file():
    fm, _ = skillfmt.parse_skill_md_text(REFUND["text"])
    assert skillfmt.taxonomy(fm) == {"category": "support", "skill_type": None,
                                     "skill_scope": None, "invocation": "model"}


# ── The vector file ───────────────────────────────────────────────────────────────────


def test_vectors_regenerate_byte_identically():
    assert VECTORS_PATH.read_bytes() == skillfmt.vectors_text().encode("utf-8")


def test_vectors_cli_writes_the_same_bytes(tmp_path):
    out = tmp_path / "v.json"
    root = Path(skillfmt.__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-m", "skillevaluation.skillfmt", "--write-vectors",
                        str(out)], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == VECTORS_PATH.read_bytes()
    r = subprocess.run([sys.executable, "-m", "skillevaluation.skillfmt", "--check-vectors",
                        str(out)], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stderr
    out.write_text("{}\n")
    assert skillfmt.main(["--check-vectors", str(out)]) == 1


def test_vectors_replay_from_the_file_alone():
    """What a client in another language does: read inputs, run them, compare outputs."""
    v = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert v["format"] == 1
    for c in v["install_names"]:
        assert skillfmt.install_name_base(c["name"]) == c["base"]
        assert skillfmt.install_name_disambiguated(c["name"], c["skill_id"]) == c["disambiguated"]
        assert skillfmt.install_name_for(c["name"], c["skill_id"]) == c["install_name"]
    for c in v["choose_install_name"]:
        if "error" in c:
            with pytest.raises(InstallNameCollision):
                skillfmt.choose_install_name(c["name"], c["skill_id"], c["taken"])
        else:
            assert skillfmt.choose_install_name(c["name"], c["skill_id"], c["taken"]) == (
                c["install_name"])
    for c in v["yaml_quote"]:
        assert skillfmt.yaml_quote(c["input"]) == c["output"]
    for c in v["allowed_tools"]:
        pol = skillfmt.allowed_tools_for(c["raw"], variant=c["variant"],
                                         scripts=[Attachment(**s) for s in c["scripts"]])
        assert [list(pol.tokens), pol.value, list(pol.notes)] == [c["tokens"], c["value"],
                                                                  c["notes"]]
    for c in v["render"]:
        b = skillfmt.render_bundle(RenderFields.from_dict(c["fields"]),
                                   [Attachment(**a) for a in c["attachments"]],
                                   install_name=c["install_name"], variant=c["variant"])
        assert b.to_dict() == c["bundle"], c["id"]
        assert skillfmt.trigger_fields(RenderFields.from_dict(c["fields"])) == c["trigger_fields"]
    for c in v["render_errors"]:
        with pytest.raises(RenderError):
            skillfmt.render_bundle(RenderFields.from_dict(c["fields"]),
                                   [Attachment(**a) for a in c["attachments"]],
                                   install_name=c["install_name"], variant=c["variant"])
    for c in v["validate"]:
        got = skillfmt.validate_skill_md(c["text"], folder_name=c["folder_name"],
                                         managed=c["managed"], expect=c["expect"])
        assert sorted([p.rule, p.scope] for p in got) == c["problems"], c["id"]
    for c in v["attachment_paths"]:
        got = skillfmt.validate_attachment_path(c["path"])
        assert sorted([p.rule, p.scope] for p in got) == c["problems"]
    for c in v["lock_hash"]:
        assert skillfmt.lock_hash(c["root"], c["skills"]) == c["lock_hash"]
    for c in v["taxonomy"]:
        assert skillfmt.taxonomy(c["frontmatter"]) == c["taxonomy"]
    for c in v["body_sha256"]:
        assert skillfmt.body_sha256(c["body"]) == c["body_sha256"]
    assert {r: [s, d] for r, (s, d) in skillfmt.RULES.items()} == v["rules"]


def test_vectors_cover_the_reference_skills_and_the_ledger():
    v = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    ids = {c["id"] for c in v["render"]}
    assert {"reference-acme-refund-policy", "reference-acme-returns-desk",
            "reference-long-imported-name"} <= ids
    by_id = {c["id"]: c for c in v["render"]}
    for i, rid in enumerate(["reference-acme-refund-policy", "reference-acme-returns-desk",
                             "reference-long-imported-name"]):
        assert by_id[rid]["bundle"]["files"][0]["content"] == oracle_case(i)["text"]
    defects = {c["id"] for c in v["validate"]}
    assert {c[0] for c in LEDGER} <= defects
    assert {c["name"] for c in v["install_names"]} >= {n for n, _ in NAME_CASES}


def test_vectors_cover_the_review_fixes():
    v = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    # A foreign wildcard read-only grant survives as the bare tool.
    assert any(c["raw"] == PROD_WILDCARD_SHAPE and c["variant"] == "foreign"
               and c["value"] == "Read" for c in v["allowed_tools"])
    assert any(c["variant"] == "foreign" and c["raw"] == ["Read(./references/**)"]
               and c["value"] is None for c in v["allowed_tools"])
    # A CR body renders with LF line endings, and its hash is over the LF body.
    crlf = {c["id"]: c for c in v["render"]}["crlf-body"]
    assert "\r" in crlf["fields"]["body"]
    assert "\r" not in crlf["bundle"]["files"][0]["content"]
    assert any("\r" in c["body"] for c in v["body_sha256"])
    # An unsafe path named SKILL.md is refused, a safe one is listed.
    assert {"unsafe-path-named-skill-md", "absolute-path-named-skill-md"} <= {
        c["id"] for c in v["render_errors"]}
    assert any(c["path"] == "references/SKILL.md/notes.md" and ["F15", "loader"] in c["problems"]
               for c in v["attachment_paths"])
    # A hand-made folder that differs only in case takes the name.
    assert any(c["taken"] == {"Acme-Refund-Policy": None}
               and c["install_name"].startswith("acme-refund-policy-")
               for c in v["choose_install_name"])


def _invisible(ch: str) -> bool:
    import unicodedata

    cat = unicodedata.category(ch)
    return (ch not in "\n\t" and cat in ("Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn")) or (
        cat == "Zs" and ch != " ")


@pytest.mark.parametrize("path", [Path(skillfmt.__file__), Path(__file__)])
def test_sources_spell_invisible_characters_as_escapes(path):
    # An editor that strips U+2028, U+FEFF or a noncharacter would silently change what the
    # vectors cover, and regenerating them would hide it. Inputs use \\uXXXX escapes.
    found = [(n, hex(ord(ch))) for n, line in enumerate(
        path.read_text(encoding="utf-8").split("\n"), 1) for ch in line if _invisible(ch)]
    assert found == []
