# cpython-extensions

**Production-oriented CPython 3.13 language-extension utilities for fast switch dispatch, bytecode-level function inlining, and validated local goto.**

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Implementation](https://img.shields.io/badge/implementation-CPython-306998)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MPL--2.0-brightgreen)](LICENSE)
[![Typing](https://img.shields.io/badge/typing-py.typed-informational)](src/python_extensions/py.typed)

The distribution is named **`cpython-extensions`**; the import package is **`python_extensions`**. The project deliberately targets CPython internals and currently supports **CPython 3.13.x** (`>=3.13,<3.14`).

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

## At a glance

| Capability | What it provides | Production-oriented default |
|---|---|---|
| **Switch** | Hash-table-style multi-way dispatch, typed key identity, guarded cases, and fallthrough | `mode="auto"` |
| **Inline** | Bytecode-level call inlining, profitability checks, shared regions, data-flow optimization | `policy="speed", binding="frozen"` |
| **Goto** | Explicit local jumps with exception-region and CFG validation | `mode="strict"` |
| **Verification** | Post-transform bytecode/control-flow verification and reports | Keep enabled |

The package favors **general Python semantics, explicit opt-ins, and fail-closed transformation** over benchmark-specific shortcuts.

## Why cpython-extensions?

Python intentionally keeps its language small and structured, but some performance-sensitive or generated-code workloads benefit from lower-level control. `cpython-extensions` explores three bounded extensions while keeping the transformation pipeline observable and verifiable:

- **Switch** — efficient multi-route dispatch without manually maintaining a dictionary of lambdas or a long `if/elif` ladder.
- **Inline** — clone selected function bodies into callers, with profitability analysis and guarded binding when targets may change.
- **Goto** — local control-flow jumps for state machines, generated parsers, and carefully audited low-level code.

This is **not** a replacement Python implementation and does not claim portability to PyPy or other interpreters. It is intentionally CPython-specific.

## Quick start

### Switch dispatch

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

Use exact runtime type as part of case identity when Python's ordinary equality aliases are undesirable:

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

### Function inlining

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

### Validated goto

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

Inspect rather than blindly trust transformed functions:

```python
from python_extensions import explain_extensions, verify_code

print(explain_extensions(execute))
verify_code(execute.__code__)
```

## Choosing the right modes

| Area | Recommended default | Choose another mode when... |
|---|---|---|
| Switch | `mode="auto"` | You have explicitly accepted the contract of a live/self-modifying backend |
| Case identity | `case_key_mode="python"` | Exact runtime types such as `1`, `1.0`, and `True` must remain distinct |
| Inline binding | `binding="frozen"` | Use `guarded` for hot reload, plugins, monkey-patching, replaceable methods, or mutable defaults |
| Inline policy | `policy="speed"` | Use `always` for controlled experiments or code-size/latency tradeoffs you have measured |
| Goto | `mode="strict"` | `unsafe` is reserved for carefully audited low-level experiments |
| Verification | enabled | Do not bypass verifier failures in production |

## Installation

### PyPI

```bash
python -m pip install cpython-extensions
```

### Development checkout

```bash
git clone <your-repository-url>
cd cpython-extensions
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
python -m pytest
```

POSIX shells:

```bash
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
python -m pytest
```

## Verification and development gates

```bash
python -m compileall -q src tests
python -m pytest
python -m coverage run --branch -m pytest
python -m coverage report
python tools/check_repo.py
```

Long-running stress tests are intentionally separated from ordinary pull-request feedback. See `.github/workflows/stress.yml`.

## Release quality

Version **1.0.3** is the current production baseline. The release line has been exercised on CPython 3.13.5 with package regressions, allocator/dev-mode checks, bytecode verification, multi-million-operation stress harnesses, reproducible wheel/sdist builds, and artifact-level installation tests.

See:

- [`PYTHON_EXTENSIONS_1.0.3_CERTIFICATION.txt`](PYTHON_EXTENSIONS_1.0.3_CERTIFICATION.txt)
- [`RELEASE_AUDIT_1.0.3.txt`](RELEASE_AUDIT_1.0.3.txt)
- [`docs/RELEASE_HISTORY.md`](docs/RELEASE_HISTORY.md)

The project does **not** currently claim independent certification for free-threaded CPython (`3.13t`).

## Documentation

- **[Comprehensive guide](docs/COMPREHENSIVE_GUIDE.md)** — complete API, performance choices, guarded/frozen semantics, composition, troubleshooting, and deployment guidance.
- **[Architecture](docs/ARCHITECTURE.md)** — transformation pipeline, invariants, and subsystem responsibilities.
- **[Compatibility](docs/COMPATIBILITY.md)** — interpreter/runtime support boundary and unsupported environments.
- **[Release process](docs/RELEASING.md)** — reproducible build, artifact verification, and tag/release workflow.
- **[Release history](docs/RELEASE_HISTORY.md)** — optimization and certification history.
- **[Changelog](CHANGELOG.md)** — version-by-version changes.
- **[Contributing](CONTRIBUTING.md)** — development expectations and test requirements.
- **[Security policy](SECURITY.md)** — how to report verifier, crash, or unsafe-boundary issues.

## Repository metadata

Recommended GitHub settings are tracked in [`.github/REPOSITORY_METADATA.md`](.github/REPOSITORY_METADATA.md). The canonical repository name is **`cpython-extensions`** and the recommended GitHub description is:

> Production-oriented CPython 3.13 bytecode extensions for switch dispatch, function inlining, and validated goto.

## Contributing

Contributions are welcome when they preserve general Python semantics and avoid benchmark- or fixture-specific shortcuts. Please run the normal development gates before opening a pull request and include focused regression coverage for observable behavior changes.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Low-level bytecode transformation can amplify interpreter/runtime assumptions. Crashes, verifier bypasses, incorrect exception-region handling, or unsafe boundary crossings should be treated as security-relevant until triaged.

See [SECURITY.md](SECURITY.md).

## License

This project is licensed under the **Mozilla Public License 2.0 (MPL-2.0)**. See [LICENSE](LICENSE).

MPL-2.0 is a file-level copyleft license: modifications to MPL-covered source files remain under MPL-2.0 when distributed, while the license permits those files to be combined with a larger work under different terms, subject to the license conditions.
