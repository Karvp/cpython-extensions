# Security Policy

## Supported versions

Version **1.1.0** is the current supported release. Security fixes for this milestone are maintained on the 1.1.x line; earlier public releases remain historical artifacts and are superseded for support.

| Version | Security support |
|---|---|
| 1.1.x | Yes — current supported line |
| 1.0.x | No — superseded by 1.1.0 |
| Pre-release/internal checkpoints | No |

The supported runtime boundary is CPython `>=3.13,<3.14` with `bytecode>=0.17,<0.18`. Expanding either boundary requires a new semantic, verifier, and stress-certification pass; see [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md).

## Reporting a vulnerability

Use GitHub's private vulnerability reporting / Security Advisory flow for [`Karvp/cpython-extensions`](https://github.com/Karvp/cpython-extensions/security) when available. Do not publish exploit details in a public issue before maintainers have had a reasonable opportunity to triage the report.

A useful report includes:

- the exact `cpython-extensions` version and CPython patch version;
- operating system and architecture;
- a minimal **source-file** reproducer when source-recognized syntax is involved;
- the affected subsystem (`switch`, `inline`, `goto`, composition, verifier, packaging, or release tooling);
- selected modes/backend options;
- `explain_extensions()` and `verify_code()` output when applicable;
- whether the issue can crash CPython, corrupt state, cross a documented control-flow boundary, violate a binding/guard contract, or bypass a verifier/safety check.

Please separate the observable impact from the suspected implementation cause. That distinction is especially useful for bytecode, exception-table, stack-depth, and adaptive-runtime failures.

## Security-relevant scope

Examples of issues that should be reported privately include:

- malformed transformed bytecode or invalid stack-depth accounting;
- verifier acceptance of code that violates documented control-flow or exception-region invariants;
- strict-goto jumps that cross protected regions incorrectly;
- transformation races that violate documented thread/re-entry semantics;
- memory-safety symptoms associated with explicitly opt-in live switch backends;
- guarded-inline deoptimization failures that execute stale or mismatched callable state;
- release-workflow or artifact-integrity failures that could publish bytes different from the certified artifacts.

The explicit live switch modes and `enable_goto(mode="unsafe")` are low-level opt-ins with documented hazards. Those documented hazards are not vulnerabilities by themselves; behavior outside the documented boundary is.

## Release and supply-chain controls

The release workflow is designed around reproducible wheel/sdist builds, checksum verification, metadata preflight, artifact revalidation after handoff, GitHub Release asset integrity checks, and PyPI Trusted Publishing via OIDC. Stable publication is restricted to the exact `v<package-version>` tag and the protected `pypi` environment.

No PyPI token is required in repository secrets. Never commit GitHub tokens, private keys, package-index credentials, signing keys, or other secrets.

## Coordinated disclosure

If a report is confirmed, maintainers should keep technical exploit details private until a fix and release plan are ready, then publish an advisory with the affected versions, fixed version, impact, and upgrade guidance. Security fixes should receive focused regression coverage in addition to the normal release gates.
