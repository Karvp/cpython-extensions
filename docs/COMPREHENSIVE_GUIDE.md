# cpython-extensions / `python_extensions` 1.0.4 — Comprehensive Guide

`cpython-extensions` is a CPython 3.13 library that adds three opt-in transformation tools while keeping ordinary Python syntax and callables at the boundary:

- **switch** — compiled multi-way dispatch with portable semantics-safe backends and optional live backends.
- **inline** — direct-call bytecode inlining plus local/CFG/dataflow optimization.
- **goto** — explicit local jumps using `goto .name` / `label .name`, with strict control-flow validation by default.

The distribution name is **`cpython-extensions`**; the import package is **`python_extensions`**.

Version 1.0.4 is a benchmark/documentation release over the unchanged 1.0.3 transformation runtime. The guarded-binding behavior remains the current inline contract.

## 1. Installation

```bash
py -3.13 -m pip install cpython-extensions
```

For a source checkout:

```bash
py -3.13 -m pip install -e .
py -3.13 -m pytest
```

The supported release line is CPython 3.13.x. The inliner depends on `bytecode>=0.17,<0.18`; switch and goto are lazily usable even when that dependency is not imported yet.

## 2. Start with the safe production defaults

```python
from python_extensions import (
    switch, case, fallthrough, enable_switch,
    inline_function, inline_calls,
    enable_goto,
    optimize_extensions,
    explain_extensions, verify_code,
)
```

Recommended defaults:

| Feature | Production starting point | Why |
|---|---|---|
| switch | `@enable_switch` / `mode="auto"` | portable, thread/recursion safe; live mutation is not used unless explicitly requested |
| switch key semantics | `case_key_mode="python"` | matches ordinary Python dict equality/hash behavior |
| inline policy | `policy="speed"` | skips expansions that do not have a conservative static benefit |
| inline binding | `binding="frozen"` for immutable hot paths; `binding="guarded"` for mutable targets | choose speed/density versus dynamic rebinding fidelity explicitly |
| shared inline bodies | `shared_regions="auto"` | activates only for callees explicitly marked `shared_region=True` |
| goto | `mode="strict"` | rejects invalid jumps across exception-protection boundaries |
| composition | `switch -> inline -> goto` | this is the library's canonical verified order |

## 3. Switch: readable multi-way dispatch

### 3.1 Basic dispatch

```python
from python_extensions import switch, case, enable_switch

@enable_switch
def classify(token):
    with switch(token):
        if case("start", "run"):
            return "active"
        elif case("stop"):
            return "inactive"
        else:
            return "unknown"
```

The subject is evaluated once. Portable lowering preserves dictionary-style hashing/equality behavior and supports closures, generators, coroutines, async generators, guards, defaults, and fallthrough.

### 3.2 Typed keys

Ordinary Python considers `1`, `1.0`, and `True` equal as mapping keys. If that is not what your protocol wants, select exact-type identity:

```python
@enable_switch(case_key_mode="typed")
def decode(value):
    with switch(value):
        if case(1):
            return "int-one"
        elif case(1.0):
            return "float-one"
        elif case(True):
            return "true"
        else:
            return "other"
```

Use `case_key_mode="python"` when you want normal Python equality/hash semantics; use `"typed"` when type is part of the protocol.

### 3.3 Fallthrough

```python
from python_extensions import fallthrough

@enable_switch
def permissions(role):
    out = []
    with switch(role):
        if case("admin"):
            out.append("admin")
            fallthrough()
        elif case("staff"):
            out.append("staff")
            fallthrough()
        elif case("user"):
            out.append("user")
    return out
```

For large fallthrough-heavy functions, `compact_routes=True` trades some routing simplicity for code-size reduction. `compact_routes="auto"` uses a conservative density heuristic. The default `False` remains speed-first.

### 3.4 Choosing a switch backend

```python
@enable_switch(mode="auto")
def route(value): ...
```

- `auto`: recommended. Uses the fastest semantics-safe portable lowering. If you explicitly set `live_threshold=N`, sufficiently large switches may opt into isolated live dispatch.
- `portable`: force non-self-modifying lowering.
- `isolated`: cached live code object per thread/active depth; safer live mode.
- `per_call`: fresh live clone for each invocation.
- `thread_local`: one live clone per thread; not re-entry safe.
- `fast`: shared live code object; only for a proven single-active-call environment.

