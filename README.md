# cpython-extensions

Production-oriented language-extension utilities for **CPython 3.13**.

The distribution is named `cpython-extensions`; the import package is `python_extensions`.

```bash
python -m pip install cpython-extensions
```

```python
from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    switch,
)
```

> `goto .name` and `label .name` are compile-time pseudo-statements recognized inside `@enable_goto` functions; they are not runtime objects that need to be imported.

## Why this package exists

`cpython-extensions` explores three carefully bounded extensions while preserving ordinary Python behavior wherever the selected mode promises it:

- **Switch** — hash-table-style case dispatch with portable production lowering, exact-type case identity when required, guarded cases, fallthrough, and explicit opt-in live backends.
- **Inline** — bytecode-level call inlining with profitability checks, shared regions, data-flow optimization, verification, and a choice between frozen and guarded target binding.
- **Goto** — explicit local jumps with strict control-flow validation, including exception-region safety and generated-bytecode verification.

The project is deliberately CPython-specific. The current supported line is **CPython 3.13.x** (`>=3.13,<3.14`).

## Quick start

### Switch

```python
from python_extensions import case, enable_switch, switch

@enable_switch
def classify(command: str) -> int:
    with switch(command):
        if case("read", "peek"):
            return 1
        if case("write"):
            return 2
        if case():
            return 0
```

Use exact runtime type as part of case identity when Python's normal equality aliases are undesirable:

```python
@enable_switch(case_key_mode="typed")
def exact(value):
    with switch(value):
        if case(1):
            return "int"
        if case(1.0):
            return "float"
        if case(True):
            return "bool"
        if case():
            return "other"
```

### Inline

```python
from python_extensions import inline_calls, inline_function

@inline_function(register_only=True)
def affine(x: int, scale: int = 4) -> int:
    return x * scale + 3

@inline_calls(policy="speed")
def hot_path(x: int) -> int:
    return affine(x)
```

The default `binding="frozen"` is the highest-optimization mode and assumes the target remains intentionally stable after transformation. Use guarded binding when a callee may be rebound or mutated:

```python
@inline_calls(policy="always", binding="guarded")
def plugin_sensitive(x):
    return affine(x)
```

Guarded mode validates the callable state used by the cloned body and falls back to the ordinary call if that state no longer matches.

### Goto

```python
from python_extensions import enable_goto
@enable_goto
def countdown(n: int) -> int:
    total = 0
    label .loop
    if n <= 0:
        goto .done
    total += n
    n -= 1
    goto .loop
    label .done
    return total
```

Strict mode is the production default and rejects jumps that cross unsafe control-flow or exception-region boundaries.

### Compose extensions

```python
from python_extensions import optimize_extensions

@optimize_extensions(
    switch=True,
    inline={"policy": "speed"},
    goto=True,
)
def execute(...):
    ...
```

The package exposes reporting and verification helpers so transformed code can be inspected rather than treated as a black box:

```python
from python_extensions import explain_extensions, verify_code

print(explain_extensions(execute))
verify_code(execute.__code__)
```

## Production defaults

| Area | Recommended production default | Use the advanced mode when... |
|---|---|---|
| Switch | `mode="auto"` | You explicitly accept live/self-modifying bytecode behavior |
| Case identity | `case_key_mode="python"` | Exact runtime types must be distinct |
| Inline | `policy="speed", binding="frozen"` | Use `binding="guarded"` for replaceable/mutable targets |
| Goto | `mode="strict"` | `unsafe` is only for carefully audited low-level experiments |
| Verification | Keep enabled | Do not bypass verifier failures in production |

## Documentation

- [Comprehensive guide](docs/COMPREHENSIVE_GUIDE.md) — full API, performance choices, guarded/frozen semantics, composition, troubleshooting, and deployment guidance.
- [Architecture](docs/ARCHITECTURE.md) — transformation pipeline, invariants, and subsystem responsibilities.
- [Compatibility](docs/COMPATIBILITY.md) — runtime/support boundary and unsupported environments.
- [Release process](docs/RELEASING.md) — reproducible build, artifact verification, and tag/release workflow.
- [Release history and benchmark narrative](docs/RELEASE_HISTORY.md) — historical optimization and certification record.
- [Changelog](CHANGELOG.md) — version-by-version changes.

## Development

```bash
git clone <your-fork-or-repository-url>
cd cpython-extensions
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX:   source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
python -m pytest
```

Additional local gates:

```bash
python -m compileall -q src tests
python -m coverage run --branch -m pytest
python -m coverage report
python tools/check_repo.py
```

Long-running stress tests are intentionally separated from normal PR feedback. See `.github/workflows/stress.yml`.

## Release status

Version **1.0.3** is the current production baseline. The release line has been tested on CPython 3.13.5 with package regressions, allocator/dev-mode checks, bytecode verification, multi-million-operation stress harnesses, reproducible wheel/sdist builds, and artifact-level installation tests. See `PYTHON_EXTENSIONS_1.0.3_CERTIFICATION.txt` and `RELEASE_AUDIT_1.0.3.txt` for the repository release certificate; the unchanged runtime transformation implementation inherits the deeper 1.0.2 hardening evidence.

This certification does **not** claim independent support for free-threaded CPython (`3.13t`).

## Security

Please read [SECURITY.md](SECURITY.md). Low-level bytecode transformation can amplify interpreter/runtime assumptions, so crashes, verifier bypasses, or unsafe boundary crossings should be treated as security-relevant until triaged.

## Contributing

Contributions are welcome when they preserve general Python semantics and avoid benchmark- or fixture-specific shortcuts. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
