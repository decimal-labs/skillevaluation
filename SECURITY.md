# Security Policy

This repository holds the `skillevaluation` **specification** (`spec/`, `schemas/`,
`compatibility-tests/`) and its **reference implementation** — the `skillevaluation` package on
[PyPI](https://pypi.org/project/skillevaluation/), which includes the static skill safety scanner in
`skillevaluation/safety.py`. Say which of the three you think you found a problem in; the fix is
different for each.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Two ways to reach us, either is fine:

- **GitHub private vulnerability reporting** — **Security → Report a vulnerability** on this
  repository. That opens a private advisory only maintainers can see.
- **Email** — [hello@decimal.ai](mailto:hello@decimal.ai). A PGP key is available on request if you
  would rather not send details in cleartext.

Include what you have: what you found, how to reproduce it, the version you were on, and what an
attacker could actually do with it. For a scanner finding, the input that beats it is the whole
report — send the `SKILL.md` (or the smallest fragment that still slips through).

## Scope

**The safety scanner** (`skillevaluation/safety.py`) is the part of this repository most likely to
have a security bug, because it is adversarial by construction. In scope:

- **A false negative** — skill content that carries a live credential, fetches and executes a remote
  payload, opens a reverse shell, exfiltrates secrets, or hides instructions in zero-width or
  bidirectional unicode, and that the scanner classifies as clean. This is the report we most want.
- A **bypass of the documentation-context downgrade** — content crafted to look like documentation
  (a fenced block, a placeholder token, a `category: security` frontmatter claim) in order to have a
  live payload downgraded to `info`.
- The scanner **echoing a secret it detected** instead of redacting it, in a finding, a log line, or
  a serialized result.
- A crash, hang, or unbounded resource use on adversarial input — the scanner runs in a publish gate,
  so a hang is a denial of service on publishing.

A **false positive** is a bug, not a vulnerability. Open a normal issue for it; a blocked-but-benign
skill is annoying, and we want to know, but nothing is exposed.

**The specification.** A spec flaw is one where an implementation that follows `spec/` correctly is
still unsafe — for example a `runner-contract.md` rule that lets a runner report a pass it did not
observe, an `eval-yaml.md` field whose defined semantics require a runner to execute something the
author did not intend, or an `llm-judge.md` protocol where case content can steer the judge's
verdict. Those are inherited by every conforming implementation, so they matter more than a single
package bug.

**The reference implementation.** Anything in `skillevaluation/`, `scripts/`, and the artifacts
published as `skillevaluation` on PyPI, including a published wheel that does not match this source
tree. Unsafe handling of untrusted input counts: the runner parses `eval.yaml` and skill folders that
other people wrote.

**Out of scope**

- **The runner executing shell validators from `eval.yaml` is intended behavior**, not a
  vulnerability. Running an eval suite is like running a `Makefile` from the same repository: if you
  do not trust the folder, do not run it. A path by which a case can execute something *outside* what
  its `eval.yaml` declares — during a `--dry-run`, during parsing, or during a scan — is in scope.
- The DecimalAI hosted registry and platform (`api.decimal.ai`, `app.decimal.ai`). Report those the
  same way, to the same address; they are just not this repository.
- Malicious or abusive skill content on the hosted registry. Also email us — that is an abuse report,
  which we act on, rather than a vulnerability in this code.
- Dependency CVEs, unless this package's use of the dependency is what makes them reachable.
- Scanner output with no demonstrated impact.

## What happens next

We are a small team, so rather than publish a response time we cannot hold to, here is what we
actually do:

- We acknowledge a report once we have read it, and we say plainly if triage is going to take a
  while.
- We tell you whether we consider it in scope and what we intend to do.
- We follow coordinated disclosure. We agree a timeline with you rather than impose one, and we will
  not ask you to stay quiet indefinitely.
- We are happy to credit you in the advisory and the `CHANGELOG.md` entry. A scanner fix also bumps
  `SCANNER_VERSION`, and we will name you in that entry if you want. Tell us how you would like to be
  named, or say that you would rather not be.

There is no paid bug bounty. That is a resourcing decision, not a judgment about the value of your
work.

## Safe harbour

If you make a good-faith effort to follow this policy, we will not pursue or support legal action
against you for your research. Good faith means avoiding privacy violations and service degradation,
only interacting with accounts and data you own or have permission to test, and giving us a
reasonable opportunity to fix the issue before you disclose it publicly.

If you are not sure whether what you found is a security issue, email
[hello@decimal.ai](mailto:hello@decimal.ai) and ask. That is always the right call.