Prefer `auto`/`portable` for libraries, servers, async applications, recursive code, and code you do not fully control. Treat live modes as explicit low-level performance tools that require workload-specific validation.

### 3.5 Generated/notebook functions

The switch transformer normally retrieves source with `inspect`. Generated, frozen, REPL, or notebook functions may not have retrievable source. Supply it explicitly:

```python
source = '''\ndef generated(x):\n    with switch(x):\n        if case(1):\n            return "one"\n        else:\n            return "other"\n'''

@enable_switch(source=source)
def generated(x):
    ...
```

For production, define transformed functions in real modules whenever possible; source files give the best debugger, traceback, coverage, and deployment behavior.

## 4. Inline: remove calls without giving up control

### 4.1 Register a helper, transform a caller

```python
from python_extensions import inline_function, inline_calls

@inline_function(register_only=True)
def affine(x, scale=7, bias=3):
    return x * scale + bias

@inline_calls(policy="speed")
def hot_path(x):
    return affine(x) + affine(x + 1)
```

`register_only=True` makes the helper available to callers but does not rewrite the helper itself. `inline_calls` transforms registered direct calls found in the caller.

### 4.2 `policy="speed"` versus `policy="always"`

- `speed`: production default. Uses conservative static profitability checks and may leave a normal CALL in place.
- `always`: prioritize call elimination/code density experiments even when a runtime speed win is not proven.

Do not assume every inlined function is faster. Small calls can already be efficient on modern CPython, and guard/setup/code-cache costs matter.

### 4.3 The important 1.0.2 choice: frozen versus guarded binding

#### Frozen binding

```python
@inline_calls(policy="speed", binding="frozen")
def stable_path(x):
    return affine(x)
```

`binding="frozen"` means the cloned inline implementation is intentionally a decoration-time snapshot where the call shape can be proven. This is the backward-compatible high-performance mode.

Use it when:

- helpers are internal implementation details;
- their global binding will not be monkey-patched;
- `__code__`, `__defaults__`, and `__kwdefaults__` will not be replaced after callers are decorated;
- class methods/descriptors used as inline targets are not dynamically replaced;
- maximum call-elimination benefit matters.

#### Guarded binding

```python
@inline_calls(policy="always", binding="guarded")
def dynamic_path(x):
    return affine(x)
```

`binding="guarded"` loads the callable exactly once, validates that it still represents the function state cloned into the caller, and takes the inline fast path only when the guard matches. Otherwise it deoptimizes to the ordinary CALL using that already-loaded callable.

It observes between-call changes including:

- global or closure rebinding;
- `function.__code__` replacement;
- positional-default tuple replacement;
- relevant keyword-only default replacement/mutation;
- staticmethod/classmethod/instance-method replacement;
- `functools.partial` argument/keyword mutation;
- callable-object `type(obj).__call__` replacement.

Mutable objects *inside* a default remain live when their identity is unchanged, matching the cloned constant object.

For a public library or plugin architecture where monkey-patching/reconfiguration is expected, prefer guarded binding. With `policy="speed"`, a trivial call may intentionally remain uninlined because the guard costs more than the expected benefit.



### 4.3.1 Guarded binding is semantic hardening, not a synchronization primitive

Guarded mode protects the *call decision* against stale decoration-time state. It is designed for changes that occur between calls. It does not make arbitrary concurrent mutation of a function, descriptor, partial, or its dependencies transactional. If one thread rewrites a target while another thread is executing that target, use the same application-level synchronization you would need for ordinary Python calls.

The guard deliberately uses identity/state checks instead of user equality. It also evaluates the callable lookup once per invocation, so a descriptor or namespace lookup is not repeated merely because the inline fast path deoptimizes.

`freeze_globals=True` and `freeze_closures=True` are explicit snapshot tools. Guarded binding validates the callable state that selects the cloned implementation, but it cannot make deliberately frozen *dependencies inside that implementation* dynamic again. If those dependencies need normal late binding, do not freeze them.

A practical matrix:

