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

The source-level `goto .name` / `label .name` notation is inspired in part by [Entrian's “goto for Python”](https://entrian.com/goto/). `cpython-extensions` uses an independent CPython 3.13 lowering pipeline with strict CFG/exception-region validation and post-transform bytecode verification; it does not reuse Entrian's implementation.

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
git clone https://github.com/Karvp/cpython-extensions.git
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
python -m compileall -q src tests tools benchmarks/scripts
python -m pytest
python -m coverage run --branch -m pytest
python -m coverage report
python tools/check_repo.py
```

Long-running stress tests are intentionally separated from ordinary pull-request feedback. See `.github/workflows/stress.yml`.

## Release quality

Version **1.1.0** is the current supported release line. It is a performance-evidence, verification, and documentation milestone over 1.0.4: the production transformation contracts are preserved, while the optimized paths are covered by stronger scaling benchmarks and structural regression tests.

The 1.1.0 tree has been exercised on CPython 3.13.5 with the package regression suite, CPython dev-mode checks, bytecode/control-flow verification, multi-million-operation stress and differential harnesses, reproducible wheel/sdist builds, and exact-artifact smoke tests.

Release evidence:

- [`PYTHON_EXTENSIONS_1.1.0_CERTIFICATION.txt`](PYTHON_EXTENSIONS_1.1.0_CERTIFICATION.txt)
- [`RELEASE_AUDIT_1.1.0.txt`](RELEASE_AUDIT_1.1.0.txt)
- [`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md)

The project does **not** currently claim independent certification for free-threaded CPython (`3.13t`) or for CPython 3.14+.

## Benchmarks

### Switch scaling: constant-shape dispatch as route count grows

For direct literal routes, the portable compiler can lower the switch to a bound table lookup. Route data grows with the number of cases, but the executable hot path remains bounded instead of expanding into an N-way comparison chain. The 1.1.0 scaling harness compares equivalent generated routers implemented as `if/elif`, `match`, a hand-written bound `dict.get`, and `@enable_switch(mode="portable")`.

Every successful route is validated before timing. Hits are distributed uniformly over every route in forward and reverse order; misses are checked for correctness but excluded from timing so the linear baselines represent average successful-hit depth rather than a deliberately worst-case miss. The committed 1.1.0 run used CPython 3.13.5 on Linux x86-64 and `timeit.repeat` with 7 repeats and 50 warm-up batches. It targets 100,000 successful dispatches per sample through 256 routes; for larger generated routers the harness scales work with route count while retaining at least 50,000 dispatches per sample.

| Integer cases | `if/elif` | `match` | bound `dict.get` | `switch` | vs `if/elif` | vs `match` |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 45.71 ns | 47.19 ns | 51.34 ns | 49.84 ns | 0.92× | 0.95× |
| 4 | 53.28 ns | 59.93 ns | 46.65 ns | 46.10 ns | **1.16×** | **1.30×** |
| 8 | 72.51 ns | 82.42 ns | 43.76 ns | 44.65 ns | **1.62×** | **1.85×** |
| 16 | 125.43 ns | 133.69 ns | 45.72 ns | 43.35 ns | **2.89×** | **3.08×** |
| 32 | 237.92 ns | 242.18 ns | 44.41 ns | 43.07 ns | **5.52×** | **5.62×** |
| 64 | 451.40 ns | 484.48 ns | 46.17 ns | 45.42 ns | **9.94×** | **10.67×** |
| 128 | 842.59 ns | 899.73 ns | 44.20 ns | 44.04 ns | **19.13×** | **20.43×** |
| 256 | 1,741.36 ns | 1,718.07 ns | 44.61 ns | 44.69 ns | **38.96×** | **38.44×** |
| 512 | 3,444.44 ns | 3,378.72 ns | 45.86 ns | 48.29 ns | **71.33×** | **69.97×** |
| 1,024 | 7,222.90 ns | 7,181.39 ns | 44.98 ns | 50.54 ns | **142.90×** | **142.08×** |

The scaling behavior is the important result. The switch stays near a dictionary lookup while the linear branch forms grow with route count. At 64 integer routes it is **9.94×** faster than `if/elif`; at 256 routes **38.96×**; at 512 routes **71.33×**; and at 1,024 routes **142.90×**. String routing shows the same large-route advantage: the 1,024-route string case records **83.86×** versus `if/elif` on this run.

