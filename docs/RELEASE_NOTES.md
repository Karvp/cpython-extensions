# Release Notes

This document covers public releases of `cpython-extensions`. Development checkpoint labels used by historical benchmark files are not published package versions.

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