| Target lifecycle | Recommended binding | Notes |
|---|---|---|
| private helper never patched after import | `frozen` | maximum optimizer freedom |
| plugin/hot-reload target may be rebound | `guarded` | deopts when binding identity changes |
| same function object may receive new `__code__` | `guarded` | deopts on code mismatch |
| defaults/kw-only defaults may be reconfigured | `guarded` | validates only state relevant to the call shape |
| helper dependencies are intentionally frozen | `frozen` or guarded + explicit freeze | snapshot is intentional |
| target changes continuously under contention | ordinary CALL or external synchronization | guards are not a transaction protocol |

### 4.4 Defaults and argument semantics

The inliner supports positional-only, positional, keyword-only, variadic, defaults, and selected partial/bound-call shapes when it can preserve evaluation order and binding semantics. It deliberately fails closed for unsupported shapes.

A useful rule: **do not rewrite code merely to trick the inliner**. If a shape is left as CALL, treat that as a semantics safeguard unless profiling demonstrates a reason to restructure it.

### 4.5 Closures and foreign globals

By default, functions whose closure/global environment cannot be safely merged are rejected or left ordinary.

Explicit capture controls exist for controlled cases:

```python
@inline_function(register_only=True, freeze_closures=True)
def helper(x): ...

@inline_function(register_only=True, freeze_globals=True)
def helper_from_other_namespace(x): ...
```

These are snapshot semantics. Use them only when capture is intentional.

### 4.6 Shared inline regions for repeated large helpers

Repeatedly duplicating a large helper can increase decoration time and code size. Mark it shareable:

```python
@inline_function(register_only=True, shared_region=True)
def transform(x):
    x = x * 3 + 1
    x = x * 5 - 2
    return x

@inline_calls(
    policy="speed",
    shared_regions="auto",
    shared_min_calls=3,
)
def pipeline(a, b, c):
    x = transform(a)
    y = transform(b)
    z = transform(c)
    return x + y + z
```

Shared regions append one cloned body and route eligible call sites through frame-local continuations. This is the preferred technique for dozens/hundreds of repetitions of a non-trivial helper.

Eligibility is deliberately conservative. Statement-separated calls are easier to share than multiple calls buried in one compound expression. If sharing is rejected, the transformer falls back to duplicated inlining or ordinary CALL rather than weakening semantics.

### 4.7 Expansion safety limits

Inlining has hard limits:

- `max_expansions`
- `max_growth_factor`
- `max_code_bytes`

Tune them downward in build systems that process untrusted/generated code. Tune upward only after measuring decoration time, code size, import latency, and instruction-cache behavior.

## 5. Goto: structured low-level control flow

```python
from python_extensions import enable_goto

@enable_goto
def sum_to(n):
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

`mode="strict"` is the recommended default. It verifies synthetic jumps against the final CFG and rejects unsafe movement across CPython exception-table protection boundaries.

`mode="unsafe"` exists for controlled experiments. Do not use it as the default in application/library code.

Goto supports generator/coroutine suspension patterns when the resulting edge is valid, but it is still a low-level tool. Prefer ordinary loops/branches unless explicit labels make a state machine or generated control flow substantially clearer.

## 6. Compose the three extensions safely

Use the canonical pipeline rather than manually guessing decorator order:

```python
from python_extensions import optimize_extensions

@optimize_extensions(
    switch={"mode": "auto", "case_key_mode": "typed"},
    inline={"policy": "speed", "binding": "guarded"},
    goto={"mode": "strict"},
)
def execute(opcode, value):
    ...
```

The fixed order is:

1. switch — source-level lowering may rebuild code;
2. inline — merges registered callee bytecode into the lowered caller;
3. goto — resolves pseudo labels after code growth;
4. final verifier — validates the resulting code object.

Bare `@optimize_extensions` is validation-only; it does not guess which transformations you wanted.

## 7. Inspect what happened

### Verify generated bytecode

```python
from python_extensions import verify_code

result = verify_code(my_function.__code__)
assert result.valid, result.errors
```

### Explain/report transformations

```python
from python_extensions import explain_extensions

