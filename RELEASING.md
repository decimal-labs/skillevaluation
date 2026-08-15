# Releasing skillevaluation

How to publish a new version of the `skillevaluation` package to PyPI.

> **PyPI is append-only.** A version number can never be reused, overwritten, or re-uploaded — even after you "delete" a release. If you ship a mistake, the only remedy is a *new* version. Treat the upload step as irreversible.

## How a release ships

Publishing a **GitHub Release** is the release path. `.github/workflows/publish.yml` then:

1. runs the test matrix (3.10–3.13),
2. checks that `pyproject.toml`'s version matches the tag, and
3. uploads via **[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)** — the job mints a short-lived OIDC credential for that upload, so there is no long-lived token to store, rotate, or leak.

`id-token: write` is granted to the publish job **only** — never workflow-wide. The test job installs `.[dev]`, which runs install- and import-time code from every transitive dev dependency; that code must not be able to mint a PyPI credential.

The PyPI side of Trusted Publishing is configured once, by hand, under PyPI → *Manage* → *Publishing*, with the values named at the top of `publish.yml` (project `skillevaluation`, owner `decimal-labs`, repo `skillevaluation`, workflow `publish.yml`, environment `pypi`). If the upload step fails with an authentication error, that pairing is the first thing to check.

## Versioning model

There are **two independent version numbers** — don't conflate them:

| Number | Lives in | Bump when |
|---|---|---|
| **Package version** | `pyproject.toml` `version` **and** `skillevaluation/__init__.py` `__version__` | the Python API changes |
| **Spec / format version** | the `eval.yaml` schema + the `skillevaluation://` evaluator-ref scheme | the **wire format** changes — rare, not on a normal package release |

`skillevaluation` **hardcodes** `__version__`, so the two package-version copies must be edited together. `scripts/release.sh` aborts if they disagree. The workflow's check is narrower — it compares `pyproject.toml` to the release tag and never reads `__init__.py` — so a stale `__init__.py` would ship a wheel that misreports its own version. Edit both, or run the script.

We're pre-1.0: breaking changes are allowed in `0.x` with a CHANGELOG entry (see `CONTRIBUTING.md`).

## Cutting a release

1. **Pick the next version** (SemVer).
2. **Bump it in both places** — they must match:
   - `pyproject.toml` → `version`
   - `skillevaluation/__init__.py` → `__version__`
3. **Add a CHANGELOG entry**: `## [X.Y.Z] — YYYY-MM-DD` with the changes.
4. **If you touched the README**: make sure every link is an **absolute** `https://github.com/...` URL. Relative `./` links render broken on PyPI.
5. **Publish a GitHub Release** tagged `vX.Y.Z` (the workflow strips the leading `v` and compares to `pyproject.toml`).
6. **Verify** — the version-specific endpoint updates fastest:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/skillevaluation/<version>/json
   ```

## Local fallback

`./scripts/release.sh` uploads from a maintainer's machine. It needs [`uv`](https://docs.astral.sh/uv/) and PyPI upload credentials that `twine` can find (see [twine's authentication docs](https://twine.readthedocs.io/en/stable/#configuration)); a PyPI account uploading by API token needs 2FA enabled to create one.

The script resolves the version, confirms the two copies match, refuses to proceed if that version already exists on PyPI, builds, runs `twine check`, smoke-tests the built wheel in a clean env, then **pauses for a typed `yes`** before the upload, and verifies afterward. Prefer the workflow: it runs the full matrix first and needs no stored credential.

Bare commands, if you skip the script:

```bash
rm -rf dist && uv build
uvx twine check dist/*
uvx twine upload dist/*        # PERMANENT — cannot be undone
```

## Notes & gotchas

- **Public repo, public package.** Both the GitHub repo and the PyPI package are public, so every README badge, every `https://github.com/decimal-labs/skillevaluation/...` link and every cross-link to `agentversion` is expected to resolve. A 404 is a real breakage to chase.
- **Development Status** classifier in `pyproject.toml` is `4 - Beta`, matching `agentversion` and `decimalai`. It moves to `5 - Production/Stable` when the spec cuts v1.0 (see `spec/versioning-policy.md`), not before.
- **PyPI cache lag.** The top-level `https://pypi.org/pypi/skillevaluation/json` can stay cached on the previous version for a minute or two after upload; the version-specific `.../<version>/json` endpoint reflects new releases almost immediately.
