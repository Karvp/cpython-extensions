# Architecture

Current documented release: **1.2.0**.

Version 1.2.0 keeps the existing fail-closed switch/inline/goto transformation model, adds a shared specialization/partial-evaluation subsystem, and renovates the explicit CPython 3.13 live-switch backend with an optional fused native dispatcher. Benchmark harnesses observe the architecture; they do not define semantics.

## Overview

`cpython-extensions` rewrites CPython 3.13 functions/code objects and, for one explicitly selected switch backend, can mutate a verified jump gate in the live adaptive bytecode buffer. Most of the package is Python; `python_extensions._livegate` is an optional CPython C accelerator used only by explicit live-switch modes.

The distribution is `cpython-extensions`; the primary import package is `python_extensions`. Compatibility modules `pyswitch`, `inline_function`, `pygoto`, and `pyspecialize` are provided for direct feature imports.

The central rule is **fail closed**: a transformation that cannot prove its structural assumptions must preserve the ordinary/generic path or reject decoration with a specific error. It must not emit speculative bytecode and hope verification succeeds later.

## Source layout

```text
src/python_extensions/
  switch.py        switch syntax recognition, portable/live planning and lowering
  _livegate.c      optional fused CPython 3.13 live-route lookup + gate write
  _specialize.py   partial evaluation, guarded specialization, adaptive hotpaths
  inline.py        registration, call-site analysis, inlining and optimization
  goto.py          label/goto recognition, strict CFG/exception validation
  compose.py       canonical multi-extension composition
  _version.py      sole package-version source
  _core/
    cfg.py          control-flow graph utilities
    dataflow.py     shared data-flow analysis
    model.py        transformation data model
    report.py       transformation reporting
    verify.py       generated-code verification
```

## Transformation pipeline

A transformed function generally moves through these stages:

1. **Recognize** supported source/bytecode marker shapes.
2. **Plan** semantics before mutating code.
3. **Select** a backend or strategy from explicit options plus proof-based safety/profitability checks.
4. **Lower** to CPython 3.13 bytecode while preserving stack, exception-table, source-location, and code-object invariants.
5. **Verify** the generated code object; reject or fall back when verification fails.
6. **Report** the selected backend, decisions, and verifier state.

Composition uses the fixed order:

```text
switch -> partial -> inline -> goto -> specialize/hotpath
```

The order is part of the public architecture. Switch may recompile source and therefore runs first. Partial evaluation should expose constants before inlining. Goto resolves pseudo labels after static code growth. Guarded specialization/hotpath wraps or modifies the final verified function. `specialize` and `hotpath` are alternative final layers.

## Switch architecture and invariants

### Portable compiler

Production `mode="auto"` defaults to the portable compiler. It evaluates the switch subject once and preserves dictionary-style hash/equality semantics in `case_key_mode="python"`.

When a route shape is provably suitable, the portable compiler can select bounded-shape table-backed lowering such as direct-value, expression-template, or statement-template plans. Route data may grow with case count while the executable hot dispatch path remains compact. Fully general heterogeneous bodies use dictionary routing followed by an inline balanced route-selection tree.

Typed mode includes exact runtime type in route identity. Optimized per-type partitions are selected only when metaclass/hash/equality assumptions are proven; custom-metaclass plans retain the conservative representation.

Portable plan strength is important to performance policy. A compact direct/expression/statement template may beat live dispatch even at large route counts and should not be discarded simply because a switch is large.

### Live compiler

Live/self-modifying backends remain explicit opt-ins. They keep case bodies inline and select a destination by writing a verified `JUMP_FORWARD` gate in the live CPython 3.13 adaptive bytecode buffer.

The compiler handles `EXTENDED_ARG` prefixes when locating marker operands and binds the live gate at the first prefix code unit. This is required for large functions whose constants or local indexes exceed 255.

Live modes have different isolation contracts (`isolated`, `per_call`, `thread_local`, `fast`). See [`LIVE_SWITCH.md`](LIVE_SWITCH.md).