print(explain_extensions(my_function))
```

Transformed functions also carry extension-specific metadata/reports. For inline-heavy tuning, inspect `function.__inline_stats__` rather than assuming a requested call was actually inlined.

Useful questions:

- How many calls were inlined versus skipped as unprofitable/unsupported?
- Did shared regions activate?
- Did constant/dataflow/stack optimizations fire?
- How did final code size compare with original code size?
- Which binding/policy/backend was recorded?

## 8. Registry lifecycle

```python
from python_extensions import (
    registered_inline_functions,
    unregister_inline_function,
    clear_inline_registry,
)
```

The registry uses weak-reference lifecycle cleanup and transactional registration. Still, tests/plugins that register helpers dynamically should explicitly unregister or clear their own registrations at lifecycle boundaries.

Avoid calling `clear_inline_registry()` from a generic per-test fixture in a large suite if other imported test modules rely on module-level registered helpers; clearing is process-global by design.

## 9. Production patterns

### Command router

```python
@enable_switch(case_key_mode="typed")
def dispatch(command):
    with switch(command.kind):
        if case("read"):
            return handle_read(command)
        elif case("write"):
            return handle_write(command)
        else:
            return handle_unknown(command)
```

Good fit: medium/large stable routing tables where readable case syntax matters.

### Numeric kernel with stable private helpers

```python
@inline_function(register_only=True)
def mix(x, y):
    return (x * 31) ^ y

@inline_calls(policy="speed", binding="frozen")
def kernel(values):
    total = 0
    for x, y in values:
        total += mix(x, y)
    return total
```

Good fit: private helpers whose implementation is immutable after module import.

### Plugin/hot-reload aware inline call

```python
@inline_function(register_only=True)
def policy(value, bias=1):
    return value + bias

@inline_calls(policy="speed", binding="guarded")
def apply_policy(value):
    return policy(value)
```

Good fit: applications where runtime patching/configuration can replace the target. If the helper is too trivial for the guard to pay off, `policy="speed"` leaves it as a normal call automatically.

### Parser/state machine

```python
@enable_goto(mode="strict")
def scan(data):
    i = 0
    label .next
    if i >= len(data):
        goto .done
    # state-machine work
    i += 1
    goto .next
    label .done
    return i
```

Good fit: generated parsers/interpreters where explicit CFG structure is useful and verified.

## 10. Performance methodology

Measure **three** costs separately:

1. **decoration/import cost** — source parsing, bytecode reconstruction, verification, optimization;
2. **runtime steady-state cost** — the transformed hot loop/call path;
3. **code-size/cache cost** — large duplicated bodies can hurt instruction-cache locality even when a microbenchmark is faster.

Recommended benchmark shape:

```python
import timeit

