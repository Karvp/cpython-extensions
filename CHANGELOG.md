# Changelog

## Unreleased

- Re-license the project from MIT to the Mozilla Public License 2.0 (`MPL-2.0`) before public package publication.
- Refresh the GitHub-facing README with installation, quick-start, mode-selection, verification, release-quality, documentation, security, and licensing guidance.
- Add `.github/REPOSITORY_METADATA.md` with recommended repository name, description, topics, website, labels, and GitHub settings.

## 1.0.3

- Productionize the source repository for direct GitHub use without changing the 1.0.2 runtime transformation semantics.
- Add multi-platform CPython 3.13 CI, dev-mode/allocator checks, branch coverage, package artifact validation, CodeQL, Dependabot, dependency review, and scheduled/manual stress workflows.
- Add a reproducible tag-release workflow that builds twice from the tagged commit epoch, validates exact wheel/sdist artifacts, creates GitHub Releases, and keeps PyPI Trusted Publishing opt-in via `PYPI_PUBLISH_ENABLED`.
- Add repository hygiene/version checks, installed-artifact smoke tests, application-style stress scenarios, contributor/security/project documentation, issue/PR templates, and GitHub setup instructions.
- Remove generated package metadata from the source repository and replace machine-local historical benchmark defaults with explicit baseline environment variables.
- Add development/build extras and an 80% branch-coverage gate while retaining CPython `>=3.13,<3.14` and `bytecode>=0.17,<0.18` runtime bounds.

## 1.0.2

- Add opt-in `binding="guarded"` inlining that validates the loaded callable and the function state relevant to the cloned body before taking the inline fast path; stale targets deopt to the exact ordinary CALL using the callable already loaded for that invocation.
- Guard global/closure rebinding, `__code__` replacement, positional and keyword-only default replacement, static/class/instance method replacement, `functools.partial` mutation, and callable-object `type.__call__` replacement without invoking user equality hooks.
- Preserve evaluate-once descriptor/global/closure lookup on both fast and deoptimized paths, including caller exception-handler behavior, async callers, shared inline regions, and concurrent ordinary invocation.
- Retain explicit `binding="frozen"` snapshot semantics as the backward-compatible speed/density mode; `policy="speed"` charges guarded hot-path validation and rejects trivial guarded inlines that cannot repay the guard.
- Add focused guarded-binding regressions and a comprehensive production usage guide.

## 1.0.1

- Made inline registration lifecycle fully transactional across success, replacement, rollback, unregistration, and weak-reference cleanup.
- Fixed failed inline decoration leaving a hidden callable identity eligible for later alias inlining.
- Fixed distinct same-named methods and local/factory functions competing for one registry slot.
- Added automatic weak-reference cleanup for ephemeral registrations.
- Removed the dormant native-`match` switch compiler that was unreachable from the semantics-safe production backend.
- Centralized release version metadata and added the PEP 561 `py.typed` marker.
- Added deep release-hardening regressions and adversarial lifecycle harness coverage.
- Removed the redundant `wheel` PEP 517 build requirement; modern `setuptools>=75` provides the wheel command directly, improving clean/offline build ergonomics.
- Renamed the distribution package to `cpython-extensions` while retaining the `python_extensions` import package, avoiding the normalized PyPI-name collision with the unrelated existing `PythonExtensions` project.
- Keep historical benchmark/certification evidence in the sdist because the long-form README links to those files; generated caches remain excluded.
- Switch marker recognition now accepts exact imported aliases and qualified public API forms such as `python_extensions.switch` / `pe.case` / `pe.fallthrough` without invoking arbitrary attribute hooks.
- Added `tools/build_release.py`: wheel builds honor a fixed `SOURCE_DATE_EPOCH`, and setuptools-selected sdists are canonically repacked with fixed ordering, ownership, and timestamps for byte-for-byte reproducible release artifacts.

## 1.0.0

- Promote the package metadata to **Production/Stable** for the CPython 3.13 release line without changing safe runtime defaults.
- Make source-tree test discovery self-contained by configuring pytest for the `src/` layout; `python -m pytest` now works directly from an extracted sdist or checkout.
- Serialize inline registry, weak-identity-set, and synthetic-name counter mutations with a re-entrant lock. This hardens concurrent decoration/registration, including free-threaded CPython builds, without adding runtime overhead to transformed call paths.
- Standardize per-extension `__version__` exports across `python_extensions.switch`, `python_extensions.inline`, and `python_extensions.goto`.
- Rebuild and verify wheel/sdist artifacts from the clean release tree.

## 0.22.0

- Inline: make guarded-closure `policy="speed"` profitability body-aware. Concrete constant branch/binary/comparison/unary and dead/redundant-code reductions can repay the identity guard; telemetry adds `guarded_closure_speed_accepted` and `guarded_closure_body_credit`.
- Inline: broaden guarded closure call shapes to registered bound methods and positional-only `functools.partial` objects while preserving evaluate-once closure lookup and rebind fallback. Keyword-bearing partials and generic callable instances stay ordinary because their behavior can mutate without object identity changing.
- Switch: replace the `compact_routes="auto"` AST-node heuristic with a side-effect-free compiled CPython bytecode-size proxy. Context-dependent tails fail closed; reports add `auto_compact_plans` and `auto_compact_estimated_bytes_saved`. The speed-first default remains `compact_routes=False`.
- Goto: prove every emitted goto/label-skip jump against the final CFG (exact target, terminator placement, CFG edge, and strict source exception semantics) and reuse one verified CFG for proof plus reporting. Reports add `cfg_verification_passes=1`.
- Added focused 0.22 regressions and a **7,860,000-call** coordinated stress harness covering guarded rebinding, bound methods/partials, unsafe identity-only exclusions, bytecode-aware switch compaction, context-sensitive fail-closed behavior, threaded use, and synthetic goto edge proofing.
- Exact CPython 3.13.5 candidate certification: **343/343** package tests, **59,871,408 portable** full-harness calls/yields, and **61,110,508 total** operations including explicit live-switch compatibility. Seven-process benchmark vs 0.21.0: body-aware guarded inline **1.331x**, trivial guarded speed control **1.097x**, goto decoration **1.327x**, goto runtime **1.014x**, default switch control **1.035x**. The bytecode-aware auto-compaction fixture shrinks **322 -> 238 bytes**; route timing is treated as density-sensitive rather than a universal speed claim.

