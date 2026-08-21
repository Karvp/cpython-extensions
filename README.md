# cpython-extensions

**Production-oriented CPython 3.13 extensions for switch dispatch, guarded specialization/partial evaluation, bytecode-level function inlining, and validated local goto.**

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Implementation](https://img.shields.io/badge/implementation-CPython-306998)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MPL--2.0-brightgreen)](LICENSE)
[![Typing](https://img.shields.io/badge/typing-py.typed-informational)](src/python_extensions/py.typed)

The distribution is **`cpython-extensions`** and the primary import package is **`python_extensions`**. The project deliberately targets CPython internals and currently supports **CPython 3.13.x** (`>=3.13,<3.14`).

```bash
python -m pip install cpython-extensions
```

```python
from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    hotpath,
    inline_calls,
    inline_function,
    optimize_extensions,
    partial,
    specialize,
    switch,
)
```

> `goto .name` and `label .name` are compile-time pseudo-statements recognized inside `@enable_goto` functions; they are not runtime objects that need to be imported.

## At a glance

| Capability | What it provides | Production-oriented default |
|---|---|---|
| **Switch** | Hash/table-backed multi-way dispatch, typed key identity, guarded cases, fallthrough, and optional live self-modifying dispatch | `mode="auto"` (portable) |
| **Partial** | Freeze selected parameters into a real transformed function and eliminate provably dead work | explicit `partial(...)` |
| **Specialize** | Guarded exact-type/constant variants with guaranteed generic fallback | explicit `@specialize(...)` |
| **Hotpath** | Bounded adaptive discovery and promotion of profitable argument shapes | `policy="speed", backend="auto"` |
| **Inline** | Bytecode-level call inlining, profitability checks, shared regions, data-flow optimization | `policy="speed", binding="frozen"` |
| **Goto** | Explicit local jumps with exception-region and CFG validation | `mode="strict"` |
| **Verification** | Post-transform bytecode/control-flow verification and transformation reports | Keep enabled |

The package favors **general Python semantics, explicit opt-ins, bounded adaptive state, and fail-closed transformation** over benchmark-specific shortcuts.

## Why cpython-extensions?

Python intentionally keeps its language and execution model structured. Some hot interpreters, parsers, generated state machines, numeric kernels, and stable internal helpers nevertheless benefit from lower-level control. `cpython-extensions` provides a set of CPython-specific transformations while keeping the selected plan inspectable and independently verifiable.

- **Switch** — efficient multi-route dispatch without a manually maintained handler dictionary or long `if/elif` ladder.
- **Partial / specialize / hotpath** — expose constants and exact runtime types to the bytecode optimizer while retaining explicit guards or generic fallback where required.
- **Inline** — clone eligible helper bodies into callers, with profitability analysis and guarded binding for replaceable targets.
- **Goto** — local control-flow jumps for generated state machines and carefully audited low-level code.

This is not a replacement Python implementation and does not claim portability to PyPy or other interpreters.

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

The default `mode="auto"` is deliberately portable and never mutates executable bytecode. Live dispatch is an explicit CPython-3.13-only optimization for hot, repeated in-frame routing:

```python
@enable_switch(mode="fast", live_engine="auto")
def run_vm(opcodes):
    acc = 0
    for opcode in opcodes:
        with switch(opcode):
            if case(0):
                acc += 1
            if case(1):
                acc ^= 7
            # ... many heterogeneous opcode bodies ...
    return acc
```

`live_engine="auto"` uses the optional fused C dispatcher when its runtime self-test succeeds and otherwise falls back to the historical ctypes gate. `live_engine="native"` requires the C accelerator; `live_engine="ctypes"` is mainly useful for diagnostics and reproducible comparisons.

**Do not assume live is universally faster.** The extensive 1.2.0 qualification shows strong gains for large dense integer VM/parser loops, while portable statement-template/direct-value lowering remains preferable for ordinary HTTP/RPC routing and trivial cases. See [Live switch architecture and performance](docs/LIVE_SWITCH.md).

### Partial evaluation

```python
from python_extensions import partial

def parse(data, mode="safe"):
    if mode == "fast":
        return fast_parse(data)
    return checked_parse(data)

fast_parse_only = partial(parse, mode="fast")
```

`partial()` produces a transformed Python function whose frozen parameters are removed from the effective call signature. Safe constant propagation and dead-branch elimination are applied without changing observable local-variable behavior.

### Guarded specialization

```python
from python_extensions import specialize

@specialize(constants={"mode": "fast"}, types={"value": int})
def convert(value, mode="safe"):
    if type(value) is int and mode == "fast":
        return value + 1
    return slow_convert(value, mode)
```

The specialized variant is guarded. A guard miss executes the original generic function. Exact-type and constant guards are used only where their matching semantics are safe.

### Adaptive hot paths

```python
from python_extensions import hotpath

@hotpath(threshold=64, max_variants=1, policy="speed")
def decode(value, mode):
    if mode == "binary":
        return decode_binary(value)
    return decode_text(value)
```

`hotpath()` observes only a bounded number of shapes for a bounded profiling budget. On eligible ordinary functions, `backend="auto"` prefers CPython 3.13 `sys.monitoring` during warm-up and can install a verified in-frame dispatcher after promotion. It falls back to a wrapper where that contract is not suitable. See [Specialization and partial evaluation](docs/SPECIALIZATION.md).

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

The default `binding="frozen"` is the highest-optimization mode and assumes the target intentionally remains stable after transformation. Use guarded binding when a callee may be rebound or mutated:

```python
@inline_calls(policy="always", binding="guarded")
def plugin_sensitive(x):
    return affine(x)
```

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

The source-level notation is inspired in part by [Entrian's “goto for Python”](https://entrian.com/goto/). `cpython-extensions` uses an independent CPython 3.13 lowering pipeline with strict CFG/exception-region validation and post-transform verification.

### Compose extensions

Composition uses one fixed order:

```text
switch -> partial -> inline -> goto -> specialize/hotpath
```

```python
from python_extensions import optimize_extensions

@optimize_extensions(
    switch=True,
    partial={"mode": "fast"},
    inline={"policy": "speed"},
    goto=True,
    specialize={"types": {"value": int}},
)
def execute(value, mode="safe"):
    ...
```

`specialize` and `hotpath` are alternative final layers and cannot both be enabled in the same pipeline.

Inspect transformed functions rather than blindly trusting them:

```python
from python_extensions import explain_extensions, verify_code

print(explain_extensions(execute))
verify_code(execute.__code__)
```

## Choosing the right mode

| Area | Recommended default | Choose another mode when... |
|---|---|---|
| Switch | `mode="auto"` | Use live modes only after accepting their CPython/runtime and concurrency contracts |
| Live engine | `live_engine="auto"` | Force `native` for certification or `ctypes` for fallback/diagnostic comparison |
| Case identity | `case_key_mode="python"` | Exact runtime types such as `1`, `1.0`, and `True` must remain distinct |
| Partial | explicit frozen bindings | You can prove the bound configuration is intentionally stable |
| Specialize | explicit constants/types | You know the valuable shape and need generic fallback |
| Hotpath | `policy="speed"`, bounded defaults | Runtime shape discovery is more useful than declaring variants manually |
| Inline binding | `binding="frozen"` | Use `guarded` for hot reload, plugins, monkey-patching, replaceable methods, or mutable defaults |
| Inline policy | `policy="speed"` | Use `always` only for controlled experiments or measured tradeoffs |
| Goto | `mode="strict"` | `unsafe` is reserved for carefully audited low-level experiments |
| Verification | enabled | Do not bypass verifier failures in production |

## Installation and native accelerator

### PyPI

```bash
python -m pip install cpython-extensions
```

When the build environment can compile the optional extension, the installed wheel contains `python_extensions._livegate`, the native live-switch accelerator. Source installations retain portable/ctypes functionality if that optional extension build is unavailable.

Check availability:

```python
import importlib.util
print(importlib.util.find_spec("python_extensions._livegate") is not None)
```

The native accelerator is **not imported automatically on free-threaded CPython 3.13 builds** because this release does not certify live self-modifying dispatch for no-GIL execution. Portable switch mode remains the supported path there.

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

Long-running stress tests are intentionally separated from ordinary pull-request feedback. See `.github/workflows/stress.yml` and the harnesses under `tests/`.

## Release quality

Version **1.2.0** is the current documented release line. It adds guarded specialization/partial evaluation and renovates the explicit CPython 3.13 live-switch backend with an optional fused native C dispatcher, dense/sparse exact-int routing, typed-builtin fast partitions, and corrected `EXTENDED_ARG` handling for large/multi-site generated functions.

The 1.2.0 qualification baseline on CPython 3.13.5 records:

- **450/450** package tests;
- **1,239,100** calls through the existing full live-switch compatibility harness under CPython dev mode;
- a **30-configuration** cross-task live benchmark matrix;
- **202,798,080 timed dispatches** in the broad matrix, excluding warmups and correctness checks;
- additional 5–10 million-dispatch sustained-loop tests;
- explicit VM/parser, state-machine, HTTP/RPC, sparse-protocol, threaded, async, and per-request controls.

Release evidence:

- [`PYTHON_EXTENSIONS_1.2.0_CERTIFICATION.txt`](PYTHON_EXTENSIONS_1.2.0_CERTIFICATION.txt)
- [`RELEASE_AUDIT_1.2.0.txt`](RELEASE_AUDIT_1.2.0.txt)
- [`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md)
- [`benchmarks/results/BENCHMARK_LIVE_EXTENSIVE_V122.md`](benchmarks/results/BENCHMARK_LIVE_EXTENSIVE_V122.md)
- [`benchmarks/results/BENCHMARK_LIVE_EXTENSIVE_V122.json`](benchmarks/results/BENCHMARK_LIVE_EXTENSIVE_V122.json)

The `v121`/`v122` suffixes in benchmark and regression filenames are **engineering evidence identifiers**, not package versions.

## Performance: use workload shape, not slogans

The portable compiler and live backend solve different problems. Portable mode is still the default because it can often collapse direct values and shared statement/expression shapes into extremely compact dispatch. Live mode keeps heterogeneous bodies inline and changes a bytecode jump gate at runtime; it is strongest when one outer call performs many dispatches among many distinct code regions.

Selected real-backend CPython 3.13.5 results from the extensive qualification:

| Workload | Routes / traffic | Portable | Native live | Native vs portable |
|---|---|---:|---:|---:|
| Dense VM | 64 / random | 250.4 ns | **147.4 ns** | **1.70×** |
| Dense VM | 1,024 / skewed | 330.6 ns | **160.5 ns** | **2.06×** |
| Dense VM | 2,048 / random | 393.3 ns | **159.2 ns** | **2.47×** |
| Integer parser | 256 / skewed | 289.0 ns | **156.5 ns** | **1.85×** |
| State machine | 128 / random | 152.9 ns | **121.1 ns** | **1.26×** |
| HTTP string router | 64 / random | **230.7 ns** | 230.0 ns | ~1.00× |
| Sparse protocol IDs | 256 / random | **237.1 ns** | 242.2 ns | 0.98× |
| Heavy server bodies | 256 / random | **406.3 ns** | 427.6 ns | 0.95× |
| Direct/minimal control | 256 / random | **49.7 ns** | 62.2 ns | 0.80× |

A separate **10,000,384-dispatch** 1,024-route VM sample recorded about **329.4 ns portable vs 169.0 ns native**, or **1.95×**. A 10-million-dispatch HTTP loop remained essentially tied (**228.9 ns portable vs 231.2 ns native**).

For a classic server that performs one router call per request, the 64-route control measured approximately **135.9 ns portable**, **141.0 ns shared native**, and **246.2 ns thread-local native**. This is why ordinary web routing should remain portable even though live dispatch is excellent for interpreter loops embedded inside a request or worker.

These are measured results from one certified host, not universal guarantees. CPU, OS, CPython patch/build, adaptive state, key type, route-body shape, traffic distribution, and surrounding work matter. Read [the live-switch guide](docs/LIVE_SWITCH.md) and [benchmark methodology](benchmarks/README.md) before making a backend decision.

The earlier 1.1.0 direct-value scaling evidence remains valid and is retained unchanged under `benchmarks/results/`: it answers a different question—how a portable table-backed switch scales against linear `if/elif` and `match` chains.

## Documentation

- **[Comprehensive guide](docs/COMPREHENSIVE_GUIDE.md)** — complete API, composition, deployment, and troubleshooting guidance.
- **[Live switch architecture and performance](docs/LIVE_SWITCH.md)** — native/ctypes engines, concurrency contracts, benchmark interpretation, and workload selection.
- **[Specialization and partial evaluation](docs/SPECIALIZATION.md)** — `partial`, `specialize`, `hotpath`, guards, profiling bounds, and composition.
- **[Architecture](docs/ARCHITECTURE.md)** — transformation pipeline, invariants, and subsystem responsibilities.
- **[Compatibility](docs/COMPATIBILITY.md)** — interpreter/runtime/build support boundary.
- **[Release process](docs/RELEASING.md)** — reproducible build, artifact verification, and tag/release workflow.
- **[Release notes](docs/RELEASE_NOTES.md)** — release summaries.
- **[Benchmarks](benchmarks/README.md)** — benchmark reproduction and evidence-retention rules.
- **[Changelog](CHANGELOG.md)** — public release changes.
- **[Contributing](CONTRIBUTING.md)** — development expectations and test requirements.
- **[Security policy](SECURITY.md)** — reporting verifier, crash, unsafe-boundary, and native live-gate issues.

## Repository metadata

Canonical repository: **[Karvp/cpython-extensions](https://github.com/Karvp/cpython-extensions)**. Recommended repository settings and topics are tracked in [`.github/REPOSITORY_METADATA.md`](.github/REPOSITORY_METADATA.md).

Recommended GitHub description:

> CPython 3.13 extensions for fast switch/live dispatch, specialization, function inlining, and verified local goto.

## Contributing

Contributions are welcome when they preserve general Python semantics and avoid benchmark- or fixture-specific shortcuts. Performance changes must include correctness coverage and a workload-appropriate benchmark; a microbenchmark win is not sufficient if a broader certified workload regresses.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Low-level bytecode transformation and explicit live self-modification amplify interpreter/runtime assumptions. Crashes, verifier bypasses, incorrect exception-region handling, unsafe gate writes, or unexpected no-GIL behavior should be treated as security-relevant until triaged.

See [SECURITY.md](SECURITY.md).

## License

This project is licensed under the **Mozilla Public License 2.0 (MPL-2.0)**. See [LICENSE](LICENSE).

MPL-2.0 is a file-level copyleft license: modifications to MPL-covered source files remain under MPL-2.0 when distributed, while the license permits those files to be combined with a larger work under different terms, subject to the license conditions.