### Native live dispatcher

`_livegate.c` fuses routing and gate mutation into one bound C call. It keeps a conservative dictionary path and can add proven fast structures for dense/sparse exact integers and safe exact-builtin typed partitions. No fast structure may bypass observable Python hash/equality/metaclass behavior.

The native module is optional at installation time. `live_engine="auto"` falls back to the ctypes engine if it is unavailable or fails runtime self-test; `live_engine="native"` fails explicitly instead.

## Specialization architecture and invariants

`_specialize.py` contains one conservative bytecode partial evaluator used by three APIs:

- `partial()` freezes selected parameters and removes them from the effective signature;
- `specialize()` creates guarded exact-type/constant variants plus generic fallback;
- `hotpath()` profiles a bounded set of runtime shapes for a bounded budget and promotes profitable variants.

The evaluator folds only operations whose semantics are proven. Shadowed builtins, custom metaclass behavior, arbitrary user equality, and unsupported object constants prevent unsafe folding rather than being assumed ordinary.

Adaptive float/complex constant guards use canonical bit-oriented identities where necessary so `+0.0`, `-0.0`, NaN, and complex components do not rely on inappropriate Python equality shortcuts.

On eligible one-variant ordinary functions, hotpath can use CPython 3.13 `sys.monitoring` for bounded warm-up and later install an in-frame dispatcher. Wrapper dispatch remains the fallback for polymorphism, coroutines, metrics, tool-slot conflicts, or unsupported guard shapes.

See [`SPECIALIZATION.md`](SPECIALIZATION.md).

## Inline architecture and invariants

Inlining is registration-based: only eligible exact call targets are cloned. Registries are transactional and use weak identity tracking so failed decoration or garbage collection cannot leave stale eligibility behind.

Two binding policies are exposed:

- `frozen` — clone against the target state observed during transformation; maximizes optimization opportunities and intentionally uses snapshot semantics.
- `guarded` — validate supported target/code/default/descriptor state at runtime and deopt to the exact ordinary callable when cloned assumptions are stale.

`policy="speed"` accounts for transformation/guard costs and may intentionally leave a call ordinary. Shared regions reduce code duplication when multiple eligible call sites can safely branch to one appended inlined body.

## Goto architecture and invariants

Strict goto validates synthetic jump targets against the final CFG, preserves required code/offset relationships, and checks CPython exception-handler stack semantics before accepting a transformation.

`mode="unsafe"` relaxes safety checks for controlled low-level experiments and is not the production recommendation.

The source-level `goto .name` / `label .name` concept is inspired in part by [Entrian's “goto for Python”](https://entrian.com/goto/). `cpython-extensions` does not reuse that implementation; it performs its own CPython 3.13 lowering and verification.

## Verification

`python_extensions.verify_code()` checks generated code structure, stack behavior, and CFG properties. Each subsystem layers feature-specific proofs on top of the shared verifier.

A verifier failure is a transformation failure, not a warning to suppress.

Live mode adds runtime layout/write self-tests because static bytecode verification cannot prove a raw memory-layout assumption.

## Concurrency and lifecycle

Portable transformed paths avoid global hot-path locks. Registration, decoration, and unregister operations serialize mutation of shared inline/specialization registry state where required.

Live switch concurrency is backend-mode-specific. Shared `fast` mutates one code object and is only valid under a single-active-call contract. Safer modes use thread/depth/per-call clones. Native dispatcher clone construction copies immutable acceleration structures without rehashing user objects.

Hotpath profiling state is bounded by both shape count and total profile budget. Profiling terminates; megamorphic traffic cannot grow state without bound.

## Compatibility boundary

The package deliberately depends on CPython 3.13 opcode, call, code-object, exception-table, and (for live mode) adaptive-buffer behavior. Widening `requires-python` is a compatibility port requiring full parser/lowering/verifier/native/stress recertification, not a metadata-only edit. See [`COMPATIBILITY.md`](COMPATIBILITY.md).