## 0.21.0

- Inline: recognize exact registered callees loaded from caller closure cells behind an operand-stack identity guard. Rebound cells fall back to the exact originally loaded callable without a second closure read; `policy="speed"` charges guard overhead and leaves trivial guarded calls ordinary, while `policy="always"` enables the guarded path. Public inline telemetry adds `guarded_closure_calls`.
- Switch: `compact_routes="auto"` adds an opt-in density heuristic for shared source-identical fallthrough continuations. The speed-first default remains `False`; auto mode compacts only when duplicated suffix AST work exceeds a conservative threshold.
- Goto: strict transformed code is explicitly CFG-verified immediately after patching and reports `synthetic_jumps_verified`, including zero-valued no-op telemetry for marker-free functions.
- Added a coordinated v21 stress harness covering repeated nonlocal rebinding, auto-compacted fallthrough, goto verification telemetry, and threaded mixed use.

## 0.20.0

- Coordinate a second production refinement across all three extensions while keeping speed-first defaults unchanged.
- `pyswitch`: add opt-in `compact_routes=True` shared-continuation hoisting for fallthrough-heavy general routes. Only source-location-identical AST suffixes are shared, preserving debugger/coverage line identity; the default remains un-compacted because measured route-position throughput can trade a few percent for code density.
- `pyswitch`: expose `shared_continuation_plans`, `shared_continuation_statements`, and `compact_routes` in transformation reports. A representative protected fallthrough fixture shrinks from 610 to 382 bytes (~37% fewer code bytes) when compaction is requested, while the default fixture remains 610 bytes and near runtime parity with 0.19.0.
- Inline: make exact-builtin constant unary/binary/comparison folding iterate to a true fixed point, so folds exposed by later passes (for example `-(a+b)` and `not (a==b)` after argument/default propagation) are eliminated in the same inlining transaction.
- Inline: fold the CPython 3.13 `LOAD_CONST; UNARY_NOT` form exposed after comparison folding, still limited to side-effect-free exact immutable builtin constants. The representative nested constant-chain caller shrinks from 18 to 12 bytes and benchmarks about 1.48x faster than 0.19.0.
- `pygoto`: strict early-jump validation now proves that the entire goto pseudo-statement span has one stable semantic exception-handler stack before placing `EXTENDED_ARG* + JUMP_*` inside it.
- `pygoto`: add `extended_arg_units` and `marker_units_elided` report telemetry (including zero values for marker-free no-op decoration) and use O(1) label-name lookup when computing lowering metrics. Runtime patched bytecode is unchanged from 0.19.0 for equivalent functions.
- Add location-aware shared-continuation regressions, fixed-point inline regressions, synthetic protected-span goto validation, lowering telemetry tests, and `tests/harness_all_extensions_v20.py`.
- Final CPython 3.13.5 source-tree certification: **325/325** package tests, **46,311,408 portable** stress/differential calls, and **47,550,508 total** calls including explicit live compatibility. The 13-process benchmark measures the fixed-point inline fixture at **1.491x** with code shrinking **18 -> 12 bytes**; `compact_routes=True` shrinks the representative fallthrough switch **610 -> 382 bytes** and remains opt-in because route-position throughput can trade a few percent for density.


## 0.19.0

- Coordinate a production-hardening/performance pass across `pyswitch`, inline, and `pygoto`; keep canonical composition fixed at `switch -> inline -> goto` and make final pipeline verification descriptor-aware.
- `pyswitch`: specialize general two-route matched/default plans to a boolean table result plus one truth branch; preserve caller-frame execution and dictionary semantics while avoiding the integer balanced-route comparison.
- `pyswitch`: verify optional `bytecode`-based stack/line rewrites before accepting them and fail closed to the original valid CPython code when a round trip produces an invalid exception-table/stack shape.
- Inline: accept `staticmethod`/`classmethod` descriptors consistently in `inline_function` and `inline_calls`; normalize descriptor/bound-method inputs in registry removal; copy `__kwdefaults__` when rebuilding functions.
- Inline: fold exact-builtin constant unary `-`, `+`, `~`, and `not` operations after propagation without invoking arbitrary user methods; expose `constant_unary_ops_folded` telemetry in `InlineStats` and reports.
- `pygoto`: move relative jumps to the earliest valid units of each goto marker and replace label marker fallthrough with one skip jump, preserving code length and unrelated offsets while removing executed pseudo-expression overhead.
- `pygoto`: compare semantic exception-handler stacks `(target, depth, lasti)` in strict mode so safe `await`/`yield` table splits are accepted while actual protected-region crossings remain rejected; retain final bytecode stack/control-flow verification.
- `pygoto`: support static/class method descriptors, preserve marker-free function identity, copy keyword-default metadata, export the complete public goto error hierarchy, and translate verifier failures into `GotoControlFlowError`.
- Add async coroutine/async-generator strict-goto regressions, long-distance `EXTENDED_ARG` coverage, a dedicated goto production harness, a unified cross-extension harness, descriptor/composition regressions, unary-fold tests, and switch binary-route tests.
- Promote package metadata from Alpha to **Beta**; production defaults remain portable/strict while explicit switch live modes and `enable_goto(mode="unsafe")` remain opt-in low-level facilities.
- Final CPython 3.13.5 certification: **315/315** package tests, **40,687,408 portable** stress/differential calls, and **41,926,508 total** calls including explicit live compatibility; seven-process benchmark vs 0.18.5 measured goto backward **2.821x**, forward-taken **2.766x**, forward-fallthrough **1.765x**, exact-builtin unary inline folds **1.161x-1.355x**, and switch two-route paths **1.054x-1.171x**.


