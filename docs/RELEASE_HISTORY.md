# python_extensions 1.0.2

A focused **CPython 3.13** library containing exactly three language extensions:

- `python_extensions.switch` — adaptive switch lowering with configurable Python-vs-exact-type case identity.
- `python_extensions.inline` — profitability-aware bytecode inlining, including optional shared appended inline regions.
- `python_extensions.goto` — offset-preserving native jumps with strict control-flow validation by default.

## Production hardening (1.0.2)

Version 1.0.2 adds opt-in **guarded inline binding** for code that may mutate or rebind inline targets after decoration. Use `binding="guarded"` when preserving ordinary dynamic Python call semantics is more important than eliminating every target check; keep `binding="frozen"` for intentionally static hot paths. See [the comprehensive guide](COMPREHENSIVE_GUIDE.md) for decision tables, patterns, diagnostics, composition, and production deployment guidance.


## Production hardening (1.0.1)

1.0.1 hardens the inline registration lifecycle after a deep release audit. Registration, replacement, rollback, unregistration, and weak-reference cleanup now update callable-identity eligibility transactionally; failed decorators no longer leave hidden inlineability or extension metadata behind. Registry keys distinguish class methods and independently-created local/factory functions instead of colliding solely on a shared short function name.

The switch module also removes the dormant legacy native-`match` compiler that the semantics-safe production path never selected, reducing unreachable transformation surface. Release versioning now has one source of truth, and the package ships a `py.typed` marker. The distribution name is `cpython-extensions` while the import namespace remains `python_extensions`. Qualified switch markers and exact imported aliases are accepted as well, so both direct imports and forms such as `import python_extensions as pe; with pe.switch(...):` are valid production syntax without broad attribute-name matching.


## Production release (1.0.0)

1.0.0 promotes the CPython 3.13 extension suite to a stable production release. It retains the semantics-safe portable switch backend as the default, the verified strict goto backend, and the guarded bytecode inliner while adding release-level packaging and concurrent-decoration hardening. Source checkouts now run the test suite directly with `python -m pytest` under the `src/` layout, and inline registry/counter mutations are serialized without adding overhead to transformed hot paths.

The 1.0.0 release preserves 0.22.0 runtime semantics and defaults; the only version-policy change is promotion from Beta to Production/Stable. Explicit live/self-modifying switch modes and `goto(mode="unsafe")` remain opt-in research/escape-hatch modes and are not the production default.

## Coordinated refinement (0.22.0)

0.22.0 makes the 0.21 guarded-closure path profitability-aware instead of charging a fixed guard cost in isolation. `policy="speed"` can now inline a closure-held registered function when constant/default specialization removes enough body work to repay the identity check; the representative default-constant arithmetic helper measured **1.331x** versus 0.21.0, while the trivial closure control remains uninlined; its short-run control measured **1.097x**, which is treated as parity/noise because the generated path is unchanged. Guarded closure support also covers registered bound methods and positional-only `functools.partial` objects. Keyword-bearing partials and generic callable instances deliberately remain ordinary calls because their behavior may mutate without object identity changing.

`pyswitch compact_routes="auto"` now estimates duplicated continuation density by compiling the shared tail into a side-effect-free CPython bytecode proxy rather than counting AST nodes. Context-sensitive constructs such as `break`, `continue`, and `await` fail closed to the un-compacted layout. The representative low-AST/high-bytecode fixture that 0.21 missed now compacts from **322 to 238 code bytes**, with report telemetry for the number of auto-compacted plans and estimated bytes saved. `compact_routes=False` remains the speed-first default, and route-level timing is not presented as a universal speedup.

`pygoto` now retains explicit metadata for every synthetic native jump, verifies the patched code once, then proves each synthetic edge against that exact final CFG before generating its report. The proof checks the final native jump opcode/target, CFG terminator placement, jump successor, and strict-mode source exception-region semantics. Reusing that verified CFG makes decoration about **1.33x faster** in the seven-process benchmark while runtime bytecode and throughput remain effectively unchanged.

The exact release tree passes **343/343 package tests**, **59,871,408 portable full-harness calls/yields**, and **61,110,508 total** operations including explicit live compatibility. Full data is recorded in `PYTHON_EXTENSIONS_0.22.0_CERTIFICATION.txt` and `BENCHMARK_ALL_EXTENSIONS_V210_V220.json`.

## Coordinated refinement (0.21.0)

0.21.0 broadens safe composition without weakening speed-first defaults. The inliner can now optimize an exact registered function held in a caller closure cell behind a runtime identity guard. The callable is loaded exactly once: the guard keeps that object on the operand stack, enters the inline body only on identity match, and otherwise reuses the same object for the original CALL path. This preserves later `nonlocal` rebinding semantics. `policy="speed"` charges the full guard cost and therefore leaves trivial guarded calls ordinary; `policy="always"` exposes the guarded transformation for callers that benefit from whole-body optimization.

`pyswitch` adds `compact_routes="auto"` as an opt-in code-density heuristic. It hoists only source-location-identical shared continuations and only when the duplicated suffix is large enough to justify a join edge. `compact_routes=False` remains the production speed-first default. `pygoto` now explicitly verifies the patched CFG before wrapping the function and reports `synthetic_jumps_verified` alongside existing lowering telemetry.

The exact release tree passes 333 package tests and the coordinated/inherited full harness matrix reproduced 52,011,408 portable calls/yields plus 1,239,100 explicit switch live-mode calls (53,250,508 total). The final seven-process control benchmark intentionally makes no guarded-inline speed claim: the medium guarded fixture measured 0.982x versus the ordinary closure call, while `policy="speed"` left the trivial guarded call uninlined and measured 1.030x control parity. Default portable switch and goto controls measured 1.035x and 1.021x respectively in the same noisy short-run matrix; their generated runtime paths are not changed by this release.

## Coordinated refinement (0.20.0)

0.20.0 keeps the speed-first production defaults established by 0.19.0 and adds targeted improvements across all three extension families. `pyswitch` gains an explicit `compact_routes=True` code-density mode for source-identical fallthrough continuations; it is opt-in because sharing a route tail can exchange a small amount of branch throughput for substantially smaller code/exception tables. The default portable switch layout is unchanged.

The inliner now folds exact-builtin unary/binary/comparison chains to a fixed point. This catches operations exposed only after another fold, including the CPython 3.13 `LOAD_CONST; UNARY_NOT` shape produced after comparison folding, without ever invoking user protocols at decoration time. On the release benchmark, the nested constant-chain caller shrinks from 18 to 12 code bytes and runs about 1.48x faster than 0.19.0.

`pygoto` strengthens strict mode by proving that the complete pseudo-statement span used for early native jump placement stays in one semantic exception-handler stack. New report fields expose inserted `EXTENDED_ARG` units and pseudo-marker units elided; equivalent patched runtime `co_code` remains identical to 0.19.0.

The exact 0.20.0 source tree passes **325/325 package tests**, **46,311,408 portable stress/differential calls**, and **47,550,508 total full-harness calls** including explicit switch live compatibility. The reproduced 13-process 0.19.0 -> 0.20.0 benchmark measures the fixed-point inline fixture at **1.491x faster** with code shrinking **18 -> 12 bytes**. The opt-in compact switch fixture shrinks **610 -> 382 bytes** (~37% fewer code bytes); route timing is intentionally not advertised as a speedup because one branch position measured ~3.4% slower, which is why compaction is not the default. Full data is recorded in `PYTHON_EXTENSIONS_0.20.0_CERTIFICATION.txt` and `BENCHMARK_ALL_EXTENSIONS_V190_V200.json`.

## Three-extension production refinement (0.19.0)

0.19.0 is the first coordinated hardening/performance release across all three extension families rather than a `pyswitch`-only optimizer step.  The canonical composition order remains **switch -> inline -> goto**, and `optimize_extensions` now handles descriptor-wrapped static/class methods consistently through final bytecode verification.

### `pyswitch`

Control-heavy portable plans that collapse to exactly two semantic routes now use a boolean table payload and one truth branch instead of the general integer balanced-route comparison.  The optimization keeps dictionary hash/equality behavior, caller-frame execution, unhashable/default semantics, typed-key identity and conservative complex-body lowering unchanged.  Optional CPython-3.13 `bytecode` round trips are now verified before acceptance and fail closed to the original valid compiler output if an exception-table/stack rewrite is not trustworthy.

### `inline_function` / `inline_calls`

Both decorators now accept `staticmethod` and `classmethod` descriptors in either practical decorator order, registry removal also normalizes bound classmethods, and rebuilt functions own a copy of `__kwdefaults__`.  Constant/default propagation now folds side-effect-free exact-builtin unary operations (`-`, `+`, `~`, and `not`); arbitrary user unary/truth methods are never invoked at decoration time.  `InlineStats` and transformation reports expose `constant_unary_ops_folded`.

