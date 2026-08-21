# Specialization and partial evaluation

Version 1.2.0 adds three related APIs:

- `partial()` — explicit compile-time-style freezing of selected arguments;
- `specialize()` — guarded constant/exact-type variants with generic fallback;
- `hotpath()` — bounded adaptive discovery and promotion of profitable argument shapes.

They share the same bytecode partial-evaluation machinery but expose different contracts.

## 1. `partial()`

```python
from python_extensions import partial

fast = partial(parse, mode="fast")
```

Decorator form is also supported for keyword bindings:

```python
@partial(mode="fast")
def parse(data, mode="safe"):
    ...
```

The result is a transformed Python function, not a `functools.partial` wrapper. Bound parameters are removed from the effective call signature and initialized as locals in the transformed frame. Read-only bound parameters can then be propagated as constants and can eliminate safe constant operations/branches.

### Important constraints

- at least one argument must be bound;
- partial binding of `*args`/`**kwargs` is rejected where removing names would make variadic capture ambiguous;
- transformations fail closed when a parameter cannot be safely represented in the rewritten frame;
- user-visible local reassignment remains valid after the bound-local initializer.

## 2. `specialize()`

```python
@specialize(
    constants={"mode": "fast"},
    types={"value": int},
    policy="always",
    max_variants=4,
    dispatch="auto",
)
def convert(value, mode="safe"):
    ...
```

`constants` constrains argument values. `types` uses **exact runtime type** constraints rather than subclass matching. The generated fast variant can simplify branches such as safe `type(x) is T` or ordinary builtin `isinstance(x, T)` checks when the surrounding global bindings prove that those operations have their ordinary semantics.

A guard miss always executes the original generic function.

Bare `@specialize` creates an initially generic dispatcher. Additional variants can be registered through the generated function's `register_specialization(...)` hook.

### Dispatch modes

- `dispatch="wrapper"` — keep the generic dispatcher as an explicit Python wrapper;
- `dispatch="inline"` — require an in-code guard for supported guard shapes;
- `dispatch="auto"` — use inline dispatch for eligible explicit ordinary-function specializations, otherwise wrapper dispatch.

Inline dispatch is intentionally restricted to guard forms whose behavior can be preserved without invoking arbitrary user code.

### Constant guards

Adaptive/explicit constants are canonicalized only for safe immutable shapes. Float and complex guards preserve bit-level distinctions required for values such as `+0.0` versus `-0.0` and avoid relying on arbitrary equality for NaN. Unsupported object constants retain identity/fallback semantics rather than executing user-defined equality inside the guard.

## 3. `hotpath()`

```python
@hotpath(
    threshold=64,
    max_variants=1,
    types=True,
    constants="auto",
    policy="speed",
    max_profiled_shapes=64,
    profile_budget=None,
    metrics=False,
    backend="auto",
)
def decode(value, mode):
    ...
```

`hotpath` discovers candidates from bytecode patterns such as argument-vs-literal comparisons and exact-type predicates. Profiling is deliberately bounded in two dimensions:

- `max_profiled_shapes` bounds retained shape diversity;
- `profile_budget` bounds total profiling work. When omitted, the implementation chooses a finite budget from the other settings.

Megamorphic call sites therefore cannot grow the profile table without bound or pay profiling overhead indefinitely. Least-observed shapes can be evicted during discovery. Once the budget is exhausted, profiling stops.

`policy="speed"` retains a promoted variant only when the static evaluator can remove enough work to justify dispatch overhead.

### Monitoring backend

On eligible ordinary one-variant functions, `backend="auto"` prefers CPython 3.13 `sys.monitoring` for the bounded warm-up. After promotion it can install a verified in-frame dispatcher and avoid keeping a permanent Python wrapper.

Wrapper fallback is used for cases such as:

- polymorphic/multi-variant promotion;
- coroutine functions;
- metrics-enabled dispatch;
- monitoring tool-slot conflicts;
- guard shapes not supported by the in-frame path.

Generator and async-generator specialization is currently rejected.

## 4. Metrics and introspection

Specialized wrappers expose:

- `specialization_stats`;
- `specialization_variants()`;
- `register_specialization(...)`;
- `__python_extensions_specialization__`;
- `__python_extensions_dispatch_mode__`.

`metrics=False` is the hot-path default for `hotpath()` because per-call hit/fallback counters themselves have measurable cost. Promotion/rejection/profile counts remain available without enabling detailed per-call accounting.

## 5. Safety and observable behavior

The evaluator is intentionally conservative around operations that can execute user code or whose meaning can be rebound:

- shadowed `type`/`isinstance` globals are not assumed to be builtins;
- custom metaclass `__instancecheck__` paths are not silently folded;
- arbitrary user equality is not used as a constant-guard shortcut;
- exceptions and invalid call signatures must match the unspecialized function;
- generic fallback is retained whenever a guard does not match.

The adversarial v1.21 qualification covers invalid calls, positional/keyword binding, `*args/**kwargs`, NaN/signed-zero/complex keys, side-effecting objects, concurrent promotion, megamorphic profiles, recursion/closures, tracing, descriptors, async functions, monitoring churn, and weak-reference cleanup.

## 6. Composition

`optimize_extensions()` applies the fixed order:

```text
switch -> partial -> inline -> goto -> specialize/hotpath
```

The order matters:

1. switch may recompile source and therefore runs first;
2. partial exposes constants/dead branches before interprocedural optimization;
3. inline sees the simplified body;
4. goto resolves pseudo labels after static code growth;
5. specialize/hotpath guard the final verified function.

`specialize` and `hotpath` are alternatives and cannot both be enabled in one composition call.

## 7. When to use which API

| Situation | Recommended API |
|---|---|
| One configuration is known permanently when you construct the function | `partial()` |
| You know one or more valuable constant/type shapes but need generic fallback | `specialize()` |
| The valuable shape depends on runtime traffic and should be discovered automatically | `hotpath()` |
| The helper call itself is the dominant overhead and the target is eligible | `inline_calls()` |

Do not layer adaptive specialization around code that is already dominated by I/O, large allocations, or expensive C-extension work without measuring the whole workload.