## 0.18.5

- Generalize allocation-free portable `case_key_mode="typed"` routing from one exact case type to mixed plans whose case-type objects all use the ordinary `type` metaclass.
- Build one raw subject dictionary per exact type and route multi-type plans through a bound type-object dictionary whose values are per-type `dict.get` callables; this removes `(type(subject), subject)` allocation without a Python helper frame or generated routing local.
- Retain the 0.18.4 single-type identity branch unchanged; custom-metaclass case plans still fall back wholesale to the conservative tuple-key backend.
- Use a bound empty-dictionary getter as the multi-router miss target, preserving one real subject hash on misses, intrinsic-unhashable default behavior, and genuine user hash failures.
- Keep subject equality inside its exact-type partition, including deliberately colliding cross-type custom values.
- Add `__pyswitch_typed_router_plan_count__` / `__pyswitch_typed_router_type_count__` telemetry and surface both in transformation reports.
- Add focused multi-router regressions plus `tests/harness_switch_typed_router_v185.py`, covering 2-8 type plans, numeric aliases, custom hash/metaclass failures, unhashables, cross-type collisions, stack-resident templates, guarded routes, generated switches, 16-thread sharing, and async concurrency.
- Certified **290/290** package tests, **26,816,008 portable** full-harness calls, and **28,055,108 total** calls including explicit live compatibility on CPython 3.13.5.
- Final interleaved seven-process benchmark vs 0.18.4: 2-type mixed **1.105x**, 4-type mixed **1.131x**, 8-type mixed **1.131x**, expression template **1.063x**, statement template **1.066x**, guarded balanced **1.024x**; unchanged single-type typed controls remain near parity/slightly positive. A longer 13-process 4-type unknown-miss check measured **1.018x**, so miss timing is treated as noise-sensitive rather than a headline claim.

## 0.18.4

- Add a proof-based allocation-free exact-type partition for portable `case_key_mode="typed"` plans whose case keys all have one exact type with the ordinary `type` metaclass.
- Route matching subjects through a raw per-type dictionary, preserving ordinary subject hash/equality behavior while removing per-dispatch `(type(subject), subject)` tuple allocation.
- Make exact-type mismatch execute the subject's real `hash()` exactly once and then default without equality; intrinsic unhashables remain misses and genuine user hash failures propagate.
- Keep mixed-type typed plans and case types with custom metaclasses on the conservative tuple-key backend rather than relying on source-key frequency or runtime-traffic assumptions.
- Defer per-type subtable construction to finalization/canonicalization so custom case keys do not incur an additional compile-time hash/equality pass and literal payload identity remains canonical.
- Add `__pyswitch_typed_partition_plan_count__` / `__pyswitch_typed_partition_type_count__` metadata and surface partition counts in optimization reports.
- Added typed-partition regressions and a **4,760,005-call** stress harness covering ints/strings, exact-type misses, unhashables, user hash failures, collisions, mixed non-transforms, stack-resident templates, guarded routes, threads, async concurrency, and compile-time hash/equality observability.
- Certified **278/278** package tests and **21,986,005 portable / 23,225,105 total** full-harness calls on CPython 3.13.5.
- Final seven-process focused benchmark vs 0.18.3: typed literal **1.179x**, typed expression template **1.116x**, typed statement template **1.082x**, and typed guarded balanced routing **1.045x**.
- Final five-process broad benchmark: typed literal hit-only **1.154x**, mixed hit/miss literal traffic **1.112x**, 64-case typed literal **1.130x**, typed strings **1.138x**, expression templates **1.084x**, statement templates **1.086x**, guarded balanced **1.064x**, and mixed-type non-partition plans **1.009x** (near parity).

## 0.18.3

