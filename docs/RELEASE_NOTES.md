# Release Notes

This document covers public releases of `cpython-extensions`. Internal benchmark/checkpoint labels are retained only where they identify historical evidence; they are not package versions.

## 1.2.0 — Specialization and native live-dispatch release

`1.2.0` adds a new specialization family and promotes the live-switch research backend into a substantially better documented and qualified explicit optimization path. The default switch contract remains portable and fail-closed.

### New specialization APIs

The package now exports `partial`, `specialize`, and `hotpath`. `partial()` freezes selected parameters into a real transformed Python function. `specialize()` creates guarded constant/exact-type fast variants with generic fallback. `hotpath()` performs bounded runtime shape discovery and promotes only variants accepted by the selected profitability policy.

Adaptive profiling has finite shape and call budgets so megamorphic call sites cannot accumulate unbounded state or pay profiling overhead forever. Eligible one-variant ordinary functions can use CPython 3.13 `sys.monitoring` during warm-up and install an in-frame dispatcher after promotion.

### Renovated live switch

Explicit live modes now accept `live_engine="auto" | "native" | "ctypes"`. The optional native engine fuses lookup and gate mutation in C and includes guarded dense/sparse integer and exact-builtin typed fast paths. The ctypes engine remains available as fallback and comparison baseline.

Large/multi-site live functions now correctly locate gates through `EXTENDED_ARG` prefixes. Runtime self-tests still validate code-object layout and reversible writes before live mutation is accepted. Live mode remains disabled on free-threaded CPython.

### Performance interpretation

The release intentionally does not publish “live is faster” as a universal rule. The new cross-task evidence demonstrates where each architecture wins.

Dense VM/parser loops are the strongest live candidates: the committed CPython 3.13.5 evidence reaches roughly **2.47× portable-to-native speedup at 2,048 random VM routes**, and a separate 10-million-dispatch 1,024-route VM sample records about **1.95×**.

HTTP/RPC routing is different. Portable statement-template/direct lowering can be extremely compact; 64-route HTTP routing is essentially tied in the long loop, and the one-router-call-per-request control favors portable once safe live isolation overhead is included. Sparse protocols and heavy server bodies can also favor portable.

See [`LIVE_SWITCH.md`](LIVE_SWITCH.md) and [`../benchmarks/results/BENCHMARK_LIVE_EXTENSIVE_V122.md`](../benchmarks/results/BENCHMARK_LIVE_EXTENSIVE_V122.md). The `v122` suffix is an evidence/checkpoint identifier, not the package version.

### Qualification

The 1.2.0 source baseline records **450/450 tests**, the existing **1,239,100-call** full live compatibility harness under CPython dev mode, and a 30-configuration broad workload matrix totaling **202,798,080 timed dispatches** before long-run supplements.

See [`PYTHON_EXTENSIONS_1.2.0_CERTIFICATION.txt`](../PYTHON_EXTENSIONS_1.2.0_CERTIFICATION.txt) and [`RELEASE_AUDIT_1.2.0.txt`](../RELEASE_AUDIT_1.2.0.txt).

## 1.1.0 — Performance evidence and verification milestone

`1.1.0` promotes the project to a new minor release line after a broad switch-scaling, structural-regression, documentation, and release-quality pass. The supported transformation contracts remain compatible with 1.0.4; the milestone is about proving and preserving the optimized behavior more rigorously rather than introducing benchmark-specific semantics.

### What changed

- Added generated switch-scaling coverage from **2 through 1,024** integer and string routes against `if/elif`, `match`, and a bound `dict.get` baseline.
- Added structural tests that require direct-value, expression-template, and statement-template switch specializations to retain bounded executable bytecode as route count grows.
- Added dedicated inline and goto intended-workload benchmarks with semantic checks before timing.
- Added configurable benchmark intensity (`--target-dispatches`, `--repeat`, `--warmup-batches`) so release evidence can be reproduced without editing the driver.
- Audited and refined all release-sensitive documentation, including security support, compatibility boundaries, contributor requirements, GitHub/PyPI setup, benchmark interpretation, architecture, release procedure, and repository metadata.
- Strengthened repository hygiene so current documentation and benchmark evidence must agree with the package version.

