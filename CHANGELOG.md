# Changelog

This changelog records **publicly published releases only**. Internal development checkpoints and benchmark lineage are intentionally omitted.

## 1.0.3 — 2026-08-21

Initial public release of `cpython-extensions`.

### Switch

- CPython 3.13 multi-way dispatch with portable production lowering.
- Python-equality and exact-type case-key modes.
- Guarded cases, fallthrough, compact-route controls, and transformation reports.
- Optional low-level live backends remain explicit opt-ins; portable behavior is the production default.

### Inline

- Registered-function bytecode inlining with profitability analysis.
- `binding="frozen"` for stable private helpers and `binding="guarded"` for replaceable or reconfigurable targets.
- Guarded deoptimization for binding, `__code__`, defaults, descriptors, partials, and supported callable-state changes.
- Shared inline regions, constant/default specialization, CFG/data-flow optimization, expansion limits, and lifecycle-safe registration.

### Goto

- Local `goto .label` / `label .label` pseudo-statements for source-file functions.
- Strict exception-region and control-flow validation by default.
- Extended-jump handling, transformation telemetry, and fail-closed verification.

### Repository and packaging

- CPython `>=3.13,<3.14`; runtime dependency `bytecode>=0.17,<0.18`.
- Mozilla Public License 2.0 (`MPL-2.0`).
- Multi-platform CI for Linux, Windows, and macOS; dev-mode, allocator-debug, branch-coverage, artifact-smoke, CodeQL, dependency-review, and stress workflows.
- Reproducible wheel/sdist tooling and tag-driven GitHub Releases.
- PyPI Trusted Publishing support is present but disabled until explicitly configured.
- Benchmark drivers and recorded evidence are organized under `benchmarks/`.
- Comprehensive usage, architecture, compatibility, contribution, security, and release documentation.

### Validation

- 370/370 package tests on the certified CPython 3.13.5 baseline.
- 81% branch-aware package coverage against an 80% repository gate.
- 19,241,648 full-scale stress operations in the release qualification pass.
- 1,167 generated code objects independently verified by the deep control-flow harness.