- Added a depth-aware last-use scheduler for CPython 3.13 stack-resident switch-template payloads.  Final carrier reads at profitable depths are moved into their logical operand position and consumed instead of copied and cleaned later.
- Depth-one payload reads can disappear entirely; depth-two reads use one `SWAP 2`; a sole return carrier at depth three may use `SWAP 2; SWAP 3` because removing the terminal return cleanup still yields a strict instruction-count win.
- Keep deeper carrier reads on the existing `COPY` lowering when rotations would be neutral or slower, preserving a conservative general-purpose profitability rule rather than benchmark-specific rewrites.
- Generalized carrier scheduling across ordinary and fused `LOAD_FAST_LOAD_FAST` payload reads; shallow arithmetic, comparison, container, indexing, formatting, and call shapes share the same structural algorithm.
- Discard the synthetic identity payload immediately after lookup when structurally identical route templates never read it, while still executing the dictionary lookup so custom hash/equality semantics remain observable.
- Added focused regressions for left/right operand ordering, fused tuple loads, depth-three single-argument calls, deliberately unoptimized deep calls, and identity-payload disposal.
- Added `tests/harness_switch_stack_scheduler_v183.py`, covering 18 single-payload and 5 multi-payload expression families, user exceptions, tracing, 16-thread contention, generated compilation, and GC churn.
- Certified **267/267** package tests, **17,226,000** portable full-harness calls, and **18,465,100** total calls including explicit live compatibility.
- Seven-process CPython 3.13.5 focused benchmark vs 0.18.2: right-literal arithmetic **1.076x**, left-literal arithmetic **1.073x**, multi-payload arithmetic **1.020x**, statement templates **1.024x**, with unchanged direct dispatch at **1.008x** (near parity).

## 0.18.2

- Added CPython-3.13 stack-resident payload lowering for branchless portable expression and straight-line statement templates, including multiple varying literal payloads and fused 3.13 fast-local loads.
- Replace payload fast-local loads with depth-correct operand-stack `COPY` operations; multi-payload return cleanup uses one deep `SWAP` plus N `POP_TOP`s while preserving CPython 3.13's required empty value stack at `RETURN_VALUE`.
- Fail the stack transform closed outside CPython 3.13 or for unsupported branching, suspension, exception/control-flow, or bytecode shapes, retaining the established fast-local template as the semantic fallback.
- Restrict statement-template unification to genuinely straight-line statements; `try`, loops, nested control flow, and other complex statements now conservatively use balanced lowering.
- Normalize generated source locations that fall before the transformed function's real first source line, eliminating line-0/pre-function events under `sys.settrace`/debugger/coverage-style instrumentation.
- Relocate isolated/thread-local/per-call live wrapper line tables into the original function's source range; explicit live modes no longer emit synthetic line-0 events.
- Make both Python-key and exact-type live dispatch treat intrinsically unhashable subjects as misses/defaults while still propagating genuine user `__hash__`/`__eq__` `TypeError`s. The hot live lookup remains bound C-level `dict.get` with evaluate-once subject staging and a zero-cost exceptional path.
- Delete live subject/gate compiler temporaries before selected user code executes and add frame-hygiene regressions.
- Added dedicated tracing/profiling/`sys.monitoring`, source-offset, exception, unhashable, typed-key, multi-switch, recursion, generator, coroutine, threading, generated-function, and compile/GC stress coverage.
- Certified **262/262** package tests, **6,450,800** stack-payload calls, **4,703,400** inherited adversarial calls, **3,680,000** inherited production calls, and **1,239,100** live-mode calls (**16,073,300** total across four full harnesses).
- Reproduced CPython 3.13.5 five-process benchmark vs 0.18.1: portable direct 64-case remains at direct-dict-class speed (**70.41 ns** vs **71.29 ns** for the dict reference); stack-resident expression, multi-expression and statement templates are ~1-3% slower in this run in exchange for frame-local transparency; typed live dispatch improves **1.154x**. The semantics-corrected untyped experimental live path is ~8.7% slower; production `auto` retains O(1)/direct-table-class scaling.
- Remaining tooling caveat: shared portable O(1) template routes intentionally share one canonical compiled body, so exact per-route case-body source-line attribution is not guaranteed within that shared region; generated locations are nevertheless constrained to the real function source range.

## 0.18.1

- Delete balanced-dispatch compiler temporaries before any selected user guard/body/default executes and clean specialized assignment temporaries after normal switch completion.
- Reuse a simple `with switch(expr) as name` alias as complex-subject storage, avoiding a redundant compiler subject local and preserving single evaluation.
- Restore the direct-assignment fast path for simple local targets without weakening exception boundaries; descriptor/subscript/destructuring targets remain staged outside the dispatch `try`.
- Rebind nested recursive self-closure cells to the transformed callable (and to isolation wrappers where applicable) without mutating the original function's closure.
- Reject non-trailing, nested, or argument-bearing `fallthrough()` markers at decoration time while allowing nested switch blocks to validate their own marker.
- Added adversarial coverage for frame-local hygiene, Python dictionary hash/equality edge cases, NaN/signed-zero behavior, guard ordering, alias evaluation, try/finally, nested recursion, zero-argument `super()`, PEP 695 type-parameter cells, generator `send`, async recursive closures, double decoration, descriptor failures, threads, and GC collection.
- Certified **232/232** package tests, **4,703,400** new adversarial calls, and the inherited **3,680,000** production-switch calls; explicit live-mode compatibility smoke/thread/recursion/generator tests also pass.
- Scale-stressed direct switches through **4,096 cases**, guarded balanced routing through **1,024 keys**, and forced collision tables through **192 same-hash keys**.
- CPython 3.13.5 five-process hardening benchmark is effectively at parity with 0.18: direct return/local-assignment paths slightly improved in the measured medians, expression/statement templates remain near parity, and the 256-route balanced path pays about 1-2% for deleting hidden dispatch locals before user code.

## 0.18.0

