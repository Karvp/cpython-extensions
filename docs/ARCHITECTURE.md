# Architecture

## Overview

`cpython-extensions` is a pure-Python package that rewrites CPython 3.13 code objects. The public package is `python_extensions`; compatibility modules `pyswitch`, `inline_function`, and `pygoto` remain available for older imports.

The central design rule is **fail closed**: a transformation that cannot prove its structural assumptions should preserve the ordinary Python path or reject decoration with a specific error instead of emitting speculative bytecode.

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

`src/python_extensions/_version.py` is the sole version source.

## Transformation pipeline

A typical decorated function follows these steps:

1. **Inspect source/bytecode** and recognize only supported marker shapes.
2. **Build a semantic plan** before mutating code.
3. **Select a backend/strategy** based on explicit options and proof-based profitability/safety checks.
4. **Lower to CPython 3.13 bytecode** while preserving stack and exception-table invariants.
5. **Verify the resulting code object** and reject/fall back when verification fails.
6. **Attach reports/telemetry** used by `explain_extensions()` and tests.

Composition uses the canonical order `switch -> inline -> goto`, followed by final verification.

## Switch invariants

Production `mode="auto"` uses the portable compiler. It preserves dictionary-style hash/equality behavior in `case_key_mode="python"`, evaluates the switch subject once, and keeps user case bodies in the caller frame where promised.

Exact typed mode includes the runtime type in route identity. Optimized typed partitions/routers are used only when their metaclass/hash/equality assumptions can be proven.

Live/self-modifying backends are explicit opt-ins and are not selected by ordinary `auto`.

## Inline invariants

Inlining is registration-based: only eligible exact call targets are cloned. The inliner maintains transactional registries and weak identity tracking so failed decoration or garbage collection cannot leave stale eligibility behind.

Two binding policies exist:

- `frozen` — clone against the target state observed during transformation; maximizes optimization opportunities.
- `guarded` — validate the loaded target/state at runtime and deopt to the exact ordinary callable when the cloned state is stale.

Profitability (`policy="speed"`) accounts for transformation/guard costs and may leave a call ordinary.

Shared regions reduce code duplication when multiple eligible call sites can safely branch to one appended inlined body.

## Goto invariants

Strict goto preserves code length/offset relationships where required, validates exception-handler stack semantics, patches only proven marker spans, and verifies synthetic jump targets against the final CFG.

`mode="unsafe"` relaxes safety checks and is not the production recommendation.

## Verification

`python_extensions.verify_code()` checks generated code structure, stack behavior, and CFG properties. Subsystems add feature-specific proofs on top of this shared verifier. A verifier failure is a transformation failure, not a warning to ignore.

## Concurrency and lifecycle

Runtime transformed paths avoid global registry locks. Registration/decorating/unregistering operations serialize the mutation of shared inline registry state. Live switch modes have backend-specific concurrency contracts documented in the comprehensive guide.

## Compatibility boundary

This package intentionally relies on CPython 3.13 bytecode and exception-table behavior. Do not broaden the declared Python range until the full parser/lowering/verifier/stress matrix has been audited against that interpreter line.