### `pygoto`

Goto/label markers retain their original bytecode footprint so unrelated offsets and exception tables stay stable, but executed marker overhead is dramatically lower: a taken goto places `EXTENDED_ARG* + JUMP_*` at the earliest valid code units, and label fallthrough uses one jump over the dead pseudo-expression.  Strict validation now compares the semantic exception-handler stack `(target, depth, lasti)` rather than incidental physical table ranges, allowing safe coroutine/async-generator jumps across compiler-created `await`/`yield` splits while still rejecting real `try`/`finally`/handler-stack crossings.  The final code is bytecode-verified, invalid stack/control flow raises public `GotoControlFlowError`, descriptor decoration is supported, marker-free decoration is an identity-preserving no-op, and the complete goto error hierarchy is exported from `python_extensions`.

The exact 0.19.0 release tree is certified on CPython 3.13.5 by **315/315 package tests**, **40,687,408 portable stress/differential calls**, and **41,926,508 total full-harness calls** including explicit live-mode compatibility. The final alternating seven-process benchmark versus 0.18.5 measured taken backward/forward goto at **2.821x / 2.766x**, goto label fallthrough **1.765x**, constant unary inline folds from **1.161x to 1.355x**, and the new switch two-route fallback from **1.054x to 1.171x**, while dynamic-inline and direct-switch controls stayed near parity. See `PYTHON_EXTENSIONS_0.19.0_CERTIFICATION.txt` and `BENCHMARK_ALL_EXTENSIONS_V185_V190.json` for the complete reproduced data.


## `pyswitch` multi-type allocation-free typed routing (0.18.5)

0.18.5 extends the 0.18.4 exact-type fast lane from one case type to mixed typed switches without restoring per-call `(type(subject), subject)` allocation.  When every case-key type object uses the ordinary `type` metaclass, the compiler builds one raw subject dictionary per exact type.  A multi-type plan then installs a bound type-router dictionary whose values are those per-type `dict.get` callables.  Runtime dispatch is `router.get(type(subject), empty.get)(subject, default)`: one exact runtime-type lookup followed by one ordinary subject lookup, with no helper frame and no generated routing local.

The original single-type 0.18.4 `type(subject) is T` path is retained byte-for-byte.  A router miss selects a bound empty-dictionary getter, which still executes the subject's real hash once; intrinsic unhashables therefore remain misses and genuine user `__hash__` failures still propagate.  Exact-type partitioning prevents subject equality from crossing type boundaries even when values deliberately share hashes.  If any case-key type has a custom metaclass, the complete plan remains on the historical tuple-key backend.  Public diagnostics now include `__pyswitch_typed_router_plan_count__` / `__pyswitch_typed_router_type_count__` and the corresponding optimization-report fields.

Certification on CPython 3.13.5: **290/290 package tests**, **4,830,003** new multi-router calls, **4,760,005** inherited typed-partition calls, **2,391,800** scheduler calls, **6,450,800** stack-payload calls, **4,703,400** adversarial calls, **3,680,000** production-switch calls, and **1,239,100** explicit live-mode calls.  That is **26,816,008 portable** and **28,055,108 total** full-harness calls.  The final interleaved seven-process benchmark versus 0.18.4 measured 2-type mixed dispatch **1.105x**, 4-type **1.131x**, 8-type **1.131x**, mixed-type expression templates **1.063x**, statement templates **1.066x**, and guarded balanced typed routing **1.024x**.  The unchanged single-type typed path remained near parity/slightly positive (**1.032x** hit, **1.014x** miss), while ordinary Python-key direct dispatch measured **1.039x** in this sample.  Unknown-type miss timing is intentionally not used as a release claim because it is more noise-sensitive; a separate 13-process, 700k-call-per-child 4-type miss check measured **1.018x** in favor of 0.18.5.  Absolute timings are machine/build specific.

## `pyswitch` allocation-free exact-type routing (0.18.4)

0.18.4 removes the largest remaining portable typed-dispatch tax without changing the production `auto` contract.  When every case key in one typed portable plan has the same exact runtime type and that type object uses the ordinary `type` metaclass, `pyswitch` proves a single exact-type partition and dispatches matching subjects through a raw per-type dictionary.  This avoids constructing `(type(subject), subject)` on every hit while retaining the subject dictionary's normal hash/equality behavior and exception propagation.

Exact-type mismatches cannot be equal under `case_key_mode="typed"`, so a partitioned miss executes the subject's real `hash()` exactly once and goes directly to the default without equality or tuple allocation.  Intrinsically unhashable subjects remain ordinary misses; genuine user `__hash__` failures propagate.  Mixed-type case plans and case types with custom metaclasses deliberately retain the conservative tuple-key backend.  Partition preparation is deferred through the existing finalization/canonicalization pass, so the optimization adds no extra compile-time case-key hash/equality pass.  New telemetry is exposed as `__pyswitch_typed_partition_plan_count__` and `__pyswitch_typed_partition_type_count__` and is included in optimization reports.

Certification on CPython 3.13.5: **278/278 package tests**, **4,760,005** new typed-partition calls, **2,391,800** inherited scheduler calls, **6,450,800** inherited stack-payload calls, **4,703,400** inherited adversarial calls, **3,680,000** inherited production-switch calls, and **1,239,100** explicit live-mode calls.  That is **21,986,005 portable** and **23,225,105 total** full-harness calls.  The final seven-process focused 0.18.3 -> 0.18.4 benchmark measured typed literal dispatch **1.179x faster**, stack-resident typed expression templates **1.116x**, typed statement templates **1.082x**, and guarded balanced typed routing **1.045x**.  The final five-process broad matrix measured all-int hit-only literal dispatch **1.154x**, mixed hit/miss literal traffic **1.112x**, 64-case typed literal dispatch **1.130x**, typed strings **1.138x**, expression templates **1.084x**, statement templates **1.086x**, and guarded balanced routing **1.064x**.  Mixed-type typed plans stayed near parity (**1.009x**) because they are intentionally left on the conservative tuple-key route.  These absolute timings are machine/build specific.

## `pyswitch` depth-aware stack-carrier scheduling (0.18.3)

0.18.3 recovers much of the small throughput cost introduced by 0.18.2 frame-transparent stack payloads without reintroducing hidden fast locals.  The CPython 3.13 portable template rewriter now schedules each payload against its **dynamic carrier depth** and consumes a carrier at its final use only when doing so is a strict instruction-count win.  Depth-one uses need no load opcode; depth-two uses become one `SWAP 2`; and a sole return carrier at depth three may use `SWAP 2; SWAP 3` because that also removes the terminal `SWAP`/`POP_TOP` cleanup.  Deeper call-argument carriers deliberately retain the proven `COPY` path.

The scheduler is consumer-agnostic: arithmetic, comparisons, tuple/list construction, indexing, f-strings, and shallow call shapes all use the same stack rotation rule.  It also understands payload components split out of CPython 3.13 `LOAD_FAST_LOAD_FAST` instructions.  When all case bodies are structurally identical and the synthetic identity payload is never read, the lookup result is discarded once at the join rather than carried through user code to the return.  Dictionary lookup still executes, so custom hash/equality behavior remains observable.

Certification on CPython 3.13.5: **267/267 package tests**, **2,391,800** new scheduler differential/stress calls, **6,450,800** inherited stack-payload calls, **4,703,400** inherited adversarial calls, **3,680,000** inherited production-switch calls, and **1,239,100** explicit live-mode compatibility calls.  That is **17,226,000 portable** and **18,465,100 total** full-harness calls.  The seven-process focused 0.18.2 -> 0.18.3 benchmark measured 64-case right-literal arithmetic **1.076x faster**, left-literal arithmetic **1.073x**, multi-payload arithmetic **1.020x**, and straight-line statement templates **1.024x**, while the unchanged direct-value path remained near parity (**1.008x**).  A broader five-process run also measured tuple-left **1.041x**, tuple-right **1.026x**, one-argument call templates **1.035x**, identical templates **1.057x**, and deep two-argument calls **0.998x** (intentionally unchanged).

See `BENCHMARK_SWITCH_V182_V183_SCHEDULER.json`, `BENCHMARK_SWITCH_V182_V183_CORE.json`, and `tests/harness_switch_stack_scheduler_v183.py`.

## `pyswitch` stack-resident payloads and tooling hardening (0.18.2)

