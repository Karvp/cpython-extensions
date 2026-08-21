# Changelog

This changelog records supported public releases. Internal benchmark/checkpoint labels remain in benchmark filenames when they are needed to preserve historical evidence, but they are not release versions.

## 1.2.0 — 2026-08-22

Feature and architecture release for CPython 3.13. It adds guarded specialization/partial evaluation and renovates the explicit live switch backend with an optional fused native C dispatcher. Portable switch remains the production default.

### Specialization and partial evaluation

- Added `partial()` for transformed-function partial evaluation with frozen parameters removed from the effective signature and safe constant/dead-branch simplification.
- Added `specialize()` for guarded constant and exact-type variants with guaranteed generic fallback, bounded variant counts, and inline/wrapper dispatch selection.
- Added `hotpath()` for bounded adaptive argument-shape discovery with finite profiling budgets, shape eviction, profitability policy, optional metrics, and a CPython 3.13 `sys.monitoring` warm-up backend where eligible.
- Added canonical float/complex constant guards that preserve signed-zero/NaN-sensitive identity behavior without executing arbitrary user equality.
- Added `pyspecialize` compatibility import and extended `optimize_extensions()` to the canonical `switch -> partial -> inline -> goto -> specialize/hotpath` order.

### Native live switch renovation

- Added optional `python_extensions._livegate` C accelerator and `live_engine="auto" | "native" | "ctypes"`.
- Fused route lookup and live jump-gate mutation into one bound C call for the native engine.
- Added semantics-guarded dense exact-int routing, sparse exact-int open-addressing, CPython compact-int extraction, and safe exact-builtin typed partitions.
- Fixed live-gate discovery/binding when generated operands require `EXTENDED_ARG`, including >255 constant and >255 local indexes.
- Fixed native dispatcher clone-lifetime/auxiliary-table handling uncovered by the expanded full test suite.
- Prevented automatic native-extension import on free-threaded CPython 3.13 and rejected live mode there rather than silently changing interpreter-wide GIL behavior.
- Retained the historical ctypes engine as a self-tested fallback and diagnostic baseline.

### Cross-task performance qualification

- Added an identical-source real-backend live benchmark that validates portable/live plan selection before timing and measures repeated dispatch inside one outer function invocation.
- Added a 30-configuration cross-task matrix covering dense VMs, integer parsers, state machines, mixed event routing, sparse protocols, HTTP/RPC, heavy server handlers, direct/minimal controls, threaded safe modes, async isolation, and one-request-per-call server routing.
- The broad matrix records **202,798,080 timed dispatches** excluding warmups/correctness checks, plus separate 5–10 million-dispatch sustained-loop samples.
- On the certified CPython 3.13.5 host, 2,048-route random dense-VM routing measured roughly **393.3 ns portable vs 159.2 ns native live (2.47×)**; a 1,024-route 10-million-dispatch VM loop measured about **329.4 ns vs 169.0 ns (1.95×)**.
- The same evidence keeps negative controls visible: ordinary HTTP string routing is approximately tied, sparse template-friendly protocol routing can favor portable, heavy handler bodies dilute dispatch savings, and direct/minimal portable routes remain faster.
- Native live beat the historical ctypes live engine across the committed broad matrix, while workload-shape guidance now explicitly recommends portable for ordinary per-request HTTP/RPC routing.

### Packaging, documentation, and validation

- Added setuptools build integration for the optional `_livegate.c` extension and source-distribution inclusion of C/CSV benchmark evidence.
- Added dedicated live-switch and specialization architecture guides; updated README, architecture, compatibility, benchmark, release, repository metadata, and smoke-test guidance for 1.2.0.
- Qualification baseline records **450/450 tests** and **1,239,100 calls** through the full live-switch compatibility harness under CPython dev mode.
- Internal `v121`/`v122` benchmark filenames remain engineering evidence identifiers, not package versions.

## 1.1.0 — 2026-08-21

Performance-evidence, verification, and documentation milestone for the CPython 3.13 extension suite. The runtime transformation engine remains semantically compatible with 1.0.4; this release strengthens the evidence and structural guards around the optimized paths and promotes the project to the 1.1 release line.

### Switch scaling and structural guarantees

- Added a reproducible route-scaling benchmark for **2 through 1,024** integer and string cases against generated `if/elif`, `match`, and hand-written bound `dict.get` baselines.
- Recorded the portable direct-value backend at roughly **43–50 ns per integer dispatch from 8 through 1,024 cases** on the certified CPython 3.13.5 run, while linear native chains grow with route count.
- Recorded median integer speedups of about **10× at 64 routes**, **37.73× at 256**, **68.96× at 512**, and **138.49× at 1,024** versus `if/elif`; string routing reaches **83.84×** at 1,024 routes on the same run.
- Added structural regression tests proving that the specialized direct-value executable bytecode remains bounded as the route table grows, with equivalent bounded-shape checks for expression-template and statement-template specialization.
- Kept small-switch crossover results visible: two-route native branching remains faster, and four-route routing is near the crossover. The benchmark is evidence for scaling, not a claim that every switch is faster at every size.

### Inline and goto evidence

- Added an intended-workload benchmark for frozen helper inlining, recording a **1.34× median speedup** over the ordinary helper call on the certified run.
- Added an explicit three-state-machine benchmark for strict goto, recording a **2.47× median speedup** over the explicit state-dispatch formulation while remaining within roughly **2.3%** of the naturally structured reference loop.
- Retained semantic differential checks before timing so benchmark results cannot mask an incorrect transformation.

### Documentation and release engineering

- Reworked the README benchmark section around reproducible evidence, crossover points, code-size behavior, and interpretation guidance.
- Audited and refined the security policy, compatibility boundary, architecture, contributor guide, repository setup, release procedure, benchmark guide, release notes, citation metadata, and repository metadata for the 1.1.0 line.
- Strengthened repository hygiene so release-sensitive documentation and current benchmark evidence must agree with the package version.
- Preserved the hardened GitHub Release / PyPI Trusted Publishing flow, metadata preflight, and stable-vs-preview tag separation.
- Retains CPython `>=3.13,<3.14`, MPL-2.0, and the certified `bytecode>=0.17,<0.18` runtime dependency boundary.

## 1.0.4 — 2026-08-21

Benchmark and documentation release. Runtime transformation semantics are unchanged from 1.0.3.

### Benchmarking and documentation

- Added a reproducible plain-CPython-vs-extension benchmark covering switch dispatch, function inlining, and strict goto.
- Published the README benchmark table from the committed JSON evidence under `benchmarks/results/`.
- Added benchmark methodology and interpretation notes so speedups and slowdowns are presented rather than selectively reported.
- Added attribution to [Entian's `goto`](https://entrian.com/goto/) as an inspiration for the goto extension's source-level label/jump idea.
- Updated public release metadata and documentation to 1.0.4.

### Packaging

- Retains CPython `>=3.13,<3.14`, MPL-2.0, and the certified `bytecode>=0.17,<0.18` runtime dependency boundary.
- Retains the hardened GitHub Release and PyPI Trusted Publishing workflow.

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