### Recorded performance evidence

On the committed CPython 3.13.5 / Linux x86-64 run, the portable direct-value switch remains close to a bound dictionary lookup while linear `if/elif` and `match` routers grow with route count. Integer routing records approximately **9.94×** speedup over `if/elif` at 64 routes, **38.96×** at 256, **71.33×** at 512, and **142.90×** at 1,024. The 1,024-route string case records **83.86×** over `if/elif` on the same run.

The same release evidence records **1.34×** median speedup for a small frozen inlined helper and **2.47×** for strict goto versus an explicit three-state dispatcher. The naturally structured reference remains slightly faster than goto, which is documented explicitly: goto is intended for genuinely state-machine-like or generated control flow, not as a blanket replacement for structured loops.

### Compatibility and support

The release continues to target CPython `>=3.13,<3.14`, retains `bytecode>=0.17,<0.18`, ships `py.typed`, and uses MPL-2.0. Free-threaded CPython and other Python minor versions remain outside the certified boundary.

The source-level `goto .name` / `label .name` notation is inspired in part by [Entrian's “goto for Python”](https://entrian.com/goto/). `cpython-extensions` uses an independent CPython 3.13 lowering and verification implementation.

See [`PYTHON_EXTENSIONS_1.1.0_CERTIFICATION.txt`](../PYTHON_EXTENSIONS_1.1.0_CERTIFICATION.txt) and [`RELEASE_AUDIT_1.1.0.txt`](../RELEASE_AUDIT_1.1.0.txt) for the qualification summary.

## 1.0.4 — Benchmark and documentation release

`1.0.4` adds a reproducible baseline benchmark comparing idiomatic CPython code with the corresponding `cpython-extensions` switch, inline, and strict-goto paths. The benchmark script and raw JSON output are committed under `benchmarks/`, and the README reports all measured scenarios, including the goto case where the extension is approximately performance-neutral/slightly slower in the recorded run.

The runtime transformation implementation is unchanged from 1.0.3. This release also documents [Entian's `goto`](https://entrian.com/goto/) as an inspiration for the goto extension's source-level label/jump concept; `cpython-extensions` uses its own CPython 3.13 transformation, strict control-flow validation, exception-region checks, and verifier.

See [`PYTHON_EXTENSIONS_1.0.4_CERTIFICATION.txt`](../PYTHON_EXTENSIONS_1.0.4_CERTIFICATION.txt) and [`RELEASE_AUDIT_1.0.4.txt`](../RELEASE_AUDIT_1.0.4.txt) for the 1.0.4 qualification summary.

## 1.0.3 — Initial public release

`1.0.3` is the first published release of `cpython-extensions` for CPython 3.13.

The package exposes three opt-in transformation families:

- **switch** for hash-table-style multi-way dispatch with portable production lowering, typed-key support, guarded cases, fallthrough, and reporting;
- **inline** for registered-function bytecode inlining with profitability checks, frozen or guarded binding, shared regions, and CFG/data-flow optimization;
- **goto** for validated local jumps using `goto .name` / `label .name` pseudo-statements.

Production defaults are conservative: `mode="auto"` for switch, `policy="speed", binding="frozen"` for inline, and `mode="strict"` for goto. Guarded inline binding is available when targets may be rebound or reconfigured after decoration. Low-level live switch modes and unsafe goto are explicit opt-ins.

The release targets CPython `>=3.13,<3.14`, depends on `bytecode>=0.17,<0.18`, ships `py.typed`, and is licensed under MPL-2.0.

Release qualification includes the 370-test regression suite, CPython dev/allocator checks, branch coverage, generated-bytecode verification, multi-million-operation stress harnesses, reproducible wheel/sdist builds, and installed-artifact smoke tests.

See [`PYTHON_EXTENSIONS_1.0.3_CERTIFICATION.txt`](../PYTHON_EXTENSIONS_1.0.3_CERTIFICATION.txt) and [`RELEASE_AUDIT_1.0.3.txt`](../RELEASE_AUDIT_1.0.3.txt) for the release qualification summary.