0.18.2 removes the last common transient payload fast-locals from CPython 3.13 portable expression/statement templates. Selected literal payloads are carried on the operand stack, including multi-payload shapes such as `x * K + C`; former payload loads become depth-correct stack copies, and normal returns use one deep `SWAP` plus the required `POP_TOP` cleanup so CPython 3.13's empty-stack-on-return invariant remains satisfied. The transform fails closed outside CPython 3.13 or for unsupported control-flow/suspension shapes, retaining the proven 0.18.1 fast-local template rather than applying version-sensitive bytecode rewriting.

Statement-template unification is now explicitly straight-line: real exception/control-flow statements such as `try`, loops, and nested branching are routed to the conservative balanced backend. Synthetic source locations that precede the function's physical first line are normalized after compilation, and isolated/thread-local/per-call wrapper line tables are relocated to the original function's source range. This removes bogus line-0/pre-function tracing events from both portable and explicit live backends. Shared portable O(1) template bodies still compile multiple routes into one canonical code region, so route-specific case-body line attribution is not guaranteed for those shared regions; the guarantee is that generated locations do not escape before the function's real source range.

Explicit live modes now match portable unhashable semantics in both Python and exact-type key modes: an intrinsically unhashable subject (`__hash__ = None`) is a miss/default, while a `TypeError` genuinely raised by user `__hash__`/`__eq__` still propagates. The common live lookup remains the bound C-level `dict.get`; the subject is evaluated exactly once and CPython's zero-cost exception machinery handles only the exceptional unhashable route. Compiler subject/gate temporaries are deleted before selected user code executes.

Certification on CPython 3.13.5: **262/262 package tests**, **6,450,800** stack-payload stress calls, **4,703,400** inherited adversarial calls, **3,680,000** inherited production-switch calls, and **1,239,100** explicit live-mode compatibility calls. That is **14,834,200 portable stress calls** and **16,073,300 total harness calls** across the four full suites. The reproduced five-process 0.18.1 -> 0.18.2 benchmark measured the 64-case production direct path at **70.41 ns/call** (direct `dict.get` reference: **71.29 ns**), stack-resident expression templates at **85.75 ns/call** (~3.2% slower than 0.18.1 in this run), multi-payload templates at **106.50 ns/call** (~1.0% slower), statement templates at **118.21 ns/call** (~1.6% slower), and typed live dispatch about **1.154x faster**. The semantics-corrected untyped experimental live path is about 8.7% slower in this run because it preserves evaluate-once unhashable recovery; ordinary portable `auto` remains the recommended production backend. The small portable costs buy frame-local transparency and remain direct-table-class/O(1) rather than scaling with case count.

See `BENCHMARK_SWITCH_V181_V182_STACK_PAYLOAD.json`, `tests/harness_switch_stack_payload_v182.py`, and `tests/harness_switch_live_v182.py`.

## `pyswitch` production hardening (0.18.1)

0.18.1 is a correctness and stress-hardening patch over the 0.18 portable compiler. General guarded/heterogeneous routes now delete compiler dispatch temporaries before entering any user guard or case/default body, and specialized assignment paths clean their temporary lifetimes after the switch. Complex subjects can reuse a simple user `as name` alias as their dispatch storage, avoiding an extra hidden local and an extra copy. Direct literal assignment to a normal local uses the lookup result directly; descriptor, subscript, and destructuring assignments retain the staged exception-safe path.

Nested recursive functions that close over their own name now receive a private transformed self-cell. Recursive calls therefore remain on the transformed portable function (and on the isolation wrapper for `isolated`/`per_call`) without mutating the original function's closure cell. The private cycle remains normal GC-managed Python state and is collectable. Invalid `fallthrough()` placement is also rejected at decoration time: the marker is valid only as the final direct statement of a case body, while nested switches own and validate their own markers independently.

The hardening suite passes **232/232** package tests. A new full adversarial harness passes **4,703,400** calls across a 1,024-case direct table, 256 guarded route keys, forced hash-collision keys, 200 exact-type mixed keys, fallthrough, three-level nested switches, 16-thread shared portable dispatch, async concurrency, deep recursive closure calls above the isolated clone cache, generated random switches, and decoration/GC churn. The inherited 0.18 production harness independently passes another **3,680,000** calls, and the explicit live-mode compatibility harness remains green.

Scale checks on CPython 3.13.5 successfully construct and dispatch a **4,096-case** direct switch. Direct-table timings remain roughly flat (about 81-101 ns/call from 1 through 4,096 cases in the local run); guarded balanced dispatch grows logarithmically (about 160 ns at 4 keys to 400 ns at 1,024 keys). Deliberately identical custom hashes degrade like an ordinary Python dictionary, as required to preserve Python hash/equality semantics rather than inventing a non-dict fallback. See `PYSWITCH_SCALE_STRESS_V181.json`, `BENCHMARK_SWITCH_V18_V181_HARDENING.json`, and `tests/harness_switch_adversarial_v181.py`.

## Production portable `pyswitch` compiler (0.18)

0.18 temporarily concentrates the release on `pyswitch`. The default `@enable_switch` / `mode="auto"` path is now a production-oriented, non-self-modifying compiler for CPython 3.13: it preserves dictionary-style hash/equality dispatch, keeps selected user code in the original Python frame, is recursion/thread/suspension safe, and never depends on executable-memory mutation. `case_key_mode="typed"` is available when exact runtime type must participate in identity, so `1`, `1.0`, and `True` can be distinct cases.

The portable compiler chooses only semantics-preserving lowerings:

- **direct-value** — literal return/assignment routes compile to one bound `dict.get` on the normal hot path; payloads are canonicalized to the generated function's actual `co_consts` objects so constant identity remains CPython-consistent;
- **expression-template** — same-shape expressions such as `return value + K` use an O(1) payload lookup. A private miss sentinel now allows a structurally different or absent default without falling back to a route tree;
- **statement-template** — same-shape straight-line multi-statement case bodies share one inline body with literal payload slots, preserving caller-frame execution while making route selection average O(1);
- **balanced** — guards, fallthrough, and heterogeneous/control-heavy bodies use one exact dictionary lookup followed by a balanced O(log r) inline route tree, where `r` is the number of distinct route bodies.

Native `match` lowering is intentionally not selected by the production compiler because `match` equality semantics can disagree with hash-table semantics for custom and unhashable subjects. The former out-of-line return-handler optimization is also excluded because a helper frame changes caller-frame introspection/tracing behavior and can misclassify user exceptions. Intrinsically unhashable subjects are treated as misses without invoking custom metaclass attribute hooks; a `TypeError` genuinely raised by user `__hash__`/`__eq__` code propagates.

```python
from python_extensions import case, enable_switch, fallthrough, switch

@enable_switch                      # auto == production portable by default
def classify(kind):
    with switch(kind):
        if case("read", "peek"):
            return 1
        if case("write"):
            return 2
        if case():
            return 0

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

`mode="fast"`, `"thread_local"`, `"isolated"`, and `"per_call"` remain explicit CPython-3.13 live-bytecode research backends. They are never selected by ordinary `auto`; passing `live_threshold=` is an explicit opt-in. Production/general-purpose code should leave that option unset unless raw bytecode mutation is deliberately required.

### 0.17 -> 0.18 switch benchmark

Five isolated CPython 3.13.5 processes, median of each process's best repeated timing:

| Scenario | 0.17 | 0.18 | Speedup | 0.18 backend |
|---|---:|---:|---:|---|
| 64-case literal return | 110.23 ns | **108.59 ns** | **1.015x** | direct-value |
| 16-case exact-type literal | 216.75 ns | **161.67 ns** | **1.341x** | direct-value |
| 64-case same-shape expression + same-shape default | 128.51 ns | **128.51 ns** | ~1.00x | expression-template |
| 64-case same-shape expression + different default | 163.79 ns | **138.71 ns** | **1.181x** | expression-template |
| 64-case three-statement same-shape body | 281.77 ns | **161.26 ns** | **1.747x** | statement-template |
| 64-case heterogeneous fallback | 267.99 ns | 270.31 ns | ~0.99x | balanced |

In the same harness the 64-case literal path measured 116.01 ns for an equivalent direct-dict wrapper, while 64-way `if/elif` and `match` measured roughly 0.91–0.99 microseconds for the large route scenarios. These figures are machine/build-specific; the important property is that the optimized table paths stay essentially flat as case count grows.

### 0.18 switch certification

The complete package suite passes **205/205** tests. The dedicated production-switch full harness passed **3,680,000** differential/stress calls spanning direct-value, expression-template, statement-template, exact-type keys, guarded duplicates, fallthrough, generated random switch tables, intrinsically unhashable subjects, and eight-thread concurrent execution. Focused regressions cover constant identity, user `TypeError` propagation, descriptor assignment failures, closures, recursion, generators, coroutines, async generators, nested lexical functions, decorator composition, source injection, and runtime-name hygiene.

See `BENCHMARK_SWITCH_V17_V18_PRODUCTION.json` and `tests/harness_switch_production_v18.py`.

Version 0.17 adds path-sensitive lazy affine materialization on top of 0.16's dominance-aware recurrence strength reduction. When globally maintaining a secondary induction value would penalize a loop path that never consumes it, the speed policy can now synchronize only on the affine path: the first exact-int affine expression snapshots its result and later same-version uses load that snapshot. Re-entering the path recomputes from the current induction value, so changing branch decisions stay correct while cold/non-affine paths execute no derived-update work. The existing calls, exception regions, unresolved goto control flow, ambiguous induction writes, dynamic scales, and profitability barriers remain conservative.

## Path-sensitive lazy affine materialization (0.17)

0.16 deliberately rejected speed-mode recurrence maintenance when any loop path could pay the derived update without eliminating enough affine work. That remains the correct choice for a globally maintained derived recurrence, but it leaves profitable partial redundancy on the table when the affine path itself repeats the same expression. 0.17 adds a fallback that is local to the consuming path.

For example:

```python
@inline_calls(region_dataflow=True, policy="speed")
def pipeline(count, use_affine):
    i = 0
    total = 0
    while count > 0:
        if use_affine:
            total += i * 7 + 3
            total += i * 7 + 3
        else:
            total += 1
        i += 2
        count -= 1
    return total