- Promoted the default `pyswitch` portable compiler to a production-oriented semantics-first backend; ordinary `mode="auto"` never mutates executable bytecode.
- Removed native `match` from automatic selection because match equality semantics can disagree with dictionary hash/equality semantics for custom and unhashable subjects.
- Removed the out-of-line return-handler path from production selection so selected user expressions remain in their caller frame and user `TypeError` exceptions cannot be mistaken for dispatch failures.
- Fixed `auto + live_threshold + case_key_mode="typed"` portable fallback so exact-type semantics and optimization reports survive a rejected live compile.
- Added hygienic runtime constants, preventing globals/arguments named `type` or `TypeError` from corrupting generated dispatch.
- Canonicalize literal table payloads against the transformed function's real compiler constant pool, retaining the one-`dict.get` hot path without changing observable CPython constant identity.
- Added partial expression-template lowering: same-shape matched expressions stay average O(1) even when the default has a different shape or is absent.
- Added statement-template lowering for same-shape straight-line multi-statement bodies, keeping execution in-frame while eliminating the growing route tree for common structured cases.
- Hardened unhashable-subject classification so `__hash__ = None` becomes a normal miss without invoking custom metaclass attribute hooks, while real user hashing/equality `TypeError` exceptions propagate.
- Prevented switch rewriting from crossing nested function/class lexical boundaries; independently decorated nested functions continue to work.
- Added strict decorator option validation and production metadata (`__pyswitch_version__`, semantics, mutation, thread-safety, re-entrancy, plan counts).
- Certified **205/205** package tests and **3,680,000** production-switch differential/stress calls, including generated random tables and eight-thread concurrent execution.
- CPython 3.13.5 five-process median benchmark vs 0.17: typed 16-case literal **1.341x**, partial-template 64-case **1.181x**, same-shape three-statement 64-case **1.747x**; literal 64-case remains direct-dict-class speed and heterogeneous fallback remains near parity.

## 0.17.0

- Added path-sensitive lazy materialization as a partial-redundancy fallback for repeated exact-int affine recurrence expressions.
- When global secondary-induction maintenance is unprofitable because a loop path can reach the induction update without consuming the affine value, repeated uses on an affine basic-block path can now be cached only when that path executes.
- Preserve the first affine expression as the synchronization point and snapshot its already-computed value with `COPY 1; STORE_FAST`; later same-version uses become one fast-local load.
- Re-materialize on every re-entry to the affine path, so changing branch decisions cannot observe a stale value and cold/non-affine paths pay no derived update or synchronization instructions.
- Split lazy values around the unique induction write when pre-update and post-update affine uses coexist, preventing a cache from crossing induction-value versions.
- Keep `policy="speed"` conservative: `COPY`/`STORE_FAST` capture cost must be beaten locally and the complete transformed function must still shrink in final bytecode size. Two multiply-only uses therefore remain unchanged, while three become profitable.
- Added `cfg_strength_lazy_values`, `cfg_strength_lazy_uses`, and `cfg_strength_lazy_materializations` diagnostics while retaining aggregate strength-reduction counters.
- Added 7 focused regressions and a full lazy-strength harness covering changing branch membership, pre/post-update versions, threaded execution, and crash-isolated execution.
- Certified 165/165 focused tests plus 300,000 generated changing-path calls, 200,000 pre/post-version calls, 800,000 threaded calls, and 2,000,000 crash-isolated calls. The inherited 0.16 dominance and 0.15 strength-reduction full harnesses each passed another 3.3 million calls.
- CPython 3.13.5 five-process median benchmark: rare affine pair hot path 1.089x, changing-path pair 1.056x, pre/post hot path 1.201x; corresponding cold/control paths remain within about 1% of 0.16.

## 0.16.0

- Extended recurrence strength reduction from same-block/pre-update uses to dominance-verified uses across structured loop blocks.
- Added normal-edge dominator computation and reject lexical backedge shapes whose loop header does not dominate the update and every rewritten affine use.
- Keep the derived recurrence synchronized across uses both before and after the unique induction write; conditional induction updates are mirrored because the derived update is inserted immediately after the original write.
- Added minimum-path profitability analysis for `policy="speed"`: every path that pays the derived update must eliminate more affine-expression instructions than the derived update costs.
- Added post-update path accounting through the current iteration boundary, allowing guaranteed branch work after the induction write to amortize the derived update without crediting early-exit paths.
- Keep rare-path branch shapes unchanged when an update-paying path contains no affine work; `policy="always"` may still select a density-oriented static reduction.
- Removed the 0.15 limitation that rejected affine uses after the induction update.
- Added 8 dominance-focused tests plus a full generated/threaded/crash-isolated dominance-strength harness.
- Preserved the 0.15 full strength-reduction harness, 0.14 affine-recurrence harness, and standalone switch v16 compatibility.

## 0.15.0

- Added exact-int recurrence strength reduction for repeated `i * SCALE [+/- OFFSET]` and `SCALE * i [+/- OFFSET]` expressions.
- Derive a secondary induction local initialized from the recurrence start and updated by `step * scale` after the original induction write.
- Insert derived initialization before the original loop-body label so first entry executes it once while existing backedges skip reinitialization.
- Require same-block pre-update uses in this first dominance-conservative slice; branch-distributed/post-update expressions remain untouched.
- Added profitability gating under `policy="speed"`: repeated expression savings must exceed the derived update cost and reduce final bytecode size.
- Extended affine-update detection to CPython 3.13 fused `STORE_FAST_LOAD_FAST(..., induction)` input shapes.
- Fuse a plain induction `STORE_FAST` with the first derived recurrence load using `STORE_FAST_LOAD_FAST(induction, derived)` when legal.
- Compact multiple derived-recurrence update chains with paired CPython 3.13 fast-local instructions.
- Added `cfg_strength_reduced_values`, `cfg_strength_reduced_uses`, and `cfg_strength_reduction_updates` to `InlineStats` and optimization reports.
- Added focused tests for left/right constant multiplication, negative/decreasing recurrences, multiple derived values, zero-iteration behavior, dynamic-scale controls, and post-update-use fallback.
- Added a dedicated full-profile strength-reduction harness: 300,000 generated affine differential calls, 200,000 dynamic controls, 800,000 threaded calls, and 2,000,000 crash-isolated calls.