baseline = timeit.repeat("baseline_fn(data)", globals=globals(), number=100_000, repeat=7)
optimized = timeit.repeat("optimized_fn(data)", globals=globals(), number=100_000, repeat=7)
```

Interleave variants in separate processes for serious work, pin the same Python build, warm up first, report distributions rather than one run, and include representative misses/errors—not only best-case hits.

Do not optimize based solely on generated bytecode length. Smaller code can be slower and larger code can be faster depending on branch prediction, cache behavior, CALL specialization, and workload distribution.

## 11. Threading, async, recursion, and multiprocessing

- Portable switch is the default for shared/threaded/recursive/async code.
- Guarded inline calls add no registry lock to the runtime fast path; registration/decorating is synchronized separately.
- A transformed ordinary function can be called concurrently when its own Python logic is thread-safe.
- Explicit live switch modes have different re-entry/concurrency contracts; use `isolated` when you truly need live dispatch in concurrent environments.
- Multiprocessing works like normal Python module import: build transformed functions at import time or import pre-transformed module definitions in workers. Ensure dynamically generated source is reproducible in each worker.

## 12. Debugging and failure modes

### Switch errors

Expect explicit errors for duplicate cases/defaults, malformed markers, unsupported runtime/source shapes, and invalid fallthrough. User `__hash__`/`__eq__` exceptions are not swallowed merely to force a default path.

### Inline errors

Common reasons a call remains ordinary or raises during decoration:

- recursive inline cycle;
- unsupported generator/coroutine/cell-variable callee shape;
- incompatible globals without `freeze_globals=True`;
- code-growth/expansion limit reached;
- an active exception context cannot safely contain the callee's own exception IR;
- guarded binding cannot validate the target without introducing user-visible extra lookup behavior;
- `policy="speed"` decides the expansion is not profitable.

### Goto errors

Strict mode rejects missing/duplicate labels, invalid stack edges, and cross-region exception semantics. Fix the control flow rather than switching to `unsafe` unless you are intentionally experimenting with raw behavior.

## 13. Compatibility imports

The preferred API is:

```python
import python_extensions as pe
```

Compatibility modules `pyswitch`, `inline_function`, and `pygoto` remain available for older code. New applications should prefer the unified package so composition/reporting/version behavior stays consistent.

## 14. Release checklist for applications using the package

Before deploying a transformed module:

1. Run your full unit/integration suite on the exact CPython 3.13 patch release you deploy.
2. Run `verify_code()` over critical transformed callables.
3. Benchmark representative runtime traffic and record decoration/import time.
4. Use `binding="guarded"` anywhere targets may be rebound, monkey-patched, hot-reloaded, or have defaults/code replaced.
5. Use `binding="frozen"` only where snapshot semantics are intentional.
6. Keep switch on `auto`/`portable` unless a live mode has a demonstrated benefit and its concurrency contract is proven.
7. Keep goto on `strict`.
8. Mark large repeated inline helpers `shared_region=True` and confirm `calls_shared` in stats.
9. Keep expansion/code-size limits finite.
10. Exercise exceptions, tracing/coverage, threads/async, serialization, and shutdown paths relevant to your application.
11. Pin the package version and the supported `bytecode` dependency range in reproducible deployments.
12. Keep the wheel/sdist hashes used for production deployment.

## 15. Decision cheat sheet

If you want **readable multi-way dispatch**, start with `@enable_switch`.

If you want **to optimize a small stable private helper**, register it and use `@inline_calls(policy="speed", binding="frozen")`.

If the helper **can be patched/reconfigured after decoration**, use `binding="guarded"`.

If one non-trivial helper appears **many times**, add `shared_region=True` and keep `shared_regions="auto"`.

If you are building a **parser/interpreter/state machine** and labels materially simplify generated flow, use `@enable_goto(mode="strict")`.

If a function uses multiple extensions, use `@optimize_extensions(...)` instead of manual decorator ordering.

If a transformation is skipped, inspect the report/stats first. A conservative non-transform is normally preferable to an optimization that changes Python semantics.

## 16. Migrating from 1.0.1 to 1.0.2

1.0.2 is source-compatible with the normal 1.0.1 API. Existing callers continue to use `binding="frozen"` unless they opt in to guarded semantics, so upgrading does not silently add runtime target checks.

Recommended migration audit:

1. Search transformed callers for helpers that are monkey-patched, hot-reloaded, rebound in tests, configured through `__defaults__` / `__kwdefaults__`, or replaced on classes.
2. Change only those callers to `binding="guarded"`.
3. Keep `policy="speed"` first; inspect `__inline_stats__` to see whether guarded expansion remains profitable.
4. If a guarded site is intentionally tiny and remains an ordinary CALL, accept that result unless profiling shows a real reason to force `policy="always"`.
5. Re-run tests that mutate the helper *after* caller decoration. This is the scenario guarded mode specifically hardens.
6. Re-measure import/decorator time and hot-path runtime. Do not copy a microbenchmark decision across workloads.

No import rename is required: the distribution installed by pip is `cpython-extensions`, while code still imports `python_extensions`.

## 17. Public API reference

These signatures are the 1.0.4 production surface. Defaults shown here matter because several options deliberately select different semantic/performance contracts.

### Switch

```python
enable_switch(
    func=None, /, *,
    mode="auto",
    unsafe_shared_slot=None,
    source=None,
    live_threshold=None,
    portable_match_threshold=5,
    max_cached_depth=16,
    expose_debug=False,
    case_key_mode="python",
    compact_routes=False,
)
```

Marker API inside a transformed function: `with switch(subject):`, `case(*keys, when=...)`, `case()` for default, and `fallthrough()` for explicit continuation. Prefer `mode="auto"`, `case_key_mode="python"`, and `compact_routes=False` until a representative benchmark justifies another choice.

### Inline registration / transformation

```python
inline_function(
    func=None, *,
    register_only=False,
    freeze_closures=False,
    policy="speed",
    binding="frozen",
    stack_strategy="auto",
    fusion_strategy="auto",
    region_dataflow=True,
    freeze_globals=False,
    shared_region=False,
    shared_regions="auto",
    shared_min_calls=3,
    shared_min_body_instructions=12,
    max_expansions=10_000,
    max_growth_factor=256,
    max_code_bytes=16_777_216,
)