```

0.16 keeps both affine expressions because the `else` path reaches `i += 2` without consuming the derived value. 0.17 leaves the first affine expression in place, snapshots its already-computed exact-int result with `COPY 1; STORE_FAST`, and replaces the second expression with a fast-local load. The `else` path does not execute the snapshot instructions and no secondary-induction update is added.

Lazy values are tied to one induction-value version. If a basic block contains both pre-update and post-update uses, the unique induction write splits them into separate cache segments. Re-entering an affine block on a later iteration always materializes again from the current `i`, so branches that alternate between affine and non-affine paths cannot observe stale state.

The speed profitability model charges two instructions for `COPY`/`STORE_FAST` and requires later eliminated affine work to beat that cost before the existing final byte-size gate. Consequently, two multiply-only expressions are left alone at break-even, while three multiply-only expressions can use the lazy cache. `policy="always"` may accept an IR break-even when CPython 3.13 superinstruction compaction can still improve density.

New diagnostics are `cfg_strength_lazy_values`, `cfg_strength_lazy_uses`, and `cfg_strength_lazy_materializations`. The existing `cfg_strength_reduced_values` and `cfg_strength_reduced_uses` remain aggregate counters and therefore include lazy reductions; `cfg_strength_reduction_updates` counts only globally maintained derived-recurrence update sites.

### 0.16 -> 0.17 lazy-strength benchmark

Five isolated CPython 3.13.5 processes, median of the best timing from three repeats per process:

| Scenario | 0.16 | 0.17 | Speedup | Code |
|---|---:|---:|---:|---:|
| Rare affine pair, cold path | 1696.03 ns | 1706.46 ns | ~1.00x | 132 -> **124 B** |
| Rare affine pair, hot path | 3380.12 ns | **3103.13 ns** | **1.089x** | 132 -> **124 B** |
| Changing affine/non-affine path | 3351.94 ns | **3173.51 ns** | **1.056x** | 136 -> **128 B** |
| Single-use control | 1696.24 ns | 1702.03 ns | ~1.00x | 110 -> 110 B |
| Existing global recurrence | 2281.41 ns | 2301.44 ns | ~1.00x | 110 -> 110 B |
| Pre/post affine pair, cold path | 1860.74 ns | 1869.39 ns | ~1.00x | 190 -> **174 B** |
| Pre/post affine pair, hot path | 6067.03 ns | **5049.58 ns** | **1.201x** | 190 -> **174 B** |

See `BENCHMARK_V16_V17_LAZY_STRENGTH.txt`.

### 0.17 certification

The focused package suite passes **165/165** tests. The new lazy-strength full-profile harness passed 300,000 generated changing-path differential calls, 200,000 generated pre/post-version calls, 800,000 threaded calls, and 2,000,000 crash-isolated calls. The inherited 0.16 dominance-strength and 0.15 strength-reduction full harnesses each independently passed another 3.3 million calls. Historical suites that intentionally manipulate the global inline registry remain isolated when run as standalone certification harnesses.

## Dominance-aware recurrence strength reduction (0.16)

0.15 required all reduced affine uses to live in the same basic block before the unique induction update. 0.16 removes that restriction for reducible structured loops. The normal-edge CFG now computes dominators and verifies that the natural-loop header dominates the induction update and every rewritten use. Because the derived recurrence is initialized before the header label and updated immediately after the exact induction write, it stays synchronized even when the induction write is conditional or when a use appears after that write.

For example:

```python
@inline_calls(region_dataflow=True, policy="speed")
def pipeline(count, left):
    i = 0
    total = 0
    while count > 0:
        if left:
            total += i * 7 + 3
        else:
            total += i * 7 + 3
        i += 2
        count -= 1
    return total
```

now uses one derived recurrence across both branch arms. The same derived local also handles:

```python
total += i * 7 + 3
i += 2
total += i * 7 + 3
```

The second use observes the already-updated derived value, mirroring the updated `i`.

The default speed policy does not simply add the static savings from mutually-exclusive branch arms. It computes the minimum affine-expression savings on every loop path that reaches the induction update, and also credits guaranteed work after the update through the current iteration boundary. A branch where the update can execute along a path with no reduced affine work is rejected. This avoids turning code-density wins into hot-path regressions.

Under `policy="always"`, the optimizer may still choose a structurally smaller cross-block reduction even when that per-update runtime proof is unavailable. Final bytecode verification and the existing final-size profitability check still apply.

### 0.15 -> 0.16 dominance benchmark

Five isolated CPython 3.13.5 processes:

| Scenario | 0.15 | 0.16 | Speedup | Code |
|---|---:|---:|---:|---:|
| Branch uses before update | 1163.13 ns | **1105.61 ns** | **1.052x** | 122 -> **110 B** |
| Branch uses after update | 1156.53 ns | **1095.30 ns** | **1.056x** | 122 -> **110 B** |
| Use before + after update | 1504.83 ns | **1205.62 ns** | **1.248x** | 106 -> **94 B** |
| Rare-path control | 838.79 ns | 834.57 ns | ~1.00x | 110 -> 110 B |

The rare-path control receives no strength-reduction rewrite. See `BENCHMARK_V15_V16_DOMINANCE_STRENGTH.txt`.

### 0.16 certification

The dominance-focused suite covers branch-distributed uses, post-update uses, conditional induction writes, updates confined to one branch, minimum-path profitability rejection, and density-policy opt-in. Its full harness passed 300,000 generated branch differential calls, 200,000 early-exit controls, 800,000 threaded calls, and 2,000,000 crash-isolated calls. The inherited 0.15 strength-reduction and 0.14 recurrence harnesses each independently passed another 3.3 million full-profile calls. Historical focused suites are certified in isolated processes because older modules intentionally clear the global inline registry.

## Recurrence strength reduction (0.15)

0.14 could prove properties of an affine recurrence but still recomputed affine expressions from scratch on every iteration. 0.15 derives a secondary induction value for repeated exact-int expressions of the form `i * SCALE [+/- OFFSET]` and `SCALE * i [+/- OFFSET]`.

For a recurrence `i_0 = start`, `i_{n+1} = i_n + step`, the optimizer derives:

```text
d_0     = start * SCALE + OFFSET
d_{n+1} = d_n + step * SCALE
```

A loop such as:

```python
@inline_calls
def pipeline(count):
    i = 0
    total = 0
    while count > 0:
        total += i * 7 + 3
        total += i * 7 + 3
        i += 2
        count -= 1
    return total