## 0.14.0

- Added symbolic exact-integer affine recurrence facts on top of the loop-aware CFG fixed point.
- Detect one-write `x = x +/- constant` / `x +=/-= constant` induction variables from proven exact-int preheader starts.
- Preserve recurrence facts through matching self-updates and fast-local copies without treating the induction value as a concrete constant.
- Fold recurrence-invariant modulo residues and power-of-two low-bit masks.
- Prove monotonic comparison bounds and equality values unreachable by the affine progression.
- Keep dynamic steps, multiple writes, remaining calls, exception regions, and unresolved goto/label pseudo-control as recurrence barriers.
- Added `cfg_affine_recurrences` and `cfg_recurrence_folds` to `InlineStats` and optimization reports.
- Added 11 focused recurrence regressions plus generated, threaded, dynamic-control, and crash-isolated recurrence harnessing.
- Expanded the focused package suite from 129 to 140 tests.

## 0.13.0

- Extended CFG-wide cross-inline dataflow through reducible `while`/`for` backedges instead of treating every loop edge as an empty state.
- Added natural-loop discovery over the transformed `bytecode` IR.
- Added must-analysis loop headers seeded from forward predecessors and constrained by initialized latch states at fixed point.
- Preserve exact constants and dynamic value versions defined outside the natural loop when every incoming edge proves the same value.
- Invalidate dynamic SSA-like tokens whose defining instruction lies inside the loop before carrying them across a backedge, preventing stale cross-iteration value propagation.
- Support loop-carried copies of the same outside value version, multiple `continue` latches, nested loops, and `FOR_ITER` without turning those edges into unconditional fact barriers.
- Keep remaining calls, exception markers, and unresolved `goto`/`label` pseudo operations as hard barriers.
- Added loop diagnostics: `cfg_loop_headers`, `cfg_loop_invariant_facts`, and `cfg_loop_variant_kills`.
- Added 11 focused loop regressions and a generated/threaded/crash-isolated loop-dataflow harness.
- Expanded the focused package suite from 118 to 129 tests.

## 0.12.0

- Added CFG-wide cross-inline dataflow after the 0.11 straight-line region pass.
- Equal constants and SSA-like inlined-result versions now survive forward `if`/`else` merges when every incoming edge proves the same abstract value.
- Added exact-type constant joins so Python equality aliases such as `1` and `True` are never conflated by optimizer facts.
- Added phi-like dynamic copy propagation for branch-local copies of the same pre-branch inlined result.
- Run CFG propagation to a bounded structural fixed point so branch pruning in one round can expose a stronger downstream merge in the next.
- Treat remaining calls, exception markers, loop/backward edges, and unresolved `goto`/`label` pseudo-operations as hard fact barriers.
- Fixed a composed-pipeline regression found during development where an unresolved goto loop could otherwise make a loop-carried local appear constant before the goto pass installed the backward jump.
- Added CFG diagnostics: `cfg_dataflow_rounds`, `cfg_merge_facts`, `cfg_constant_propagations`, `cfg_copy_propagations`, `cfg_branches_folded`, `cfg_dead_instructions_pruned`, and `cfg_redundant_jumps_removed`.
- Added seven focused CFG regressions and a generated/threaded/crash-isolated CFG harness.
- Expanded the focused package suite from 111 to 118 tests.

## 0.11.0

- Added fixed-point whole-region cross-inline dataflow after result fusion.
- Propagates exact constants and proven caller-local copies through multiple inlined callees while preserving caller stores.
- Recognizes CPython 3.13 `STORE_FAST_LOAD_FAST` as both an inlined result destination and a copy edge.
- Reruns constant arithmetic/comparison/branch folding, dead-block pruning, and redundant-jump cleanup after region propagation.
- Added barriers for labels, exception regions, real control flow, unresolved goto/label pseudo operations, and remaining Python calls.
- Added a CPython 3.13 `frame.f_locals` write-through regression proving non-inlined calls are observability barriers.
- Added a speed profitability gate: copy-only rewrites are discarded unless they create a structural gain.
- Added `region_dataflow=True/False` to `inline_function` and `inline_calls`.
- Added six region-specific `InlineStats`/report counters.
- Added 8 focused tests and a generated/threaded/crash-isolated region-dataflow harness.
- Focused package suite is 111/111.

## 0.10.0

- Added cross-inline result fusion for values returned by one inlined call and consumed by subsequent caller/inlined expressions.
- Added `fusion_strategy="auto" | "safe" | "aggressive" | "off"`. `auto` resolves to the semantics-preserving safe path.
- Safe dynamic handoffs replace `STORE_FAST; LOAD_FAST` with `COPY 1; STORE_FAST`, preserving the caller-local binding while keeping the value on the operand stack.
- Added semantics-preserving constant-result propagation across separately inlined callees. The caller local remains stored, while proven direct loads are replaced with the producer constant and branch/comparison/arithmetic cleanup is rerun globally.
- Added explicit aggressive handoff elimination for single-use immediate result locals. This reaches nested-expression bytecode shapes but intentionally relaxes caller `f_locals`/trace-local observability for eliminated locals.
- Added fusion counters to `InlineStats` and optimization reports: `fused_result_handoffs`, `constant_result_handoffs`, and `aggressive_result_handoffs`.
- Added 7 focused fusion tests and a million-scale randomized/threaded/crash-isolated fusion harness.
- Preserved all 0.9 segmented-lifetime, 0.8 split, 0.7 selective-spill, 0.6 multi-stack, 0.5 stack-resident, and earlier dataflow optimizer regressions.

