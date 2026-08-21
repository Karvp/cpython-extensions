# Architecture

Current documented release: **1.1.0**.

Version 1.1.0 preserves the production transformation contracts of 1.0.4 and strengthens the structural regression and scaling evidence around the optimized paths. The architecture below describes the supported runtime design; benchmark harnesses observe that design but do not define it.

## Overview

`cpython-extensions` is a pure-Python package that rewrites CPython 3.13 code objects. The distribution is `cpython-extensions`; the primary import package is `python_extensions`. Compatibility modules `pyswitch`, `inline_function`, and `pygoto` remain available for older imports.

The central rule is **fail closed**: a transformation that cannot prove its structural assumptions must preserve the ordinary Python path or reject decoration with a specific error. It must not emit speculative bytecode and hope verification succeeds later.

## Source layout

```text
src/python_extensions/
  switch.py       switch syntax recognition, route planning, lowering
  inline.py       registration, call-site analysis, inlining and optimization
  goto.py         label/goto recognition, strict control-flow validation, patching
  compose.py      canonical multi-extension composition
  _core/
    cfg.py         control-flow graph utilities
    dataflow.py    shared data-flow analysis
    model.py       transformation data model
    report.py      transformation reporting
    verify.py      generated-code verification
```

`src/python_extensions/_version.py` is the sole package-version source.

## Transformation pipeline

A decorated function generally moves through six stages:

1. **Recognize** supported source/bytecode marker shapes.
2. **Plan** semantics before mutating code.
3. **Select** a backend or strategy from explicit options plus proof-based safety/profitability checks.
4. **Lower** to CPython 3.13 bytecode while preserving stack, exception-table, and code-object invariants.
5. **Verify** the generated code object; reject or fall back when verification fails.
6. **Report** the selected backend, transformation decisions, and verifier state for tests and `explain_extensions()`.

Composition uses the canonical order `switch -> inline -> goto`, followed by final verification.

## Switch architecture and invariants

Production `mode="auto"` uses the portable compiler. It evaluates the switch subject once and preserves dictionary-style hash/equality semantics in `case_key_mode="python"`.

When a route shape is provably suitable, the portable compiler can specialize dispatch into a table-backed backend. The **route table grows with case count; the hot executable dispatch path does not become an N-way comparison chain**. Release 1.1.0 makes this property an explicit structural regression contract through 1,024 direct-value routes, with bounded-shape checks for expression-template and statement-template lowering as well.

Typed mode includes the runtime type in route identity. Optimized typed partitions/routers are selected only when their metaclass/hash/equality assumptions are proven.

Guarded/fallthrough-heavy shapes may require a different backend with different scaling characteristics. The optimizer must never select a faster specialization by weakening case semantics.

Live/self-modifying backends remain explicit opt-ins and are not selected by ordinary `auto` mode.

## Inline architecture and invariants

Inlining is registration-based: only eligible exact call targets are cloned. Registries are transactional and use weak identity tracking so failed decoration or garbage collection cannot leave stale eligibility behind.

Two binding policies are exposed:

- `frozen` — clone against the target state observed during transformation; maximizes optimization opportunities and intentionally uses snapshot semantics.
- `guarded` — validate supported loaded target/state at runtime and deopt to the exact ordinary callable when the cloned state is stale.

`policy="speed"` accounts for transformation/guard costs and may intentionally leave a call ordinary. Shared regions reduce code duplication when multiple eligible call sites can safely branch to one appended inlined body.

## Goto architecture and invariants

Strict goto validates synthetic jump targets against the final CFG, preserves required code/offset relationships, and checks CPython exception-handler stack semantics before accepting a transformation.

`mode="unsafe"` relaxes safety checks for controlled low-level experiments and is not the production recommendation.

The source-level `goto .name` / `label .name` concept is inspired in part by [Entian's “goto for Python”](https://entrian.com/goto/). `cpython-extensions` does not reuse that implementation; it performs its own CPython 3.13 lowering and verification.

## Verification

`python_extensions.verify_code()` checks generated code structure, stack behavior, and CFG properties. Each subsystem layers feature-specific proofs on top of the shared verifier.

A verifier failure is a transformation failure, not a warning to suppress.

## Concurrency and lifecycle

Runtime transformed paths avoid global registry locks. Registration, decoration, and unregister operations serialize mutation of shared inline registry state. Live switch modes have backend-specific concurrency contracts documented in the comprehensive guide.

## Compatibility boundary

The package deliberately depends on CPython 3.13 opcode, call, code-object, and exception-table behavior. Widening `requires-python` is therefore a compatibility port requiring full parser/lowering/verifier/stress recertification, not a metadata-only edit. See [`COMPATIBILITY.md`](COMPATIBILITY.md).