```

can therefore load the same derived value twice and update it by `14` once per induction update, removing both multiplies and both offset additions from the hot loop body. The initialization is inserted immediately before the original loop-body label. The first fallthrough executes it once; the existing backedge still targets the label itself and skips reinitialization.

CPython 3.13's `STORE_FAST_LOAD_FAST` form is used when the original induction store permits it, combining `STORE_FAST i` with the first derived-value load. Multiple derived recurrences sharing one induction update are supported and their synthetic update chain is compacted with the same paired-local superinstruction where possible.

The 0.15 implementation was deliberately dominance-conservative: all rewritten uses had to occur in the same basic block before the unique induction update. 0.16 removes that particular restriction; the rest of the safety boundaries described here remain relevant.

New diagnostics are `cfg_strength_reduced_values`, `cfg_strength_reduced_uses`, and `cfg_strength_reduction_updates`.

### 0.14 -> 0.15 strength-reduction benchmark

Five isolated CPython 3.13.5 processes:

| Scenario | 0.14 | 0.15 | Speedup | Code |
|---|---:|---:|---:|---:|
| Two `i*7+3` uses | 871.68 ns | **730.89 ns** | **1.193x** | 106 -> **94 B** |
| Four `i*5` uses | 1129.20 ns | **1025.85 ns** | **1.101x** | 126 -> **114 B** |
| Two derived affine values | 1445.61 ns | **1138.42 ns** | **1.270x** | 142 -> **118 B** |
| Single-use control | 632.88 ns | 627.62 ns | ~1.00x | 84 -> 84 B |

The single-use control is structurally unchanged. See `BENCHMARK_V14_V15_STRENGTH_REDUCTION.txt`.

### 0.15 certification

The new strength-reduction suite covers right/left constant multiplication, offsets, negative scales, decreasing recurrences, multiple derived recurrences, zero-iteration behavior, dynamic-scale controls, and uses after the induction update. The dedicated full-profile harness passed 300,000 generated affine differential calls, 200,000 dynamic-scale control calls, 800,000 threaded calls, and 2,000,000 crash-isolated calls. Inherited recurrence, loop-aware CFG, CFG-region, whole-region, and fusion suites are certified in isolated processes to avoid historical registry/test-order contamination between old test modules.

## Affine loop recurrence analysis (0.14)

0.13 could preserve values that were invariant around a backedge, but a real induction variable such as `i += 2` correctly became unknown because its concrete value changes every iteration. 0.14 adds a second symbolic loop solve for a deliberately narrow, semantics-safe class: an exact integer preheader value with one static `x = x +/- constant` or `x +=/-= constant` update in a reducible natural loop.

The optimizer records an abstract recurrence `(start, step)` rather than pretending the induction variable has one concrete value. Only properties true for every reachable recurrence value are rewritten. Current folds include:

- modulo residue when `step % modulus == 0`;
- low-bit masks `x & (2^k-1)` when the step preserves those bits;
- monotonic lower/upper bounds;
- equality/inequality values the affine progression can never reach;
- copies of the same recurrence fact through caller fast locals.

For example:

```python
@inline_function(register_only=True)
def choose(i, value):
    if i % 2 == 0:
        return value + 7
    return value - 11

@inline_calls
def pipeline(value, count):
    i = 0
    total = 0
    while count > 0:
        total += choose(i, value)
        i += 2
        count -= 1
    return total
```

Every reachable `i` is congruent to zero modulo two, so the inlined modulo/comparison/branch is removed from the loop body. This is a recurrence proof, not first-iteration constant propagation.

The recurrence detector is intentionally strict. It requires an exact `int` start, a constant integer step, one static induction write, and no remaining Python call, exception marker, or unresolved goto/label pseudo-edge in the natural loop. Dynamic steps and multiple writes remain untouched. Conditional execution of the single update is allowed because skipping an update preserves congruence and monotonicity.

New diagnostics are `cfg_affine_recurrences` and `cfg_recurrence_folds`.

### 0.13 -> 0.14 recurrence benchmark

Five isolated CPython 3.13.5 processes:

| Scenario | 0.13 | 0.14 | Speedup | Code |
|---|---:|---:|---:|---:|
| Parity recurrence | 780.06 ns | **553.16 ns** | **1.410x** | 106 -> **78 B** |
| Low-bit recurrence | 782.51 ns | **551.03 ns** | **1.420x** | 106 -> **78 B** |
| Monotonic bound | 675.04 ns | **549.66 ns** | **1.228x** | 100 -> **78 B** |
| Dynamic-step control | 801.02 ns | 804.91 ns | ~1.00x | 104 -> 104 B |

The dynamic-step control is structurally unchanged. See `BENCHMARK_V13_V14_RECURRENCE.txt`.

### 0.14 certification

The focused package suite passes **140/140** tests. The dedicated recurrence harness passed 300,000 generated affine differential calls, 200,000 dynamic-step control calls, 800,000 threaded calls, and 2,000,000 crash-isolated calls. The inherited 0.13 loop-aware and 0.12 CFG harnesses each independently passed another 3.3 million full-profile transformed calls. The standalone switch v16 suite remains **34/34** green.

## Loop-aware CFG value propagation (0.13)

0.12 deliberately injected an empty abstract state on every backward edge. That made loops safe but prevented facts such as a constant configuration flag or an inlined result computed before the loop from reaching inline consumers inside later iterations.

0.13 recognizes reducible natural loops and solves the must-state across their backedges. A loop header is initially seeded from its forward/preheader predecessors; once a latch has been analyzed, its exit state joins the header like every other predecessor. Before that join, a dynamic token whose defining instruction belongs to the natural loop is removed because the same static instruction creates a new runtime value on each iteration. Exact constants and dynamic tokens defined outside the loop may survive when the body preserves them.

For example:

```python
@inline_calls
def pipeline(value, count):
    produced = flag()       # inlined -> True
    alias = produced
    total = 0
    while count > 0:
        total += choose(alias, value)
        count -= 1
    return total
```

In 0.12 the loop backedge killed `alias=True`, so the inlined `choose()` truth test remained in every iteration. 0.13 proves the value is loop-invariant and removes the branch. If the body instead does `alias = False`, or recomputes an inlined dynamic result inside the loop, the header fact becomes unknown and no unsafe substitution occurs.

The same analysis handles structured `for` loops, multiple `continue` latches, nested loops, same-value loop-carried copies, and zero-iteration exit paths. Remaining calls still clear all facts because CPython 3.13 permits write-through caller-local mutation through `frame.f_locals`; exception markers remain barriers; unresolved goto/label pseudo operations remain barriers until the later goto pass installs their real edges.

New `InlineStats`/report fields are `cfg_loop_headers`, `cfg_loop_invariant_facts`, and `cfg_loop_variant_kills`.

### 0.12 -> 0.13 loop-dataflow benchmark

Five-process isolated CPython 3.13.5 medians:

| Scenario | 0.12 | 0.13 | Speedup | Code |
|---|---:|---:|---:|---:|
| Invariant `while` | 382.41 ns | **306.00 ns** | **1.250x** | 96 -> **72 B** |
| Invariant `for` | 232.66 ns | **205.85 ns** | **1.130x** | 76 -> **52 B** |
| Changing-value control | 398.25 ns | 385.69 ns | 1.033x | 100 -> 100 B |

The changing-value control is structurally unchanged; the measured timing difference is normal process/specialization noise rather than an optimizer rewrite. See `BENCHMARK_V12_V13_LOOP_DATAFLOW.txt`.

### 0.13 certification

The focused package suite passes **129/129** tests. The dedicated loop-dataflow harness passed 300,000 generated constant-loop differential calls, 200,000 generated dynamic-version loop calls, 800,000 threaded calls, and 2,000,000 crash-isolated calls. The inherited 0.12 CFG and 0.11 whole-region harnesses each independently passed another 3.3 million transformed calls, and the 0.10 fusion harness passed 3.8 million randomized/threaded/crash-isolated calls.

The final wheel was install-tested without the inline dependency for base/switch/goto use and with `bytecode 0.17.0` for the full loop-aware inline subsystem. The extracted final sdist independently passes **129/129** tests.

## CFG-wide branch-merge dataflow (0.12)

After the 0.11 straight-line region pass, 0.12 builds a lightweight CFG over the transformed `bytecode` IR. Each local carries an abstract value version. A merge retains a fact only when every incoming forward edge proves the same value. Exact constants preserve exact type identity, so equality aliases such as `1` and `True` are not conflated by the optimizer. Dynamic results produced by an inlined call receive stable SSA-like version tokens; copies on separate branches can therefore rejoin when they originate from the same pre-branch result.

For example:

```python
@inline_calls
def pipeline(x, choose_left):
    root = flag()       # inlined -> True
    if choose_left:
        alias = root
    else:
        alias = root
    return choose(alias, x)