## 0.9.0

- Completed the stack/spill split-state model with a verified middle resident segment: `local -> stack -> local` between older and younger crossing resident clusters.
- Exploited the laminar/non-crossing resident interval structure: a rejected lifetime needs at most an initial conflict cluster, a final conflict cluster, or both, so prefix/suffix/middle segments cover the complete split shape without an unbounded transition DP.
- Made middle splitting profitability-aware: density mode pays the LOAD/STORE transition pair only when another synthetic local lifetime fits wholly inside the resulting fast-local hole and can reuse that physical slot.
- Generalized final retained-value consumption beyond one pending operand using semantics-preserving `SWAP 2 ... SWAP n` rotations.
- Added deferred retained-value cleanup at proven zero-stack or terminal-return boundaries when direct rotation is not the cheapest valid lowering.
- Moved transient synthetic `STORE_FAST -> LOAD_FAST` round-trip elimination both before and after stack scheduling, exposing simpler expression DAGs before residency decisions.
- Reworked fast-local coloring into segmented lifetime allocation: multiple local-backed segments of one synthetic name can be colored independently around stack-resident holes.
- Added `stack_middle_splits` and `segmented_local_lifetimes` to inline statistics and optimization reports.
- Kept the default speed policy conservative: whole-lifetime residency still requires positive instruction savings, while middle/deep segmentation remains density-oriented unless independently proven profitable.
- Added deep-expression, middle-crossing, overloaded-operator, segmented-slot-reuse, generated differential, threaded, and crash-isolated regressions.
- Expanded the focused package suite from 91 to 96 tests.

## 0.8.0

- Added live-range splitting for crossing synthetic inline lifetimes.
- Added suffix splitting at exact zero-expression-stack boundaries: one reload seeds a resident suffix, later reads use COPY/SWAP, and the shortened fast-local lifetime can be reused by slot coloring.
- Added prefix splitting: move the existing spill store later, keep useful early reads stack-resident, and restore the original stack shape before the conflicting lifetime begins.
- Added a split-aware speed refinement for isolated two-node crossings. It is enabled only when the younger full-resident candidate has higher static benefit, the older prefix has exactly two reads, and the split adds no instructions.
- Kept broader prefix and all suffix splitting density-oriented after pinned-core CPython 3.13 calibration showed that extra COPY/reload traffic is not a universal latency win.
- Added exact linear stack-depth proof for split seed boundaries; nonzero-expression-stack promotion is rejected.
- Added `stack_split_values`, `stack_split_reads`, and `stack_split_instruction_cost` to `InlineStats` and transformation reports.
- Added overloaded-operator, unsafe-seed rejection, slot-reuse, speed-policy, generated differential, threaded, and crash-isolated split tests.
- Expanded the focused package suite from 84 to 91 tests.

## 0.7.0

- Added selective spilling for crossing stack-resident inline lifetimes.
- Added a crossing-conflict graph and maximum-weight non-crossing solver for density-oriented scheduling.
- Small conflict components are solved exactly; components above 18 nodes use a deterministic bounded greedy fallback to cap decoration complexity.
- Added a retained-lifetime dependency DAG metric and peak simultaneous stack-residency metric.
- Added `stack_strategy="auto" | "speed" | "density" | "off"` to `inline_calls()` and `inline_function()`.
- `stack_strategy="auto"` maps `policy="speed"` to the latency-calibrated 0.6 lexical strategy and `policy="always"` to exact density scheduling.
- `stack_strategy="speed"` preserves the 0.6 crossing-lifetime choice so the default path does not trade hot-path latency for code density.
- `stack_strategy="density"` can spill one long crossing lifetime in favor of multiple compatible nested lifetimes, reducing fast-local count and generated code size.
- Extended `InlineStats` and transformation reports with scheduler candidates, spills, crossing conflicts, maximum COPY depth, instruction savings, dependency edges, and peak resident values.
- Added arbitrary-permutation lifetime fuzzing across speed/density/off strategies, threaded stress, and crash-isolated execution.
- `stack_strategy="off"` bypasses scheduler candidate analysis entirely, providing a true zero-scheduler baseline.
- Expanded the focused package suite from 76 to 84 tests.

## 0.6.0

- Reworked stack-resident inline scheduling into a lifetime-aware nested allocation pass.
- Fixed a 0.5.0 semantic bug where independently promoting overlapping nested temporaries could compute stale COPY depths and change operand identity.
- Reject crossing retained lifetimes and keep the conflicting value in a fast-local spill slot.
- Lower compatible nested lifetimes innermost-first so outer COPY depths include inner retained values.
- Added direct support for CPython 3.13 `STORE_FAST_LOAD_FAST` when its stored value becomes stack-resident.
- Added multi-value tests covering two and three retained temporaries, fused stores, crossing spills, and overloaded operator order.
- Added a generated multi-stack differential fuzzer and dedicated threaded/crash-isolated harness.

## 0.5.0