inline_calls(
    func=None, *,
    policy="speed",
    binding="frozen",
    stack_strategy="auto",
    fusion_strategy="auto",
    region_dataflow=True,
    shared_regions="auto",
    shared_min_calls=3,
    shared_min_body_instructions=12,
    max_expansions=10_000,
    max_growth_factor=256,
    max_code_bytes=16_777_216,
)
```

Registry helpers:

```text
registered_inline_functions() -> tuple[str, ...]
unregister_inline_function(func) -> bool
clear_inline_registry() -> None
```

Treat `fusion_strategy="aggressive"` as an explicit observability tradeoff: it may remove otherwise visible synthetic/local materialization that tracing or `f_locals`-sensitive tooling could observe. The normal strategies are preferable for general application code.

### Goto

```python
enable_goto(func=None, /, *, mode="strict")
```

Markers are `label .name` and `goto .name`. Keep strict mode unless the application intentionally accepts raw control-flow hazards.

### Composition, reports, and verification

```python
optimize_extensions(
    func=None, /, *,
    switch=False,
    inline=False,
    goto=False,
)

verify_code(code, *, raise_on_error=True)
explain_extensions(function) -> str
```

Each `switch` / `inline` / `goto` argument to `optimize_extensions` is either `False`, `True` for that extension's defaults, or an option mapping. Example:

```python
@optimize_extensions(
    switch={"mode": "auto", "case_key_mode": "typed"},
    inline={"policy": "speed", "binding": "guarded"},
    goto={"mode": "strict"},
)
def execute(opcode, value):
    ...
```

## 18. Troubleshooting quick table

| Symptom | Likely reason | Preferred response |
|---|---|---|
| switch decorator cannot inspect source | REPL/stdin/generated function | define it in a module or pass exact `source=` |
| `1`, `1.0`, `True` collapse to one case | Python mapping equality | use `case_key_mode="typed"` when exact type is part of the key |
| inline target changed but caller uses old behavior | frozen snapshot semantics | use `binding="guarded"` before decorating caller |
| guarded `policy="speed"` reports no inline | guard cost exceeds conservative benefit | keep CALL, enlarge helper workload, or force `always` only after profiling |
| large caller is slow to decorate | many duplicated inline bodies | mark suitable helper `shared_region=True`, split generated functions, or reduce expansion limits |
| shared region did not activate | call shape/body below eligibility threshold | inspect stats; use statement-separated calls; do not weaken semantics merely to force sharing |
| cross-module helper is declined | global namespace cannot be merged safely | keep ordinary CALL or intentionally use `freeze_globals=True` with snapshot semantics |
| strict goto rejects a jump | stack/exception-region edge is unsafe | restructure control flow; do not paper over it with unsafe mode |
| optional bytecode dependency missing | inline subsystem requested without dependency | install `bytecode>=0.17,<0.18` / normal distribution dependencies |
| transformed behavior differs only under tracing/profiling | aggressive optimization changed observability | return to conservative fusion/stack settings and test with your instrumentation |

## 19. What not to do

- Do not use `fast` live switch mode in shared/reentrant code simply because its name sounds best.
- Do not choose `policy="always"` globally; forced inlining can increase runtime and import cost.
- Do not use frozen binding for targets your framework deliberately patches at runtime unless snapshot behavior is the goal.
- Do not assume guarded binding synchronizes concurrent mutation.
- Do not freeze globals/closures that are meant to stay dynamically configurable.
- Do not increase expansion/code-size limits without measuring import time and instruction-cache effects.
- Do not use unsafe goto to silence a strict-mode control-flow error you have not understood.
- Do not depend on private `python_extensions` implementation modules or synthetic local names; use the public API and reports.