```

0.11 stops the fact at the first control-flow edge. 0.12 proves `alias=True` at the join and folds the inlined `choose` branch while preserving both branch-local stores. If the incoming values differ, the merge becomes unknown and the downstream branch is left intact.

The CFG pass is itself fixed-point. If round one folds a branch and removes an incoming path, round two rebuilds the CFG and may discover a new equal-value join farther downstream. Under `policy="speed"`, a CFG rewrite is retained only when it creates an objective structural gain such as code shrinkage, branch/comparison/arithmetic folding, dead-code pruning, or jump removal.

Safety boundaries are deliberately strict: remaining Python calls clear all facts because CPython 3.13 `frame.f_locals` is write-through; exception markers clear facts; backward edges contribute an empty state; and unresolved `goto`/`label` pseudo-operations clear facts before the later goto pass materializes their jumps. The composed `switch -> inline -> goto` loop is a permanent regression test.

New `InlineStats` fields are `cfg_dataflow_rounds`, `cfg_merge_facts`, `cfg_constant_propagations`, `cfg_copy_propagations`, `cfg_branches_folded`, `cfg_dead_instructions_pruned`, and `cfg_redundant_jumps_removed`.

### 0.11 -> 0.12 CFG benchmark

Process-isolated CPython 3.13.5 medians:

| Scenario | 0.11 | 0.12 | Speedup | Code |
|---|---:|---:|---:|---:|
| Equal branch merge | 46.40 ns | **43.40 ns** | **1.069x** | 64 -> **40 B** |
| Fixed-point branch chain | 56.47 ns | **49.10 ns** | **1.150x** | 88 -> **44 B** |
| Different-value control | 47.08 ns | 47.38 ns | 0.994x | 64 -> 64 B |

The different-value control is structurally unchanged; its timing is effectively neutral. See `BENCHMARK_V11_V12_CFG_DATAFLOW.txt`.

## Whole-region cross-inline dataflow (0.11)

`inline_calls(..., region_dataflow=True)` is enabled by default. After all supported calls are merged and immediate result fusion has run, 0.11 repeatedly analyzes straight-line regions rooted at inlined result locals. Exact constants and proven local copies may flow through ordinary caller assignments into later inlined consumers. Caller stores are preserved, so normal `locals()` bindings remain present.

For example:

```python
@inline_function(register_only=True)
def flag():
    return True

@inline_function(register_only=True)
def choose(enabled, x):
    if enabled:
        return x + 1
    return x - 1

@inline_calls
def pipeline(x):
    produced = flag()
    alias = produced       # ordinary caller copy
    return choose(alias, x)
```

0.10 could inline both helpers but the caller copy interrupted constant propagation. 0.11 closes that gap: the fact flows through `alias`, the downstream truth test folds, the dead branch is pruned, and the caller assignments remain. CPython 3.13 `STORE_FAST_LOAD_FAST` fused stores are recognized as result/copy edges as well.

The pass is fixed-point and conservative. Labels, exception boundaries, real control-flow instructions, unresolved `goto`/`label` pseudo operations, and remaining non-inlined calls terminate a region. The call barrier is important on CPython 3.13 because `frame.f_locals` is write-through: an arbitrary remaining helper may change its caller's local before the next load.

With `policy="speed"`, copy-only rewrites are discarded unless the pass produces a structural gain such as smaller bytecode, constant folding, dead-block pruning, or redundant-jump removal. `policy="always"` may retain coverage-oriented copy propagation. Disable the pass explicitly with `region_dataflow=False`.

New `InlineStats` fields are `region_dataflow_rounds`, `region_constant_propagations`, `region_copy_propagations`, `region_branches_folded`, `region_dead_instructions_pruned`, and `region_redundant_jumps_removed`.

### 0.10 → 0.11 region-dataflow benchmark

On CPython 3.13.5 the release benchmark measured:

| Scenario | 0.10 | 0.11 | Speedup | Code | Locals |
|---|---:|---:|---:|---:|---:|
| Constant copy chain | 68.13 ns | **57.38 ns** | **1.187×** | 48 → **24 B** | 4 → 4 |
| Constant arithmetic chain | 64.07 ns | **49.21 ns** | **1.302×** | 26 → **14 B** | 2 → 2 |
| Dynamic copy chain | 79.01 ns | 77.00 ns | 1.026× | 30 → 30 B | 4 → 4 |

The dynamic copy-only candidate is rejected by the speed profitability gate; its generated bytecode remains the 0.10 shape. See `BENCHMARK_V10_V11_REGION_DATAFLOW.txt`.

## Cross-inline result fusion (0.10)

Version 0.10 adds a caller-level fusion pass after individual callees have been merged. It targets handoffs such as:

```python
@inline_calls(fusion_strategy="safe")
def pipeline(x):
    a = first(x)
    b = second(a)
    return b - 3
```

The default `fusion_strategy="auto"` resolves to `"safe"`. For an immediate dynamic handoff, safe fusion changes the bytecode shape from:

```text
... producer result ...
STORE_FAST a
LOAD_FAST a
... consumer body ...
```

to:

```text
... producer result ...
COPY 1
STORE_FAST a
... consumer body ...
```

The caller local remains bound and visible through `locals()`, while the original result continues directly on the operand stack.

When the inlined producer returns a literal constant, 0.10 can propagate that constant through the handoff while retaining the `STORE_FAST`. The whole merged caller is then rerun through constant arithmetic/comparison/branch folding and dead-block pruning. This allows separately inlined functions to optimize as one expression region without discarding the caller's local binding.

For maximum speed, `fusion_strategy="aggressive"` may remove a single-use immediate caller-local handoff completely. This can make a sequence of assigned inline calls compile to the same bytecode shape as a nested expression. It is opt-in because the eliminated local is no longer guaranteed to appear as bound in `f_locals` or trace-local snapshots.

Disable the pass with `fusion_strategy="off"`.

New `InlineStats` fields are `fused_result_handoffs`, `constant_result_handoffs`, and `aggressive_result_handoffs`.

### 0.9 → 0.10 fusion benchmark

On CPython 3.13.5, the release benchmark measured:

| Path | 0.9 | 0.10 | Speedup | Code | Locals |
|---|---:|---:|---:|---:|---:|
| Safe dynamic handoff | 43.10 ns | **41.85 ns** | **1.030×** | 32 → 32 B | 3 → 3 |
| Aggressive handoff | 46.83 ns | **38.81 ns** | **1.207×** | 32 → **24 B** | 3 → **1** |
| Safe constant handoff | 40.20 ns | **34.36 ns** | **1.170×** | 40 → **16 B** | 2 → 2 |

Safe constant fusion keeps the caller local stored while removing the consumer branch. See `BENCHMARK_V09_V10_FUSION.txt`.


## 0.9 segmented lifetimes and middle residency

The 0.8 prefix/suffix model is now complete for the scheduler's non-crossing resident interval family. A rejected candidate can face an initial conflict cluster, a final cluster, or both. When both exist, 0.9 may place one **middle resident segment** between them:

```text
local-backed -> stack-resident -> local-backed
```

The transition is admitted only at exact zero-expression-stack boundaries. Density mode also requires a concrete reuse opportunity: another synthetic local lifetime must fit wholly inside the middle hole before the optimizer pays the extra reload/spill instructions. The segmented fast-local allocator then colors the local-backed pieces independently, so that hole can actually recover a physical local slot.

Retained values can now be consumed from deeper expression stacks with an order-preserving SWAP rotation, and a deferred-cleanup alternative lets the value remain underneath the expression until a verified empty-stack/return boundary when that is cheaper. Transient `STORE_FAST -> LOAD_FAST` elimination runs both before and after stack scheduling, preventing stack residency from blocking a cheaper round-trip removal.

The default `stack_strategy="speed"` remains conservative. In the release benchmark its generated speed-path bytecode is unchanged from 0.8; middle/deep segmentation is a density/frame-footprint optimization unless a separate speed proof exists. See `BENCHMARK_V08_V09_SEGMENTED_LIFETIMES.txt`.

New report fields include `stack_middle_splits` and `segmented_local_lifetimes`. The 0.9 focused package suite contained **96 tests**.

## 0.8 live-range splitting

A crossing lifetime no longer has to remain spilled for its entire duration. The optimizer can split it into a stack-resident and fast-local portion.

**Prefix split:** an older spilled value may remain on the operand stack through useful early reads, then move its existing `STORE_FAST` to a proven empty-stack boundary immediately before a younger resident lifetime begins. This adds no instruction when the original store is plain.

**Suffix split:** a spilled value that outlives its crossing resident may reload once at a later exact zero-stack boundary and keep the remaining reads on the operand stack. This deliberately costs one seed instruction, so it is enabled by `stack_strategy="density"`, not by the default speed path. Shortening the spill interval lets the later fast-local coloring pass reuse that slot.

For isolated two-node conflicts, `stack_strategy="speed"` may choose the younger full resident plus an older prefix split only when the younger candidate has a strictly higher static benefit, the prefix has exactly two useful reads, and the split adds no instructions. Wider prefix splits remain density-only after CPython 3.13 calibration showed that extra COPY traffic can lose to fast-local loads.

New statistics: `stack_split_values`, `stack_split_reads`, and `stack_split_instruction_cost`. See `BENCHMARK_V07_V08_LIVE_RANGE_SPLIT.txt`.

## 0.7 selective spilling and density scheduling

Crossing retained lifetimes no longer have one hard-coded allocation policy. The scheduler first builds a conflict graph from eligible synthetic-local live ranges. Properly nested/disjoint ranges are compatible; crossing ranges are conflicts.

`stack_strategy="auto"` is the default. With `policy="speed"` it preserves the latency-calibrated 0.6 lexical choice. With `policy="always"` it selects the density solver. The objective can also be selected explicitly:

```python
@inline_calls(stack_strategy="speed")    # conservative hot-path default
def hot(x): ...