- Added stack-resident lifetime scheduling for repeated-read compiler-generated inline locals.
- A single-assignment synthetic value can remain below the expression stack; earlier reads lower to depth-aware `COPY`, and the final right-hand `BINARY_OP` use lowers to `SWAP 2` so the retained value is consumed without a fast-local slot.
- Added support for earlier reads embedded in CPython 3.13 `LOAD_FAST_LOAD_FAST` superinstructions by safely expanding only the affected paired load.
- Stack-resident values may survive across intervening ordinary calls; stack depth is derived from instruction stack effects rather than hard-coded call shapes.
- Preserved overloaded binary operand order with dedicated side-effect/operator tests.
- Added a speed guard that rejects `COPY` depths above 255 rather than introducing `EXTENDED_ARG` overhead.
- Evaluated comparison/subscript final consumers and intentionally kept them on fast locals because they did not produce a reliable speed win on CPython 3.13.
- Extended `InlineStats` and transformation reports with `stack_resident_values`.
- Added focused depth, call, control-flow, overloaded-operator, comparison/subscript fallback, and encoding-guard tests.
- Added a dedicated stack-resident stress harness: 1,000,000 randomized differential rounds, 800,000 threaded calls, and 2,000,000 crash-isolated calls.
- Expanded the focused package suite from 63 to 71 tests.

## 0.4.0

- Added SSA-like straight-line dataflow for compiler-generated inline locals.
- Eliminated transient `STORE_FAST`/`LOAD_FAST` and CPython 3.13 `STORE_FAST_LOAD_FAST(x, x)` round trips when the synthetic local has no remaining lifetime.
- Added stack duplication lowering (`COPY 1`) for one-assignment temporaries used twice immediately, avoiding a fast-local slot.
- Added single-assignment synthetic copy propagation from proven `LOAD_FAST` sources, with source-write/control-flow guards.
- Added single-assignment synthetic constant propagation, enabling the existing constant branch/comparison/arithmetic passes for locals introduced inside callees.
- Preserved compact `LOAD_FAST_LOAD_FAST` superinstructions during copy propagation when CPython can encode them; high local indexes automatically expand through the assembler.
- Added conservative cross-callee linear-scan fast-local coloring for non-overlapping synthetic lifetimes. Exception markers, backward jumps, checked reads, deletes, and clear-sensitive forms disable the allocation pass.
- Extended `InlineStats` and transformation reports with `synthetic_roundtrips_elided`, `synthetic_copies_propagated`, `synthetic_constants_propagated`, and `coalesced_local_slots`.
- Added high-index, source-mutation, checked-unbound, branch, nested-chain, and cross-callee slot tests.
- Added a generated differential harness covering 300 generated callees / 300,000 calls, plus a dedicated 1,000,000-round threaded/crash-isolated dataflow harness.
- Expanded the focused package suite from 53 to 63 tests while retaining inherited v5/v6 inline and v16/v16.1 switch compatibility.

## 0.3.0

- Added shared fast-local dataflow analysis in `_core.dataflow`.
- Repeated duplicated inline sites now reuse one synthetic local namespace when CPython proves no potentially-unbound/delete lifetime semantics; this reduces `co_nlocals` without stale-value leakage.
- Added direct caller-local aliasing for read-only callee parameters when call arguments are CPython-proven initialized `LOAD_FAST` values. Potentially-unbound `LOAD_FAST_CHECK` sites remain unaliased.
- Added post-inline constant propagation for exact immutable truth values, primitive comparisons, and bounded primitive binary operations. Runtime exceptions are never moved to decoration time.
- Added dead-block pruning, redundant-jump removal, and late operand-stack forwarding after constant specialization.
- Shared inline regions can now stay inside one caller exception-protection context by placing the reusable region immediately after the active `TryBegin`; caller handlers continue to catch exceptions raised by the shared body.
- Protected shared regions conservatively reject callees that contain their own exception markers because `bytecode` 0.17 does not support nested `TryBegin` pseudo instructions. Ordinary inlining now skips those sites rather than failing decoration.
- Extended `InlineStats` and transformation reports with optimizer/fallback counters.
- Added optimizer differential/stress harness: 1,000,000 randomized rounds, 800,000 threaded calls, 100,000 unbound-local checks, and 2,000,000 crash-isolated calls.
- Expanded package suite to 53 focused tests and retained inherited v5/v6 inline and v16/v16.1 switch compatibility.

## 0.2.0

- Added private `python_extensions._core` instruction decoder, CFG builder, exception-region model, normal-flow stack verifier, and transformation reports.
- All switch, inline, and goto transformations now pass through common post-transform verification.
- Added `explain_extensions()` and public `verify_code()` diagnostics.
- Added `optimize_extensions()` with canonical `switch -> inline -> goto -> verify` ordering.
- Transformation reports now accumulate across composed stages.
- Added strict goto mode as the default; it rejects jumps across exception/cleanup protection boundaries and direct handler entry.
- Retained `enable_goto(mode="unsafe")` for explicit low-level experiments.
- Added reusable appended inline regions for repeated eligible callees.
- Added `shared_region=True` registration hint and `shared_regions`, `shared_min_calls`, and `shared_min_body_instructions` controls.
- Shared inline regions use frame-local parameter/continuation slots and an O(log n) balanced return dispatcher.
- Calls inside caller exception regions are excluded from shared-region movement.
- Extended `InlineStats` with `calls_shared` and `shared_regions`.
- Preserved compatibility helpers `_self_test_live_layout` and `_encode_jump` through the legacy `pyswitch` shim.
- Expanded package suite from 28 to 40 focused tests.