The bounded-code regression explains why the curve does not become linear: the specialized direct-value function remains **74 bytes of executable bytecode at 1,024 routes**, while the generated `if/elif` reaches **17,420 bytes** and `match` reaches **19,466 bytes**. The table payload still consumes memory proportional to route count; the bounded claim applies to the executable dispatch path, not total storage.

The crossover is also part of the evidence. At two routes, native branching is faster. Around 4–8 routes the table-backed path reaches parity and then begins to pull away. Use `switch` for the semantics and maintainability you need, and treat the scaling advantage as strongest for larger static route sets that qualify for the portable specialization.

Reproduce the matrix:

```bash
python benchmarks/scripts/benchmark_switch_scaling_v110.py \
  --target-dispatches 100000 --repeat 7 --warmup-batches 50 \
  --json benchmarks/results/BENCHMARK_SWITCH_SCALING_V110.json
```

Raw evidence: [`benchmarks/results/BENCHMARK_SWITCH_SCALING_V110.json`](benchmarks/results/BENCHMARK_SWITCH_SCALING_V110.json).

### Inline and goto on intended workloads

The companion harness keeps the comparisons specific to the feature being measured. Frozen inlining is compared with an ordinary call to the same helper. Strict goto is compared with an explicit state-machine loop, with a naturally structured reference measured separately so the cost of label/jump lowering remains visible.

| Scenario | Plain Python | With extension | Relative result |
|---|---:|---:|---:|
| Small frozen affine helper | 58.31 ns/call | 43.52 ns/call | **1.34× faster** |
| Three-state explicit FSM (`n=32`) | 4,005.89 ns/call | 1,620.34 ns/call | **2.47× faster** |
| Naturally structured reference for the same work | 1,602.91 ns/call | 1,620.34 ns/call (`goto`) | 1.1% slower than structured reference |

The goto result should be read narrowly. `goto` is not a replacement for code that is already naturally expressed as a loop. It is useful when the source genuinely is a state machine, generated control-flow graph, or cleanup-oriented low-level routine; in that setting strict lowering can remove repeated state-variable dispatch while preserving explicit label/jump structure and verifier checks.

Reproduce these measurements:

```bash
python benchmarks/scripts/benchmark_extension_benefits_v110.py \
  --json benchmarks/results/BENCHMARK_EXTENSION_BENEFITS_V110.json
```

Raw evidence: [`benchmarks/results/BENCHMARK_EXTENSION_BENEFITS_V110.json`](benchmarks/results/BENCHMARK_EXTENSION_BENEFITS_V110.json).

### Benchmark discipline

These are microbenchmarks, not universal performance guarantees. CPU, operating system, CPython patch release, adaptive specialization state, key type, hit distribution, route-body shape, and surrounding application work can all change the result. The repository therefore keeps timing evidence separate from semantic qualification, validates results before timing, commits raw JSON, and uses structural tests to guard the mechanism behind the large-switch scaling result.

The broader 1.1.0 baseline is available through [`benchmarks/scripts/benchmark_readme_baseline_v110.py`](benchmarks/scripts/benchmark_readme_baseline_v110.py) and its committed JSON result. Historical benchmark evidence remains under `benchmarks/results/` and is not rewritten when a new release is cut.

See [`benchmarks/README.md`](benchmarks/README.md) for reproduction and evidence-retention guidance.

## Documentation

- **[Comprehensive guide](docs/COMPREHENSIVE_GUIDE.md)** — complete API, performance choices, guarded/frozen semantics, composition, troubleshooting, and deployment guidance.
- **[Architecture](docs/ARCHITECTURE.md)** — transformation pipeline, invariants, and subsystem responsibilities.
- **[Compatibility](docs/COMPATIBILITY.md)** — interpreter/runtime support boundary and unsupported environments.
- **[Release process](docs/RELEASING.md)** — reproducible build, artifact verification, and tag/release workflow.
- **[Release notes](docs/RELEASE_NOTES.md)** — release summary, including 1.1.0.
- **[Changelog](CHANGELOG.md)** — public release changes.
- **[Contributing](CONTRIBUTING.md)** — development expectations and test requirements.
- **[Security policy](SECURITY.md)** — how to report verifier, crash, or unsafe-boundary issues.

## Repository metadata

Canonical repository: **[Karvp/cpython-extensions](https://github.com/Karvp/cpython-extensions)**. Recommended repository settings and topics are tracked in [`.github/REPOSITORY_METADATA.md`](.github/REPOSITORY_METADATA.md). The recommended GitHub description is:

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