@inline_calls(stack_strategy="density")  # maximize stack-resident values / reduce locals
def compact(x): ...

@inline_calls(stack_strategy="off")      # keep eligible values in fast locals
def baseline(x): ...
```

Density mode solves conflict components up to 18 candidates exactly using a memoized maximum-weight independent-set search. Larger components use a bounded deterministic greedy fallback, preventing pathological decoration-time growth for generated code. Selected nested ranges are then treated as a dependency DAG and lowered innermost-first.

A representative crossing graph has one early value `a` conflicting with both `b` and `c`, while `b` contains `c`. The 0.6/default-speed choice retains one value. Density mode spills `a` and retains `b` plus `c`, reducing the caller from three fast locals to two and from 80 to 78 bytes in the release benchmark. The benchmark also shows why density is not the default: maximum residency can be slightly slower on some CPython 3.13 expressions even when it shrinks code. See `BENCHMARK_V06_V07_SELECTIVE_SPILL.txt`.

New `InlineStats` fields include `stack_scheduler_candidates`, `stack_spilled_values`, `stack_crossing_conflicts`, `stack_max_copy_depth`, `stack_instruction_savings`, `stack_dependency_edges`, and `stack_peak_resident_values`.

## 0.6 multi-value stack scheduling

The inline optimizer now treats stack-resident synthetic values as a nested lifetime allocation problem instead of promoting each temporary independently. Properly nested live ranges are lowered innermost-first, so outer COPY depths account for every already-retained inner value. Crossing live ranges are conservatively spilled to fast locals. This fixes a 0.5.0 correctness bug where two independently promoted nested temporaries could change operand identity.

CPython 3.13 `STORE_FAST_LOAD_FAST` superinstructions are handled directly: retaining the stored value preserves the fused trailing load as an ordinary `LOAD_FAST`, and outer scheduling includes that value in its depth proof. Multi-value groups can therefore eliminate two, three, or more synthetic locals while preserving overloaded-operator order.

The focused package suite now contains **76 tests**, including a direct regression for the 0.5.0 nested-lifetime failure, fused-store scheduling, three simultaneously resident values, crossing-lifetime spills, and overloaded arithmetic ordering. See `BENCHMARK_V05_V06_MULTI_STACK.txt` for measured local-vs-stack results.

## Installation

After publishing the distribution to a package index:

```bash
python -m pip install cpython-extensions
```

From a local 1.0.2 wheel:

```bash
python -m pip install cpython_extensions-1.0.2-py3-none-any.whl
```

The distribution name is `cpython-extensions`; the Python import namespace remains `python_extensions`.

Development install:

```bash
python -m pip install -e '.[test,build]'
```

The inline subsystem currently depends on `bytecode>=0.17,<0.18`. Base package import, switch, goto, CFG verification, and reports remain lazily usable without importing the inline subsystem.

## Compose all three extensions

```python
from python_extensions import (
    case,
    inline_function,
    optimize_extensions,
    switch,
)

@inline_function(register_only=True)
def bump(x):
    return x + 1

@optimize_extensions(
    switch={"case_key_mode": "typed"},
    inline={"policy": "speed"},
    goto=True,
)
def run(kind, rounds):
    count = 0

    label .again
    count = bump(count)

    with switch(kind):
        if case(1):
            bonus = 10
        if case(True):
            bonus = 20
        if case():
            bonus = 30

    if count < rounds:
        goto .again
    return count + bonus
```

The pipeline order is fixed to:

```text
switch -> inline -> goto -> bytecode verification
```

Switch runs first because it may recompile source. Inline then merges registered callees into the lowered function. Goto resolves pseudo labels last so its offset-preserving patch sees final bytecode layout.

## Shared inline regions

Repeated medium/large callees can opt into one appended reusable bytecode body instead of duplicating the body at every call site:

```python
from python_extensions import inline_calls, inline_function

@inline_function(register_only=True, shared_region=True)
def transform(x):
    x = x * 3 + 1
    x = x * 5 - 2
    x = x * 7 + 3
    x = x * 11 - 4
    return x

@inline_calls(
    shared_regions="auto",
    shared_min_calls=3,
    shared_min_body_instructions=12,
)
def work(a, b, c):
    x = transform(a)
    y = transform(b)
    z = transform(c)
    return x + y + z
```

Each call site:

1. evaluates arguments in normal Python order;
2. stores them in frame-local shared parameter slots;
3. stores a frame-local continuation ID;
4. jumps forward to one appended callee region;
5. returns through an O(log n) balanced continuation dispatcher.

The continuation state is frame-local, so threads and recursive calls do not share it. Version 0.3 can also share repeated sites that live under the exact same caller exception-protection context: the reusable body is inserted immediately after that region's `TryBegin`, normal entry jumps over it, and call sites jump backward into it. Exceptions raised inside the shared body remain covered by the original caller handler.

If the callee has its own `try`/exception markers while the caller is already protected, the current `bytecode` IR cannot represent the nested `TryBegin`. That case is deliberately left as an ordinary call rather than risking malformed bytecode.

`shared_regions="auto"` only shares callees explicitly registered with `shared_region=True`. `shared_regions=True` enables sharing for every otherwise-eligible repeated callee. `shared_regions=False` disables the feature.

Shared regions are primarily a **code-size / instruction-cache** tool. Tiny callees normally remain faster when directly duplicated, so the feature is not silently applied to unmarked functions.

## Inline optimizer passes

Version 0.3 runs conservative post-inline optimizations after the call merge:

- **safe local-slot reuse** — repeated copies of the same callee can reuse one synthetic fast-local namespace when CPython emits no potentially-unbound `LOAD_FAST_CHECK`, delete, or clear lifetime operations;
- **caller-local aliasing** — read-only callee parameters can map directly to caller `LOAD_FAST` locals, including definitely-initialized non-parameter locals; `LOAD_FAST_CHECK` arguments are never aliased;
- **constant propagation** — frozen/default exact builtin constants can fold truth tests, primitive comparisons, and bounded primitive arithmetic;
- **dead-block pruning** — branches made unreachable by specialization are removed for exception-free cloned bodies;
- **late stack forwarding** — argument STORE/LOAD round trips that only become redundant after branch folding are eliminated;
- **jump cleanup** — unconditional jumps to the immediately following label disappear.

Example:

```python
@inline_function(register_only=True)
def calculate(x, mode="fast", scale=3):
    if mode == "fast":
        return x * x + scale * 2
    return -x

@inline_calls(policy="always")
def hot(x):
    return calculate(x)
```

For the default call, `mode == "fast"` and `scale * 2` are specialized at decoration time while `x` is aliased directly to the caller slot. User-defined truth/comparison/arithmetic methods are never executed by these folds. Operations that would raise at runtime are left unfused so the exception still occurs at runtime.


### Version 0.4 local dataflow passes

After callee cloning and constant specialization, the inliner now runs additional conservative local/value passes:

- **stack round-trip elimination** — CPython 3.13 patterns such as `STORE_FAST_LOAD_FAST(tmp, tmp)` disappear when the synthetic temporary has no later lifetime; `tmp*tmp` can become `COPY 1` instead of materializing a fast local;
- **single-assignment copy propagation** — straight-line `tmp = caller_local` aliases can be replaced by the proven-bound caller local when neither side is mutated during the lifetime;
- **single-assignment constant propagation** — compiler-generated locals assigned from `LOAD_CONST` can feed the existing constant comparison/branch/arithmetic folders directly;
- **cross-callee fast-local coloring** — non-overlapping synthetic lifetimes from different inline callees share physical fast-local slots when there are no exception markers, backward jumps, checked reads, deletes, or clear-sensitive operations;
- **paired-load preservation** — propagated `LOAD_FAST_LOAD_FAST` operations stay in CPython 3.13's compact superinstruction form when the assembler can encode their indexes.

The dataflow passes touch only compiler-generated `__inl_*` locals. User variable names are never coalesced or removed. Potentially-unbound `LOAD_FAST_CHECK` semantics remain a hard safety boundary, and every rebuilt code object still passes through the shared CFG/stack verifier.

Example:

```python
@inline_function(register_only=True)
def helper(x):
    a = x
    b = a + 1
    c = b * 2
    return c

@inline_calls(policy="always", shared_regions=False)
def hot(x):
    return helper(x)
```

On CPython 3.13.5, the optimized `hot` contains no synthetic fast locals at all: the temporary chain stays on the operand stack and the caller has only its original `x` local.

### Version 0.5 stack-resident lifetime scheduling

Version 0.5 extends stack forwarding beyond immediate store/load round trips. For a straight-line compiler-generated lifetime such as:

```python
temp = x + 1
return temp * 2 + temp
```

the transformed body can retain `temp` below the expression stack:

```text
LOAD_FAST x
LOAD_CONST 1
BINARY_OP +
COPY 1
LOAD_CONST 2
BINARY_OP *
SWAP 2
BINARY_OP +
```

No synthetic fast-local slot is required. Earlier reads use `COPY depth`; the final right-hand binary use is supplied by `SWAP 2`, so operand order remains exactly `left OP temp`, including for overloaded operators. The scheduler may keep a retained value underneath intervening ordinary calls, and CPython stack effects are used to compute the required copy depth.

The proof is deliberately conservative:

- exactly one definite synthetic `STORE_FAST`;
- at least two ordinary reads;
- no labels, exception markers, or branches across the retained lifetime;
- final consumption through `BINARY_OP`;
- no `COPY` depth requiring `EXTENDED_ARG`;
- every rebuilt function must still pass the shared CFG/stack verifier.

Comparison and subscript consumers were evaluated but are not enabled by this speed-oriented pass because they did not show a reliable latency improvement over CPython 3.13 fast-local specialization.

## CPython 3.13.5 optimizer measurements

The original 0.3 measurements below remain useful for the constant/alias layer. Version 0.4 adds a dedicated dataflow benchmark in `BENCHMARK_INLINE_DATAFLOW_CPYTHON_3_13_5.txt`.

Measured on the release harness (results vary by CPU):

| Scenario | Ordinary caller | Optimized caller | Speedup | Final code |
|---|---:|---:|---:|---:|
| Default boolean branch | 76.51 ns | 50.49 ns | 1.515× | 12 B |
| Repeated read (`a*a+b*b`) | 79.30 ns | 63.29 ns | 1.253× | 20 B |
| Four local-heavy calls | 218.17 ns | 158.31 ns | 1.378× | 122 B |
| Default string mode | 77.84 ns | 51.60 ns | 1.509× | 12 B |
| Constant arithmetic | 89.96 ns | 61.63 ns | 1.460× | 24 B |

The specialized default-branch paths reach essentially the same bytecode shape as the direct expression. Repeated local-slot reuse is primarily a frame-size improvement; for the four-call case the same reusable synthetic local group serves all duplicated sites.

### 0.4 dataflow benchmark

A direct 0.3.0/0.4.0 comparison on the same CPython 3.13.5 runtime produced:

| Scenario | 0.3 optimized | 0.4 optimized | 0.3 code/locals | 0.4 code/locals |
|---|---:|---:|---:|---:|
| Ephemeral local chain | 42.53 ns | **34.31 ns** | 30 B / 4 | **18 B / 1** |
| Duplicate temporary | 46.08 ns | **44.22 ns** | 20 B / 2 | **18 B / 1** |
| Copy propagation | 49.65 ns | **46.92 ns** | 20 B / 2 | **16 B / 1** |
| Constant local branch | 37.39 ns | **30.60 ns** | 40 B / 2 | **12 B / 1** |
| Cross-callee live slots | 80.74 ns | 78.94 ns | 46 B / 3 | **46 B / 2** |

Timing varies by CPU and specialization state; code size and local-count reductions are deterministic for these fixtures. The full raw comparison is shipped as `BENCHMARK_V03_V04_DATAFLOW_COMPARISON.txt`.

### 0.5 stack-resident benchmark

A process-isolated 0.4.0/0.5.0 comparison on CPython 3.13.5 produced the following medians (seven process-level minima, each process using seven 300,000-call timing repeats):

| Scenario | 0.4 | 0.5 | Speedup | Code / locals |
|---|---:|---:|---:|---:|
| Repeated right-hand value | 77.95 ns | **74.78 ns** | **1.042×** | 28 B / 2 → **26 B / 1** |
| Deeper repeated value | 88.35 ns | **80.74 ns** | **1.094×** | 40 B / 2 → **38 B / 1** |
| Value retained across `abs()` | 68.00 ns | **63.67 ns** | **1.068×** | 40 B / 2 → **38 B / 1** |

The raw process samples are shipped as `BENCHMARK_V04_V05_STACK_RESIDENT.txt`. Timing varies by CPU; the bytecode/local reductions are deterministic for these fixtures.

Protected shared regions remain intentionally code-size oriented. In the protected benchmark, duplicated inlining produced 456 B of caller bytecode while one protected shared region produced 240 B; direct duplication remained faster in latency.

## Switch

```python
from python_extensions import case, enable_switch, switch

@enable_switch(case_key_mode="typed")
def classify(value):
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

`case_key_mode="python"` preserves normal Python key collisions. `"typed"` distinguishes exact types.

## Goto safety

```python
from python_extensions import enable_goto

@enable_goto  # mode="strict"
def flow(flag):
    if flag:
        goto .done
    result = 10
    goto .return_value

    label .done
    result = 20

    label .return_value
    return result
```

Strict mode rejects jumps whose source and target have different CPython exception-table protection signatures, as well as direct synthetic entry into exception-handler targets.

The legacy low-level behavior remains explicit:

```python
@enable_goto(mode="unsafe")
def experimental(value):
    ...
```

Unsafe mode may bypass `finally`, context-manager cleanup, or exception-stack invariants and should only be used in controlled experiments.

## Shared CFG verifier and reports

Every transformed function is verified after transformation. The verifier checks instruction alignment, jump targets, normal-flow stack consistency, exception-table boundaries, and computed stack depth against `co_stacksize`.

```python
from python_extensions import explain_extensions, verify_code

print(explain_extensions(run))
result = verify_code(run.__code__)
assert result.valid
```

Pipeline reports accumulate on:

```python
run.__python_extensions_reports__
run.__python_extensions_report__
run.__python_extensions_pipeline__
```

## Compatibility imports

```python
from pyswitch import enable_switch, switch, case
from inline_function import inline_function, inline_calls
from pygoto import enable_goto
```

## Verification

The 0.8.0 focused package suite contains **91 tests**. Release certification also ran:

- live-range-split harness: 300 generated functions / 450,000 differential calls, 1,200,000 threaded transformed calls, and 2,000,000 crash-isolated transformed calls;
- selective-spill harness: 300 generated lifetime graphs / 270,000 differential calls, 1,200,000 threaded transformed calls, and 2,000,000 crash-isolated transformed calls;
- multi-stack harness: 1,000,000 randomized differential calls, 800,000 threaded calls, 300 generated functions / 300,000 generated calls, and 2,000,000 crash-isolated calls;
- inherited stack-resident harness: 1,000,000 randomized differential calls, 800,000 threaded calls, and 2,000,000 crash-isolated calls;
- inherited dataflow harness: 1,000,000 randomized calls, 800,000 threaded calls, 100,000 checked-unbound rounds, and 2,000,000 crash-isolated calls;
- inherited optimizer harness: 1,000,000 randomized calls, 800,000 threaded calls, 100,000 unbound-local rounds, and 2,000,000 crash-isolated calls;
- generated dataflow fuzzing: 300 generated callees / 300,000 differential executions;
- clean-wheel base import without `bytecode`, plus full switch/inline/goto smoke with the declared `bytecode 0.17.0` dependency;
- inherited inline v6/v5 suites: 11/11, 4/4, and 28/28 passed in isolated processes;
- inherited switch v16/v16.1 suites: 34/34 and 7/7 passed; switch stress passed 250,000 randomized calls, 80,000 recursive threaded calls, 2,000 generators, 5,000 coroutines, 50 GC cycles, and 1,000,000 crash-isolated fast calls;
- extracted-sdist package suite: 91/91 passed.

Standard local verification commands:

```bash
python -m pytest
python -m build
python -m twine check dist/*
```

## 0.10 certification

The final focused suite passes **103/103** tests. The dedicated fusion harness passes **1,000,000 randomized differential rounds**, **800,000 threaded calls**, and **2,000,000 crash-isolated calls**. The inherited 0.9 segmented-lifetime, 0.8 live-range split, 0.7 selective-spill, 0.6 multi-stack, 0.5 stack-resident, 0.4 dataflow, and optimizer harnesses were rerun and remain green.

The final wheel was clean-installed in two environments: base/switch/goto without the inline dependency, and the full package with the declared `bytecode 0.17.0` dependency. The extracted final sdist independently passes **103/103** tests.
